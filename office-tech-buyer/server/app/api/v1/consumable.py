"""设备与耗材映射查询路由。

供宿主 AI 在 `matched: false` 时主动询问云端映射表，也供运维查看映射覆盖情况。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ...services import category_engine as engine

router = APIRouter(tags=["consumable"])


@router.get("/consumable/mapping", summary="设备与耗材/配件映射查询")
async def consumable_mapping(
    device: str = Query(..., min_length=1, max_length=200, description="设备型号或关键词"),
) -> dict[str, Any]:
    """按设备型号查询耗材/配件映射，并对未命中情况给出 AI 兜底提示。"""
    match = engine.build_match(device)
    if not match.matched:
        # 该接口是显式查询，重复写入会污染日志；仅在无匹配时记录一次
        engine.log_unmatched(device, client_ip="-", extra="endpoint=/consumable/mapping")
        return {
            "matched": False,
            "device": device,
            "normalized_keyword": match.normalized,
            "hint": (
                "映射表未收录该设备。请宿主 AI 推理其耗材/配件型号，"
                "再以「[型号] 原装」为关键词调用 /api/v1/search 二次查询。"
            ),
            "related": [],
        }

    return {
        "matched": True,
        "device": device,
        "normalized_keyword": match.normalized,
        "matched_by": match.matched_by,
        "match_key": match.match_key,
        "category": match.category,
        "device_name": match.device_name,
        "attributes": match.attributes,
        "unit_calculation": match.unit_calculation,
        "unit_price_basis": match.unit_price_basis,
        "notes": match.notes,
        "primary_keyword": match.primary_keyword,
        "related": [item.as_dict() for item in match.related],
    }


@router.get("/consumable/unmatched", summary="未命中关键词统计（运维用）")
async def unmatched(limit: int = Query(200, ge=1, le=5000)) -> dict[str, Any]:
    """查看未命中日志，用于评估映射表缺口并优先补齐高频机型。"""
    stats = engine.unmatched_stats()
    return {
        "stats": stats,
        "recent": engine.read_unmatched(limit=limit),
    }


@router.post("/consumable/reload", summary="热重载映射表（运维用）")
async def reload_mappings() -> dict[str, Any]:
    """修改 data/*.json 后无需重启服务即可生效。"""
    engine.TABLES.reload()
    return {
        "status": "OK",
        "counts": {
            "consumables": len(engine.TABLES.consumables),
            "server_hardware": len(engine.TABLES.server_hardware),
            "paper_spec": len(engine.TABLES.paper_spec),
        },
    }
