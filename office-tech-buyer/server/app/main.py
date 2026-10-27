"""FastAPI 主入口。

启动方式::

    # 开发
    uv run uvicorn app.main:app --host 0.0.0.0 --port 8446 --reload

    # 或直接运行
    uv run python -m app.main

端口默认 8446，由 ``PORT`` 环境变量覆盖。

云端部署时建议置于 Nginx / SLB 之后，并开启 ``OFFICE_BUYER_TRUST_FORWARDED_FOR=1``，
以便未命中日志记录到真实客户端 IP。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.v1 import router as v1_router
from .config import SOURCE_NAMES, settings
from .services import category_engine as engine
from .services.maishou88 import UpstreamError, maishou

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("office-tech-buyer")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时预热映射表与日志目录，关闭时释放上游连接。"""
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine.TABLES.load(force=True)
    logger.info(
        "映射表已加载：打印机耗材 %d 条 / 服务器配件 %d 条 / 纸品文具 %d 条",
        len(engine.TABLES.consumables),
        len(engine.TABLES.server_hardware),
        len(engine.TABLES.paper_spec),
    )
    logger.info("未命中日志：%s", settings.unmatched_log)
    await maishou.startup()
    try:
        yield
    finally:
        await maishou.shutdown()
        logger.info("上游连接已关闭")


app = FastAPI(
    title="办公设备易采买 云端服务",
    description=(
        "为 WorkBuddy「办公设备易采买」技能提供后端能力：全品类比价搜索、"
        "设备-耗材/配件映射、专票与质保解析，以及未命中关键词日志记录。"
    ),
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(v1_router, prefix=settings.api_prefix)


# --------------------------------------------------------------------------- #
# 全局异常处理：任何异常都以统一 JSON 结构返回，客户端无需处理 HTML 错误页
# --------------------------------------------------------------------------- #
@app.exception_handler(UpstreamError)
async def upstream_error_handler(request: Request, exc: UpstreamError) -> JSONResponse:
    logger.warning("上游异常 %s: %s", request.url.path, exc.message)
    return JSONResponse(
        status_code=502,
        content={
            "status": "UPSTREAM_ERROR",
            "message": exc.message,
            "upstream_status": exc.status,
            "detail": exc.payload,
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常 %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"status": "INTERNAL_ERROR", "message": str(exc)},
    )


# --------------------------------------------------------------------------- #
# 基础端点
# --------------------------------------------------------------------------- #
@app.get("/", summary="服务信息")
async def root() -> dict[str, Any]:
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "endpoints": [
            f"{settings.api_prefix}/search?keyword=惠普 M126a&source=0&page=1",
            f"{settings.api_prefix}/detail?source=2&id=123456789",
            f"{settings.api_prefix}/consumable/mapping?device=M126a",
            f"{settings.api_prefix}/consumable/unmatched",
            f"{settings.api_prefix}/consumable/reload",
        ],
        "sources": SOURCE_NAMES,
    }


@app.get("/health", summary="健康检查")
async def health() -> dict[str, Any]:
    engine.TABLES.load()
    log = engine.unmatched_log()
    slices = [str(p) for p in log.existing_paths()]
    return {
        "status": "ok",
        "version": settings.app_version,
        "mappings": {
            "consumables": len(engine.TABLES.consumables),
            "server_hardware": len(engine.TABLES.server_hardware),
            "paper_spec": len(engine.TABLES.paper_spec),
        },
        "unmatched_log": str(settings.unmatched_log),
        "unmatched_log_exists": settings.unmatched_log.exists(),
        "unmatched_log_rotation": {
            "max_bytes": settings.log_max_bytes,
            "backup_count": settings.log_backup_count,
            "slices": slices,
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )
