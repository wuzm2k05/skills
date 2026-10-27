"""API v1 版本路由。"""

from fastapi import APIRouter

from . import consumable, detail, search

router = APIRouter()
router.include_router(search.router)
router.include_router(detail.router)
router.include_router(consumable.router)

__all__ = ["router"]
