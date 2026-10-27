"""详情接口：购买链接、淘口令、专票、质保，以及未授权渠道的降级提示。"""

from __future__ import annotations

import httpx
import pytest

from app.services.maishou88 import parse_invoice, parse_warranty


class TestDetailOk:
    async def test_returns_buy_url_and_coupon_code(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/v1/detail", params={"source": 2, "id": "210001"})
        assert r.status_code == 200

        body = r.json()
        assert body["status"] == "OK"
        assert body["platform"] == "京东"
        assert body["warnings"] == []

        data = body["data"]
        assert data["buy_url"] == "https://item.example.com/210001"
        assert data["coupon_code"] == "￥ABC210001￥"
        assert data["goodsId"] == "210001"
        assert data["source"] == 2

    async def test_parses_vat_invoice_and_warranty(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/v1/detail", params={"source": 2, "id": "210001"})
        data = r.json()["data"]

        assert data["invoice"]["supported"] is True
        assert data["invoice"]["evidence"] == "增值税专用发票"
        assert data["warranty"]["kind"] == "全国联保"
        assert data["warranty"]["term"] == "1年"

    async def test_drops_raw_payloads_but_keeps_field_summary(
        self, api: httpx.AsyncClient
    ) -> None:
        """原始结构体积大且客户端用不到，只回字段名清单。"""
        r = await api.get("/api/v1/detail", params={"source": 2, "id": "210001"})
        data = r.json()["data"]

        assert "raw_detail" not in data
        assert "raw_target" not in data
        assert "title" in data["raw_summary"]["detail_fields"]
        assert "appUrl" in data["raw_summary"]["target_fields"]


class TestDetailUnauthorizedChannel:
    async def test_warns_when_target_url_missing(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/v1/detail", params={"source": 3, "id": "310001"})
        body = r.json()

        assert body["status"] == "OK"
        assert body["data"]["buy_url"] == ""
        assert body["data"]["coupon_code"] == ""
        assert any("未授权" in w for w in body["warnings"])


class TestDetailValidation:
    async def test_missing_id_rejected(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/v1/detail", params={"source": 2})
        assert r.status_code == 422

    async def test_missing_source_rejected(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/v1/detail", params={"id": "210001"})
        assert r.status_code == 422


class TestInvoiceParsing:
    """专票判定必须保守：证据不足时返回 None，不默认按支持处理。"""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("支持开具增值税专用发票", True),
            ("本店可开13%专票", True),
            ("仅支持普通发票", False),
            ("不开发票", False),
            ("商品标题里出现了发票二字", None),
            ("完全没有相关字样", None),
        ],
    )
    def test_supported_flags(self, text: str, expected: bool | None) -> None:
        assert parse_invoice(text)["supported"] is expected

    def test_negative_signal_beats_positive_word(self) -> None:
        # 「不开专票」里含「专票」，必须判为不支持
        assert parse_invoice("本店不开专票，请知悉")["supported"] is False


class TestWarrantyParsing:
    @pytest.mark.parametrize(
        ("text", "kind", "term"),
        [
            ("全国联保 3年", "全国联保", "3年"),
            ("整机质保：2年", "整机质保", "2年"),
            ("提供上门服务", "上门服务", None),
            ("保修12个月", "保修", "12个月"),
            ("无相关说明", None, None),
        ],
    )
    def test_kind_and_term(self, text: str, kind: str | None, term: str | None) -> None:
        result = parse_warranty(text)
        assert result["kind"] == kind
        assert result["term"] == term

    def test_unlabeled_returns_none_confidence(self) -> None:
        assert parse_warranty("")["confidence"] == "none"
