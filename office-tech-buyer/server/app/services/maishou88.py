"""maishou88 上游 API 封装。

对外暴露三个语义化方法（``search`` / ``detail`` / ``target_url``），内部统一处理：
- 请求头伪装（App UA / Web UA / Referer / openid / version）
- 超时、指数退避重试、并发信号量与最小请求间隔限流
- 上游返回结构差异（``data`` 为 list 或 dict）与 ``utf-8-sig`` BOM 解析
- 统一的错误包装 ``UpstreamError``
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any

import httpx

from ..config import SOURCE_NAMES, settings


class UpstreamError(RuntimeError):
    """上游不可用或返回异常结构。"""

    def __init__(self, message: str, *, status: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload


# --------------------------------------------------------------------------- #
# 限流器：保证对上游的最小请求间隔，避免触发风控
# --------------------------------------------------------------------------- #
class _RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(0.0, min_interval)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self._last + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._last = now


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class MaishouClient:
    """异步上游客户端，进程内复用一个 ``httpx.AsyncClient``。"""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(settings.upstream_concurrency)
        self._limiter = _RateLimiter(settings.upstream_min_interval)

    # -- 生命周期 ---------------------------------------------------------- #
    async def startup(self) -> None:
        if self._client is None or self._client.is_closed:
            timeout = httpx.Timeout(
                settings.upstream_timeout, connect=settings.upstream_connect_timeout
            )
            self._client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"Accept": "application/json", "Accept-Encoding": "gzip, deflate"},
            )

    async def shutdown(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            raise UpstreamError("上游客户端尚未初始化，请先在 startup 中创建")
        return self._client

    # -- 请求头 ------------------------------------------------------------ #
    def _app_headers(self) -> dict[str, str]:
        return {
            "User-Agent": settings.upstream_app_ua,
            "Referer": settings.upstream_referer,
            "openid": settings.upstream_openid,
            "version": settings.upstream_version,
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def _web_headers(self) -> dict[str, str]:
        return {
            "User-Agent": settings.upstream_web_ua,
            "Referer": settings.upstream_referer,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # -- 通用请求 ---------------------------------------------------------- #
    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        data: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.startup()
        last_exc: Exception | None = None

        for attempt in range(1, settings.upstream_retries + 1):
            try:
                async with self._semaphore:
                    await self._limiter.acquire()
                    response = await self.client.request(
                        method, url, headers=headers, data=data, json=json_body
                    )
                if response.status_code >= 500 or response.status_code == 429:
                    raise UpstreamError(
                        f"上游返回 {response.status_code}",
                        status=response.status_code,
                        payload=response.text[:500],
                    )
                if response.status_code >= 400:
                    raise UpstreamError(
                        f"上游返回 {response.status_code}",
                        status=response.status_code,
                        payload=response.text[:500],
                    )
                # 上游有时返回带 BOM 的 JSON
                text = response.content.decode("utf-8-sig", errors="replace")
                try:
                    payload = json.loads(text)
                except ValueError as exc:
                    raise UpstreamError(
                        "上游返回非 JSON 内容", status=response.status_code, payload=text[:500]
                    ) from exc
                if not isinstance(payload, dict):
                    raise UpstreamError(
                        "上游返回顶层结构不是对象",
                        status=response.status_code,
                        payload=str(payload)[:500],
                    )
                return payload
            except (httpx.TimeoutException, httpx.TransportError, UpstreamError) as exc:
                last_exc = exc
                if attempt >= settings.upstream_retries:
                    break
                # 指数退避 + 抖动，避免重试风暴
                delay = settings.upstream_backoff * (2 ** (attempt - 1))
                await asyncio.sleep(delay + random.uniform(0, delay * 0.3))

        if isinstance(last_exc, UpstreamError):
            raise last_exc
        raise UpstreamError(f"上游请求失败: {last_exc}")

    # -- 业务方法 ---------------------------------------------------------- #
    async def search(
        self,
        keyword: str,
        source: int = 0,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """搜索商品，返回原始行列表（上游 ``data`` 字段）。"""
        payload = await self._request_json(
            "POST",
            settings.upstream_search_url,
            headers=self._app_headers(),
            data={
                "isCoupon": 0,
                "keyword": str(keyword),
                "openid": settings.upstream_openid,
                "order": "desc",
                "page": int(page),
                "pddListId": "",
                "sort": "",
                "sourceType": str(source),
                "user_id": "",
            },
        )
        rows = payload.get("data")
        if isinstance(rows, dict):
            # 少数渠道把结果包在 data.list / data.items 里
            rows = rows.get("list") or rows.get("items") or []
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    async def detail(self, goods_id: str, source: int) -> dict[str, Any]:
        """商品详情（原始 data 对象）。"""
        payload = await self._request_json(
            "POST",
            settings.upstream_detail_url,
            headers=self._web_headers(),
            json_body={
                "goodsId": str(goods_id),
                "sourceType": str(source),
                "inviteCode": settings.upstream_invite_code,
                "supplierCode": "",
                "activityId": "",
                "isShare": "1",
                "token": "",
                "keyword": "",
                "usageScene": 5,
            },
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def target_url(self, goods_id: str, source: int) -> dict[str, Any]:
        """转链信息（真实购买链接 / 淘口令）。"""
        payload = await self._request_json(
            "POST",
            settings.upstream_target_url,
            headers=self._web_headers(),
            json_body={
                "goodsId": str(goods_id),
                "sourceType": str(source),
                "inviteCode": settings.upstream_invite_code,
                "supplierCode": "",
                "activityId": "",
                "isShare": "1",
                "token": "",
                "isDirectDetail": 0,
            },
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    # -- 清洗 -------------------------------------------------------------- #
    @staticmethod
    def clean_search_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        """把上游原始行裁剪为统一结构，并计算券后到手价参考。

        ``limit`` 是**跨渠道合计**的上限，而上游是按相关度把各渠道结果交错
        返回的，所以截断会按渠道偏斜（实测「戴尔 PowerEdge R750」京东腿从
        20 条被砍到 2 条，纯原装行则整体落在头部之后）。调用方请给足
        ``settings.max_items_per_query``，不要指望在小 limit 下「采样」到全渠道。
        """
        cleaned: list[dict[str, Any]] = []
        for idx, raw in enumerate(rows[:limit], start=1):
            source = raw.get("sourceType")
            try:
                source_int = int(source)
            except (TypeError, ValueError):
                source_int = 0
            item = {
                "idx": idx,
                "goodsId": str(raw.get("goodsId") or ""),
                "source": source_int,
                "platform": SOURCE_NAMES.get(source_int, f"渠道{source_int}"),
                "title": raw.get("title") or "",
                "shopName": raw.get("shopName") or "",
                "originalPrice": _to_float(raw.get("originalPrice")),
                "actualPrice": _to_float(raw.get("actualPrice")),
                "couponPrice": _to_float(raw.get("couponPrice")),
                "commission": _to_float(raw.get("commission")),
                "monthSales": _to_int(raw.get("monthSales")),
                "picUrl": raw.get("picUrl") or "",
            }
            item["coupon_desc"] = _coupon_desc(item)
            cleaned.append(item)
        return cleaned

    @staticmethod
    def clean_detail(
        goods_id: str,
        source: int,
        detail: dict[str, Any],
        target: dict[str, Any],
    ) -> dict[str, Any]:
        """解析详情：购买链接、淘口令、专票与质保。"""
        title = detail.get("title") or ""
        blob = title + " " + str(detail.get("description") or "") + " " + str(
            detail.get("shopName") or ""
        )
        return {
            "goodsId": str(goods_id),
            "source": int(source),
            "platform": SOURCE_NAMES.get(int(source), f"渠道{source}"),
            "title": title,
            "shopName": detail.get("shopName") or "",
            "buy_url": target.get("appUrl") or target.get("schemaUrl") or "",
            "coupon_code": target.get("kl") or "",
            "original_price": _to_float(
                detail.get("originalPrice") or detail.get("reservePrice")
            ),
            "actual_price": _to_float(
                detail.get("actualPrice") or detail.get("zkFinalPrice")
            ),
            "coupon_price": _to_float(detail.get("couponPrice")),
            "month_sales": _to_int(detail.get("monthSales") or detail.get("volume")),
            "pic_url": detail.get("picUrl") or detail.get("pictUrl") or "",
            "invoice": parse_invoice(blob),
            "warranty": parse_warranty(blob),
            "raw_detail": detail,
            "raw_target": target,
        }


def _to_float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number <= 0 else round(number, 2)


def _to_int(value: Any) -> int | None:
    if value in (None, "", "null"):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coupon_desc(item: dict[str, Any]) -> str:
    original = item.get("originalPrice")
    actual = item.get("actualPrice")
    if original and actual and original > actual:
        return f"原价 ¥{original}，券后 ¥{actual}，省 ¥{round(original - actual, 2)}"
    return ""


# --------------------------------------------------------------------------- #
# 专票 / 质保解析
# --------------------------------------------------------------------------- #
INVOICE_SIGNALS = ("增值税专用发票", "专票", "可开专票", "开具专票", "增值税发票", "13%专票")
INVOICE_NEGATIVE = ("不开发票", "不开专票", "无发票", "仅开普票", "普通发票")

#: 质保说明：先抓「质保类型 + 期限」，期限缺失时也能给出类型
WARRANTY_RE = re.compile(
    r"(全国联保|整机质保|整机保修|质保|保修|上门服务|上门安装|以换代修|三包)"
    r"\s*[:：]?\s*(\d{1,3}\s*(?:年|个月|月))?"
)


def parse_invoice(text: str) -> dict[str, Any]:
    """解析增值税专用发票开具能力。

    只在有明确信号词时给出 ``supported``；仅出现"发票"而无法判断类型时返回
    ``None``，由客户端如实标注"专票未标注"，不得默认按支持处理。

    注意：上游详情接口**不提供开票资质字段**。实测扫描 detail 全部 69~70 个
    字段（京东/拼多多/天猫）均无「发票/专票/质保/保修/联保/三包」信号，``desc``
    是标题回显、``shopInfo`` 恒为空数组。所以这里喂进来的 ``blob`` 只是标题+
    店铺名的拼接，本函数对真实数据**必然**落在「未标注」分支——它的价值在于
    一旦上游补上该字段就能立即生效，以及宿主 AI 拿到合成文本时的行为可测
    （见 tests/test_detail.py 的 TestInvoiceParsing）。
    """
    blob = text or ""
    for neg in INVOICE_NEGATIVE:
        if neg in blob:
            return {"supported": False, "evidence": neg, "confidence": "high"}
    for sig in INVOICE_SIGNALS:
        if sig in blob:
            return {"supported": True, "evidence": sig, "confidence": "high"}
    if "发票" in blob:
        return {
            "supported": None,
            "evidence": "标题/详情提及发票但未明确类型",
            "confidence": "low",
        }
    return {
        "supported": None,
        "evidence": "上游详情接口未提供开票资质字段（已扫描全部返回字段）",
        "confidence": "none",
    }


def parse_warranty(text: str) -> dict[str, Any]:
    """解析商家质保说明，返回质保类型与期限。"""
    match = WARRANTY_RE.search(text or "")
    if not match:
        return {"text": "未标注", "term": None, "kind": None, "confidence": "none"}
    term = (match.group(2) or "").strip().replace(" ", "")
    return {
        "text": match.group(0).strip(),
        "term": term or None,
        "kind": match.group(1),
        "confidence": "high",
    }


#: 进程内单例
maishou = MaishouClient()
