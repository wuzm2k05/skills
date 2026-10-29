"""搜索路由：品类识别 → 上游并发查询 → 统一结构输出。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Request

from ...config import SOURCE_NAMES, settings
from ...services import category_engine as engine
from ...services.maishou88 import UpstreamError, maishou
from ..deps import client_ip

router = APIRouter(tags=["search"])


async def _run_query(
    keyword: str,
    source: int,
    page: int,
    limit: int,
    kind: str,
    label: str,
) -> dict[str, Any]:
    """执行单条上游查询并清洗；上游异常被捕获为结果内的 ``error`` 字段。"""
    try:
        rows = await maishou.search(keyword, source=source, page=page)
    except UpstreamError as exc:
        return {
            "keyword": keyword,
            "kind": kind,
            "label": label,
            "source": source,
            "page": page,
            "platform": SOURCE_NAMES.get(source, "全部"),
            "count": 0,
            "status": "UPSTREAM_ERROR",
            "error": exc.message,
            "channels": [],
            "items": [],
        }

    if not rows:
        return {
            "keyword": keyword,
            "kind": kind,
            "label": label,
            "source": source,
            "page": page,
            "platform": SOURCE_NAMES.get(source, "全部"),
            "count": 0,
            "status": "NO_RESULTS",
            "error": None,
            "channels": [],
            "items": [],
        }

    items = maishou.clean_search_rows(rows, limit)
    return {
        "keyword": keyword,
        "kind": kind,
        "label": label,
        "source": source,
        "page": page,
        "platform": SOURCE_NAMES.get(source, "全部"),
        "count": len(items),
        "status": "OK",
        "error": None,
        "channels": _channels_of(items),
        "items": items,
    }


def _channels_of(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """统计一批结果实际来自哪些渠道（供宿主 AI 如实声明覆盖范围）。

    上游 ``sourceType=0`` 并不等于「全部渠道」：实测 8 个关键词 × 2 页的并集只有
    1/2/3（淘宝天猫/京东/拼多多），苏宁、唯品会、抖音、快手零条；单独指定
    ``source=4/5/7/8`` 却各有 20 条。所以覆盖范围必须由**实际返回的数据**推导，
    不能由请求参数推导。
    """
    counter: dict[int, int] = {}
    for item in items:
        source = int(item.get("source") or 0)
        counter[source] = counter.get(source, 0) + 1
    return [
        {
            "source": source,
            "platform": SOURCE_NAMES.get(source, f"渠道{source}"),
            "count": count,
        }
        for source, count in sorted(counter.items())
    ]


@router.get("/search", summary="办公设备/耗材全网比价搜索")
async def search(
    request: Request,
    keyword: str = Query(..., min_length=1, max_length=200, description="搜索关键词"),
    source: int = Query(0, ge=0, le=99, description="渠道 0:全部 1:淘宝/天猫 2:京东 3:拼多多"),
    page: int = Query(1, ge=1, le=50, description="页码"),
) -> dict[str, Any]:
    """全品类搜索入口。

    - 命中映射表：并发查询「主设备 + 原装耗材 + 兼容耗材/扩展配件」，返回 ``matched: true``。
    - 未命中映射表：记录 ``logs/unmatched.log``，仅用原始关键词查询，返回 ``matched: false``，
      由客户端宿主 AI 按 ``references/answer-rules.md`` 改用「设备型号 + 耗材类别词」二次调用。
    """
    ip = client_ip(request)
    match = engine.build_match(keyword)

    if not match.matched:
        line = engine.log_unmatched(keyword, ip, extra=f"source={source} page={page}")
        # 未命中：直接用原始关键词做基础搜索
        primary = await _run_query(
            keyword=match.primary_keyword,
            source=source,
            page=page,
            limit=settings.max_items_per_query,
            kind="primary",
            label="原始关键词直搜",
        )
        enriched = _enrich_query(primary, match)
        return {
            "matched": False,
            "keyword": keyword,
            "normalized_keyword": match.normalized,
            "category": match.category,
            "matched_by": match.matched_by,
            "log_line": line,
            "client_ip": ip,
            "channels_covered": _merge_channels([enriched]),
            "hint": (
                "云端静态映射表未命中该设备。请宿主 AI 改用「设备型号 + 耗材类别词」再次调用本接口"
                "（如「惠普 DeskJet 2720 墨盒」「奔图 P2200 碳粉盒」），以返回的 "
                "items[].attributes.supply_type / model_code 判断实际在售的原装与兼容耗材；"
                "不要凭设备型号推测耗材型号，最后与本次主设备结果合并呈现。"
            ),
            # 未命中同样要补属性解析：补搜耗材时宿主 AI 靠 items[].attributes
            # 区分原装/兼容、读 model_code，缺了这一层就只能看到裸标题。
            "queries": [enriched],
        }

    # 命中：主查询 + 关联查询并发执行
    plan = match.plan(settings.max_related_queries)
    tasks = []
    for step in plan:
        tasks.append(
            _run_query(
                keyword=step["keyword"],
                source=step.get("source", source) or source,
                page=page if step["kind"] == "primary" else 1,
                limit=settings.max_items_per_query,
                kind=step["kind"],
                label=step["label"],
            )
        )
    queries = [_enrich_query(q, match) for q in await asyncio.gather(*tasks)]

    return {
        "matched": True,
        "keyword": keyword,
        "normalized_keyword": match.normalized,
        "category": match.category,
        "matched_by": match.matched_by,
        "match_key": match.match_key,
        "device_name": match.device_name,
        "attributes": match.attributes,
        "unit_calculation": match.unit_calculation,
        "unit_price_basis": match.unit_price_basis,
        "notes": match.notes,
        "client_ip": ip,
        "channels_covered": _merge_channels(queries),
        "queries": queries,
    }


def _merge_channels(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """汇总本次所有查询实际覆盖到的渠道（去重后按 source 升序）。"""
    counter: dict[int, int] = {}
    for query in queries:
        for channel in query.get("channels") or []:
            source = int(channel["source"])
            counter[source] = counter.get(source, 0) + int(channel["count"])
    return [
        {
            "source": source,
            "platform": SOURCE_NAMES.get(source, f"渠道{source}"),
            "count": count,
        }
        for source, count in sorted(counter.items())
    ]


def _enrich_query(query: dict[str, Any], match: engine.MatchResult) -> dict[str, Any]:
    """为每条结果补充属性解析与单价换算（纸品/文具才有实际折算）。"""
    if not query.get("items"):
        return query
    query = dict(query)
    query["items"] = [engine.enrich_item(item, match) for item in query["items"]]
    return query
