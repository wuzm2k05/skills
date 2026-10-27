"""pytest 公共夹具：打桩上游 + 提供 ASGI 测试客户端。

上游被 ``httpx.MockTransport`` 完全替换，因此整套测试无需联网、
不依赖 maishou88 真实可用性，也不会产生真实的上游请求。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx
import pytest
import pytest_asyncio

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services.maishou88 import maishou  # noqa: E402

PLATFORMS = {1: "淘宝/天猫", 2: "京东", 3: "拼多多"}


# --------------------------------------------------------------------------- #
# 上游假响应
# --------------------------------------------------------------------------- #
def search_payload(keyword: str, source: int, page: int) -> dict:
    """按关键词造 3 条稳定的假商品，覆盖包装规格相关的典型标题写法。"""
    rows = []
    for idx, source_type in enumerate((2, 1, 3), start=1):
        if "复印纸" in keyword:
            spec = {1: "5包箱装 2500张", 2: "1包 500张 单包", 3: "20包 整箱批发"}[idx]
            title = f"{keyword} {spec}"
        elif "88A" in keyword and "原装" in keyword:
            title = "惠普(HP) CC388A 88A 原装碳粉盒 硒鼓"
        elif "88A" in keyword:
            title = "适用惠普 88A 碳粉盒 兼容 M126a M1136 国产"
        else:
            title = f"{keyword} 商用办公款 {PLATFORMS.get(source_type, '')}"
        rows.append(
            {
                "goodsId": f"{source_type}{page}{idx}0001",
                "sourceType": source_type,
                "title": title,
                "shopName": "京东自营官方旗舰店" if source_type == 2 else "品牌专营店",
                "originalPrice": 1299 - idx * 10,
                "actualPrice": 92.5 if "复印纸" in keyword else 1099 - idx * 50,
                "couponPrice": None,
                "commission": 12.5,
                "monthSales": 3000 + idx,
                "picUrl": "https://example.com/p.jpg",
            }
        )
    return {"code": 200, "message": "ok", "data": rows}


DETAIL_DATA = {
    "title": "惠普 M126a 多功能一体机 支持增值税专用发票 全国联保 1年上门服务",
    "shopName": "京东自营官方旗舰店",
    "originalPrice": 1299,
    "actualPrice": 1099,
    "monthSales": 5000,
    "picUrl": "https://example.com/p.jpg",
}


def make_handler(*, search_fail_times: int = 0) -> Callable[[httpx.Request], Any]:
    """返回一个 MockTransport handler。``search_fail_times`` 用于模拟上游持续 5xx。"""
    state = {"search_fails": search_fail_times}

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "homepage/searchList" in url:
            if state["search_fails"] > 0:
                state["search_fails"] -= 1
                return httpx.Response(503, text="upstream busy")
            params = dict(httpx.QueryParams(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json=search_payload(
                    params.get("keyword", ""),
                    int(params.get("sourceType", 0)),
                    int(params.get("page", 1)),
                ),
                headers={"content-type": "application/json"},
            )
        if "goods/detail" in url:
            return httpx.Response(200, json={"code": 200, "data": DETAIL_DATA})
        if "share/getTargetUrl" in url:
            payload = json.loads(request.content.decode("utf-8"))
            if int(payload["sourceType"]) == 3:
                # 拼多多：模拟渠道未授权，拿不到转链
                return httpx.Response(200, json={"code": 200, "data": {}})
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "appUrl": f"https://item.example.com/{payload['goodsId']}",
                        "kl": f"￥ABC{payload['goodsId']}￥",
                    },
                },
            )
        return httpx.Response(404, text="not found")

    return handler


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def clean_unmatched_log() -> None:
    """每条用例前清空未命中日志（含滚动分片），避免相互干扰。

    日志写入器是进程内单例，配置被 ``monkeypatch`` 改过的用例会换到新实例，
    但已写入的分片分属于不同实例，只有按文件名清理才能保证互不污染。
    """
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    for stale in settings.log_dir.glob(f"{settings.unmatched_log.name}*"):
        stale.unlink()
    yield
    for stale in settings.log_dir.glob(f"{settings.unmatched_log.name}*"):
        stale.unlink()


@pytest_asyncio.fixture
async def install_upstream() -> AsyncIterator[Callable[..., None]]:
    """把上游客户端换成 MockTransport；返回的函数可用于即时切换故障模式。"""

    async def install(*, search_fail_times: int = 0) -> None:
        await maishou.startup()
        await maishou.shutdown()
        maishou._client = httpx.AsyncClient(  # noqa: SLF001  (测试专用注入)
            timeout=httpx.Timeout(5.0),
            transport=httpx.MockTransport(make_handler(search_fail_times=search_fail_times)),
        )

    await install()
    try:
        yield install
    finally:
        await maishou.shutdown()


@pytest_asyncio.fixture
async def api(install_upstream) -> AsyncIterator[httpx.AsyncClient]:
    """指向 ASGI 应用的测试客户端。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
