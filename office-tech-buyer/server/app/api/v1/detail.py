"""详情路由：解析真实购买链接、淘口令、专票与质保信息。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from ...config import SOURCE_NAMES
from ...services.maishou88 import UpstreamError, maishou
from ..deps import client_ip

router = APIRouter(tags=["detail"])


@router.get("/detail", summary="商品详情（购买链接/专票/质保）")
async def detail(
    request: Request,
    source: int = Query(..., ge=0, le=99, description="渠道来源，与 search 结果中的 source 一致"),
    id: str = Query(..., min_length=1, max_length=64, description="商品 ID (goodsId)"),
) -> dict[str, Any]:
    """返回该 goodsId 的真实购买链接、淘口令、专票开具能力与质保说明。

    购买链接与淘口令来自上游转链接口；若渠道未授权绑定，``buy_url`` 会为空并在
    ``warnings`` 中说明，客户端应如实告知用户「该平台接口未授权，无法提取直达链接」。
    """
    ip = client_ip(request)
    warnings: list[str] = []

    detail_data: dict[str, Any] = {}
    target_data: dict[str, Any] = {}

    try:
        detail_data = await maishou.detail(id, source)
    except UpstreamError as exc:
        warnings.append(f"详情接口调用失败：{exc.message}")

    try:
        target_data = await maishou.target_url(id, source)
    except UpstreamError as exc:
        warnings.append(f"转链接口调用失败：{exc.message}")

    if not detail_data and not target_data:
        return {
            "status": "UPSTREAM_ERROR",
            "goodsId": str(id),
            "source": int(source),
            "platform": SOURCE_NAMES.get(int(source), f"渠道{source}"),
            "client_ip": ip,
            "warnings": warnings or ["上游未返回任何数据"],
            "data": None,
        }

    cleaned = maishou.clean_detail(str(id), int(source), detail_data, target_data)
    if not cleaned["buy_url"] and not cleaned["coupon_code"]:
        warnings.append(
            f"【{cleaned['platform']}】接口可能未授权绑定，或该商品已下架/暂不支持转链，"
            "暂时无法提取直达购买链接，请在回复中如实说明并退守展示已授权的平台。"
        )

    # 详情体积可能很大，默认不返回原始结构，避免客户端解析负担
    raw_detail = cleaned.pop("raw_detail", {})
    raw_target = cleaned.pop("raw_target", {})
    cleaned["raw_summary"] = {
        "detail_fields": sorted(raw_detail.keys()),
        "target_fields": sorted(raw_target.keys()),
    }

    return {
        "status": "OK",
        "goodsId": str(id),
        "source": int(source),
        "platform": cleaned["platform"],
        "client_ip": ip,
        # 上游未提供开票资质字段：实测扫描 detail 全部 69~70 个字段（京东/拼多多/天猫）
        # 均无「发票/专票/质保/保修/联保/三包」信号，故 invoice/warranty 恒定落在
        # 「未标注」分支。这里给出显式开关，让宿主 AI 能整列抑制而不是逐行写「未标注」。
        "invoice_data_available": False,
        "warnings": warnings,
        "data": cleaned,
    }
