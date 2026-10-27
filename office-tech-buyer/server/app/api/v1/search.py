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
        "items": items,
    }


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
      由客户端宿主 AI 按 ``references/answer-rules.md`` 推导耗材型号后二次调用。
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
        return {
            "matched": False,
            "keyword": keyword,
            "normalized_keyword": match.normalized,
            "category": match.category,
            "matched_by": match.matched_by,
            "log_line": line,
            "client_ip": ip,
            "hint": (
                "云端静态映射表未命中该设备。请宿主 AI 依据自身知识推理其标准耗材/配件型号，"
                "再以「[型号] 原装」为关键词二次调用本接口，并将结果与本次主设备结果合并呈现。"
            ),
            "queries": [primary],
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
        "queries": queries,
    }


def _enrich_query(query: dict[str, Any], match: engine.MatchResult) -> dict[str, Any]:
    """为每条结果补充属性解析与单价换算（纸品/文具才有实际折算）。"""
    if not query.get("items"):
        return query
    query = dict(query)
    query["items"] = [engine.enrich_item(item, match) for item in query["items"]]
    return query
