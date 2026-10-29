"""耗材运维路由：未命中关键词统计与映射表热重载。

比价流程不经过本模块 —— 耗材由 `/search` 基于设备型号自动并发带出，或由宿主 AI
按「设备型号 + 耗材类别词」再搜一轮，两种方式都不需要先问映射表。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ...services import category_engine as engine

router = APIRouter(tags=["consumable"])


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
