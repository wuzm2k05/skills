"""搜索接口：映射命中、未命中日志、上游降级、参数校验。"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from app.config import settings
from app.services import category_engine as engine


async def test_root_reports_endpoints(api: httpx.AsyncClient) -> None:
    r = await api.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == settings.app_version
    assert any("/search" in e for e in body["endpoints"])
    assert body["sources"]["2"] == "京东"


async def test_health_reports_mapping_counts(api: httpx.AsyncClient) -> None:
    r = await api.get("/health")
    assert r.status_code == 200
    health = r.json()
    assert health["status"] == "ok"
    assert health["mappings"]["consumables"] > 0
    assert health["mappings"]["server_hardware"] > 0
    assert health["mappings"]["paper_spec"] > 0


class TestMatchedSearch:
    """映射命中：应并发带出主设备 + 该设备的各类耗材。"""

    async def test_returns_primary_and_consumable_queries(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/v1/search", params={"keyword": "惠普 M126a", "source": 0})
        assert r.status_code == 200
        body = r.json()

        assert body["matched"] is True
        assert body["matched_by"] == "consumables"
        assert body["match_key"] == "M126a"

        kinds = [q["kind"] for q in body["queries"]]
        assert kinds == ["primary", "consumable"]

        primary = next(q for q in body["queries"] if q["kind"] == "primary")
        assert primary["status"] == "OK"
        assert primary["count"] == 3
        assert primary["platform"] == "全部"

    async def test_consumable_query_carries_its_model_label(
        self, api: httpx.AsyncClient
    ) -> None:
        """每条耗材查询的 label 就是它自己的型号关键词，便于按型号分区渲染。"""
        r = await api.get("/api/v1/search", params={"keyword": "惠普 M126a"})
        consumable = next(q for q in r.json()["queries"] if q["kind"] == "consumable")

        assert consumable["keyword"] == "惠普 88A 碳粉盒"
        assert consumable["label"] == "惠普 88A 碳粉盒"
        # 「原装」「适用」后缀不再出现在查询词里——上游不按这两个词筛选
        assert "原装" not in consumable["keyword"]
        assert "适用" not in consumable["keyword"]

    async def test_items_carry_platform_and_price_fields(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/v1/search", params={"keyword": "惠普 M126a"})
        primary = next(q for q in r.json()["queries"] if q["kind"] == "primary")
        first = primary["items"][0]

        assert first["platform"] == "京东"
        assert first["source"] == 2
        assert first["actualPrice"] == 1049.0
        assert first["coupon_desc"].startswith("原价 ¥")

    async def test_supply_type_distinguishes_original_from_compatible(
        self, api: httpx.AsyncClient
    ) -> None:
        """同一批结果里原装与兼容并存，靠每行的 attributes 区分而非查询词。"""
        r = await api.get("/api/v1/search", params={"keyword": "惠普 M126a"})
        consumable = next(q for q in r.json()["queries"] if q["kind"] == "consumable")
        tiers = [i["attributes"]["supply_type"] for i in consumable["items"]]

        assert "原装" in tiers
        assert "第三方兼容" in tiers

    async def test_supply_tier_flags_recycled_toner_marked_original(
        self, api: httpx.AsyncClient
    ) -> None:
        """标称「原装」但带兼容信号的再生品：supply_type 照旧，supply_tier 降级。

        supply_type 是 answer-rules 依赖的契约，只能如实反映标题文本；真正给
        用户的判断走 supply_tier 旁路，避免把 ¥43.9 的兼容品当原装推荐。
        """
        r = await api.get("/api/v1/search", params={"keyword": "惠普 M126a"})
        consumable = next(q for q in r.json()["queries"] if q["kind"] == "consumable")
        by_price = {i["actualPrice"]: i["attributes"] for i in consumable["items"]}

        oem = by_price[1049.0]
        assert oem["supply_type"] == "原装"
        assert oem["supply_tier"] == "oem_likely"

        # 「惠普 88A 碳粉盒 原装 再生 得力388AXT」含「适用」类信号 → 兼容
        recycled = by_price[999.0]
        assert recycled["supply_tier"] == "compatible"

    async def test_hardware_items_annotate_spec_match(
        self, api: httpx.AsyncClient
    ) -> None:
        """服务器配件只标注规格相符度，不按规格过滤结果。"""
        r = await api.get("/api/v1/search", params={"keyword": "戴尔 PowerEdge R750"})
        query = next(q for q in r.json()["queries"] if q["kind"] == "accessory")

        assert query["items"], "配件查询不应因规格不同被清空"
        for item in query["items"]:
            spec = item["attributes"]["spec_match"]
            assert spec["verdict"] in {"match", "conflict", "unknown"}

    async def test_channels_covered_reflects_actual_data_not_request(
        self, api: httpx.AsyncClient
    ) -> None:
        """source=0 声称「全部」，覆盖范围必须由实际返回的数据推导。"""
        r = await api.get("/api/v1/search", params={"keyword": "惠普 M126a", "source": 0})
        body = r.json()

        covered = {c["source"] for c in body["channels_covered"]}
        assert covered == {1, 2, 3}
        assert all(c["count"] > 0 for c in body["channels_covered"])

    async def test_hardware_match_returns_accessory_keywords(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/v1/search", params={"keyword": "戴尔 PowerEdge R750"})
        body = r.json()

        assert body["matched"] is True
        assert body["matched_by"] == "server_hardware"
        assert body["category"] == "服务器"
        assert all(q["kind"] == "accessory" for q in body["queries"] if q["kind"] != "primary")
        # max_related_queries 默认 2，因此最多 1 条主查询 + 2 条配件查询
        assert len(body["queries"]) == 3

    async def test_accessory_keywords_put_specs_after_the_noun(
        self, api: httpx.AsyncClient
    ) -> None:
        """规格 token 必须排在类别词之后，且不得写死容量/频率。

        词序实测：同一批 8 台服务器 2 轮（sourceType=0 取小值），规格后置的
        「戴尔 R750 服务器内存 ECC RDIMM」21 条，把规格挪到类别词之前的
        「戴尔 R750 ECC RDIMM 服务器内存」只剩 1 条（sourceType=2 上为 0）。
        容量（32G…）与频率由用户需求决定，写死会砍掉召回。
        """
        r = await api.get("/api/v1/search", params={"keyword": "戴尔 PowerEdge R750"})
        keywords = [q["keyword"] for q in r.json()["queries"] if q["kind"] == "accessory"]

        memory = next(k for k in keywords if "内存" in k)
        assert memory == "戴尔 R750 服务器内存 ECC"
        assert "32G" not in memory and "64G" not in memory and "3200MHz" not in memory
        # 规格词一律不追加（形态/代际只做 spec_match 标注），故不含任何形态词
        assert "RDIMM" not in memory

    async def test_machine_grade_items_skip_unit_price(self, api: httpx.AsyncClient) -> None:
        """整机/耗材类不存在按包折算的需求，unit_price 恒为 null。"""
        r = await api.get("/api/v1/search", params={"keyword": "惠普 M126a"})
        primary = next(q for q in r.json()["queries"] if q["kind"] == "primary")
        assert primary["items"][0]["unit_price"] is None

    async def test_no_unmatched_log_written(self, api: httpx.AsyncClient) -> None:
        await api.get("/api/v1/search", params={"keyword": "惠普 M126a"})
        assert not settings.unmatched_log.exists()


class TestUnmatchedSearch:
    """未命中：记录日志、返回 matched=false 与宿主 AI 兜底提示。"""

    KEYWORD = "未知冷门设备型号123"

    async def test_returns_unmatched_flag_and_hint(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/v1/search", params={"keyword": self.KEYWORD})
        body = r.json()

        assert body["matched"] is False
        assert body["matched_by"] == "none"
        assert body["category"] == "未知"
        assert len(body["hint"]) > 10
        assert body["queries"][0]["label"] == "原始关键词直搜"
        assert body["queries"][0]["kind"] == "primary"

    async def test_log_line_matches_spec(self, api: httpx.AsyncClient) -> None:
        r = await api.get(
            "/api/v1/search",
            params={"keyword": self.KEYWORD},
            headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"},
        )
        body = r.json()

        assert body["client_ip"] == "203.0.113.7"
        assert body["log_line"].startswith("[")
        assert f"[UNMATCHED] Keyword: {self.KEYWORD} | IP: 203.0.113.7" in body["log_line"]

    async def test_log_written_to_disk_verbatim(self, api: httpx.AsyncClient) -> None:
        await api.get("/api/v1/search", params={"keyword": self.KEYWORD})

        assert settings.unmatched_log.exists()
        text = settings.unmatched_log.read_text(encoding="utf-8")
        # 关键词按原文记录，不做归一化，便于按用户真实说法补齐映射表
        assert f"[UNMATCHED] Keyword: {self.KEYWORD} | IP:" in text
        assert "source=0 page=1" in text

    async def test_overlong_keyword_truncated_in_log_and_response(
        self, api: httpx.AsyncClient
    ) -> None:
        """接口放行 200 字关键词，但落盘的单行必须更短，避免日志被单行撑大。"""
        long_keyword = "未" * 200
        r = await api.get("/api/v1/search", params={"keyword": long_keyword})
        assert r.status_code == 200

        line = r.json()["log_line"]
        assert "(截断)" in line
        assert len(line) < 400

        written = settings.unmatched_log.read_text(encoding="utf-8")
        assert "(截断)" in written
        assert max(len(ln) for ln in written.splitlines()) < 400

    async def test_appends_without_truncating(self, api: httpx.AsyncClient) -> None:
        for kw in ("未命中词A", "未命中词B", "未命中词C"):
            await api.get("/api/v1/search", params={"keyword": kw})

        lines = [ln for ln in settings.unmatched_log.read_text(encoding="utf-8").splitlines() if ln]
        assert len(lines) == 3
        assert "未命中词A" in lines[0]
        assert "未命中词C" in lines[2]

    async def test_untrusted_forwarded_for_is_ignored(
        self, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """关闭信任代理头后，日志只能记录 TCP 对端地址，避免 IP 被伪造。"""
        from app.api import deps

        monkeypatch.setattr(
            deps, "settings", replace(deps.settings, trust_forwarded_for=False)
        )
        r = await api.get(
            "/api/v1/search",
            params={"keyword": self.KEYWORD},
            headers={"x-forwarded-for": "1.2.3.4"},
        )
        assert r.json()["client_ip"] == "127.0.0.1"


class TestPaperUnitPrice:
    """纸品单价换算：能算才算，算不准就如实标注。"""

    async def test_unit_price_basis_returned(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/v1/search", params={"keyword": "A4 复印纸 70g"})
        body = r.json()

        assert body["matched"] is True
        assert body["matched_by"] == "paper_spec"
        assert body["unit_calculation"] == "calculate_per_pack_price"
        assert body["unit_price_basis"] == {
            "sheets_per_pack": 500,
            "packs_per_box": 5,
            "pieces_per_box": 2500,
        }

    async def test_box_and_single_pack_are_both_converted(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/v1/search", params={"keyword": "A4 复印纸 70g"})
        items = next(q for q in r.json()["queries"] if q["kind"] == "primary")["items"]
        by_count = {i["attributes"]["pack_count"]: i["unit_price"] for i in items}

        assert by_count[5]["basis"] == "box"
        assert by_count[5]["unit_price"] == 18.5  # 92.5 / 5 包
        assert by_count[1]["basis"] == "single_pack"
        assert by_count[1]["unit_price"] == 92.5


class TestUpstreamFailure:
    """上游 5xx：重试耗尽后降级，不影响其它查询与整体响应。"""

    async def test_retries_then_degrades_to_upstream_error(
        self, api: httpx.AsyncClient, install_upstream
    ) -> None:
        await install_upstream(search_fail_times=99)
        r = await api.get("/api/v1/search", params={"keyword": "惠普 M126a"})

        assert r.status_code == 200  # 单个查询失败不应让整个请求 500
        body = r.json()
        assert body["matched"] is True  # 映射仍然命中，只是拿不到商品
        primary = next(q for q in body["queries"] if q["kind"] == "primary")
        assert primary["status"] == "UPSTREAM_ERROR"
        assert primary["count"] == 0
        assert primary["error"]

    async def test_transient_failure_recovers_within_retries(
        self, api: httpx.AsyncClient, install_upstream
    ) -> None:
        # 上游抖动 2 次后恢复；未命中路径只发 1 条查询，重试次数可确定预测。
        # OFFICE_BUYER_UPSTREAM_RETRIES 默认 3，因此第 3 次尝试应当成功。
        await install_upstream(search_fail_times=2)
        r = await api.get("/api/v1/search", params={"keyword": "未命中的抖动测试词"})

        assert r.json()["queries"][0]["status"] == "OK"
        assert r.json()["queries"][0]["count"] == 3


class TestValidation:
    async def test_empty_keyword_rejected(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/v1/search", params={"keyword": ""})
        assert r.status_code == 422

    async def test_keyword_too_long_rejected(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/v1/search", params={"keyword": "x" * 201})
        assert r.status_code == 422

    async def test_page_out_of_range_rejected(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/v1/search", params={"keyword": "A4纸", "page": 0})
        assert r.status_code == 422


class TestOperationsEndpoints:
    async def test_mapping_endpoint_removed(self, api: httpx.AsyncClient) -> None:
        """mapping 是误导来源，已下线：端点必须 404，根清单也不得再列出。"""
        r = await api.get("/api/v1/consumable/mapping", params={"device": "M126a"})
        assert r.status_code == 404

        root = await api.get("/")
        assert not any("consumable/mapping" in e for e in root.json()["endpoints"])

    async def test_unmatched_stats_aggregates_top_keywords(
        self, api: httpx.AsyncClient
    ) -> None:
        for _ in range(2):
            await api.get("/api/v1/search", params={"keyword": "重复未命中词"})
        await api.get("/api/v1/search", params={"keyword": "另一个未命中词"})

        r = await api.get("/api/v1/consumable/unmatched", params={"limit": 50})
        stats = r.json()["stats"]

        assert stats["total"] == 3
        assert stats["unique"] == 2
        assert stats["top_keywords"][0] == {"keyword": "重复未命中词", "count": 2}

    async def test_reload_mappings(self, api: httpx.AsyncClient) -> None:
        r = await api.post("/api/v1/consumable/reload")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "OK"
        assert body["counts"]["consumables"] == len(engine.TABLES.consumables)
