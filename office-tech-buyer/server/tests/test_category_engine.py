"""品类引擎：关键词归一化、映射匹配、属性解析、单价换算的保守性。

单价换算是本服务最容易出错的地方（电商标题的包装信息经常自相矛盾），
因此这里用大量真实标题做回归，重点验证「算不准时不硬算」。
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator

import pytest

from app.services import category_engine as engine


@contextmanager
def monkeypatch_settings(**overrides) -> Iterator[None]:
    """临时替换 ``category_engine`` 看到的配置。

    ``Settings`` 是 frozen dataclass，只能整对象替换；日志写入器是进程内单例，
    会按配置指纹自动重建，因此退出时无需手动回收。
    """
    original = engine.settings
    engine.settings = replace(original, **overrides)
    try:
        yield
    finally:
        engine.settings = original


@pytest.fixture(scope="module")
def paper_match() -> engine.MatchResult:
    """A4 70g 的匹配结果：500 张/包、5 包/箱、共 2500 张。"""
    match = engine.build_match("A4 复印纸 70g")
    assert match.matched
    return match


def convert(match: engine.MatchResult, title: str, price: float = 61.9) -> dict:
    """对给定标题做一次单价换算。"""
    return engine.enrich_item({"title": title, "actualPrice": price}, match)["unit_price"]


class TestNormalize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("  惠普  M126a  ", "惠普 M126a"),
            ("帮我查一下奔图P2200的价格", "奔图P2200"),
            ("Ａ４复印纸", "A4复印纸"),
            ("A4复印纸70g？", "A4复印纸70g"),
        ],
    )
    def test_normalization(self, raw: str, expected: str) -> None:
        assert engine.normalize_keyword(raw) == expected


class TestMatching:
    @pytest.mark.parametrize(
        ("keyword", "table", "category"),
        [
            ("惠普 M126a", "consumables", "多功能一体机"),
            ("惠普M126a打印机", "consumables", "多功能一体机"),
            ("奔图 P2200", "consumables", "激光打印机"),
            ("戴尔 PowerEdge R750", "server_hardware", "服务器"),
            ("R750", "server_hardware", "服务器"),
            ("联想 ThinkCentre M730q", "server_hardware", "电脑整机"),
            ("A4 复印纸 70g", "paper_spec", "复印纸"),
            ("A4纸80克整箱", "paper_spec", "复印纸"),
            ("收银纸 57x50", "paper_spec", "收银纸"),
            ("彩色复印纸 A4", "paper_spec", "彩色纸"),
        ],
    )
    def test_hits(self, keyword: str, table: str, category: str) -> None:
        match = engine.build_match(keyword)
        assert match.matched is True, keyword
        assert match.matched_by == table, keyword
        assert match.category == category, keyword

    @pytest.mark.parametrize(
        "keyword",
        ["未知冷门设备型号123", "随便什么东西", "xyz", "", "   "],
    )
    def test_misses(self, keyword: str) -> None:
        match = engine.build_match(keyword)
        assert match.matched is False
        assert match.matched_by == "none"

    def test_alias_matching_covers_same_series(self) -> None:
        """同系列机型共用耗材，别名表必须能命中。"""
        for keyword in ("L3153", "L3118", "L3251"):
            match = engine.build_match(keyword)
            assert match.matched, keyword
            assert match.match_key == "L3151", keyword

    def test_bare_model_expands_to_official_name(self) -> None:
        """只给型号时补全为官方全称，否则上游容易召回配件而非整机。"""
        assert engine.build_match("M126a").primary_keyword == "惠普 HP LaserJet Pro MFP M126a"
        assert engine.build_match("R750").primary_keyword == "戴尔 Dell PowerEdge R750 机架式服务器"

    def test_specific_keyword_is_kept_verbatim(self) -> None:
        """用户给的关键词已足够具体时原样使用，短查询召回更好。"""
        assert engine.build_match("惠普 M126a").primary_keyword == "惠普 M126a"

    def test_related_queries_by_table(self) -> None:
        consumable = engine.build_match("惠普 M126a")
        assert consumable.related
        assert all(r.kind == "consumable" for r in consumable.related)
        # label 就是该耗材的型号关键词，宿主 AI 靠它把「粉盒」「硒鼓」分开渲染
        assert all(r.label == r.keyword for r in consumable.related)

        hardware = engine.build_match("戴尔 PowerEdge R750")
        assert hardware.related and all(r.kind == "accessory" for r in hardware.related)

        paper = engine.build_match("A4 复印纸 70g")
        assert paper.related and all(r.kind == "variant" for r in paper.related)

    def test_drum_and_toner_are_separate_queries(self) -> None:
        """鼓粉分离的机型必须为粉盒与硒鼓各出一条查询。

        DCP-7180DN 的 model_code 是「TN-2325 / DR-2350」，旧表只搜了粉盒，
        硒鼓 DR-2350 从未进入比价；而实测两者差一个价位档（各 3 轮中位价：
        粉盒 ¥32.4/¥90.0/¥35.9，硒鼓 ¥82.1/¥92.5/¥87.5），漏掉的是更贵的那一半。
        """
        related = engine.build_match("兄弟 DCP-7180DN").related
        keywords = [r.keyword for r in related]

        assert "兄弟 TN-2325 粉盒" in keywords
        assert "兄弟 DR-2350 硒鼓" in keywords

    def test_consumable_keywords_drop_original_compatible_suffixes(self) -> None:
        """关键词不带「原装/适用」后缀——上游不按这两个词筛选。

        实测「惠普 88A 碳粉盒 原装」上游 52 条里 oem_likely 只有 2 条
        （¥529/¥1009），compatible 47 条（最低 ¥5.0、中位 ¥28.0），等于白占
        一条 max_related_queries 配额。供应类型改由每行结果的 attributes 判定。
        """
        for keyword in ("惠普 M126a", "兄弟 DCP-7180DN", "联想 M7605D"):
            for related in engine.build_match(keyword).related:
                assert "原装" not in related.keyword, related.keyword
                assert "适用" not in related.keyword, related.keyword

    def test_hardware_memory_keyword_appends_ecc_only_after_noun(self) -> None:
        """内存配件只追加 `ECC`，且一律追加在类别词之后。

        8 台服务器 2 轮实测（sourceType=0，取小值）：不追加后缀合计 260 条，
        只追加 `ECC` 246 条（含 ECC 的行从 89 升到 151），追加到 `DDR4 ECC` 190 条，
        再到 `DDR4 ECC RDIMM` 只有 70 条（「浪潮 NF5280M5 服务器内存 ECC」41→0、
        「联想 SR650 …」40→1）。代际与形态同理，只做 spec_match 标注。
        """
        related = engine.build_match("戴尔 PowerEdge R750").related
        memory = next(r for r in related if "内存" in r.keyword)

        assert memory.keyword == "戴尔 R750 服务器内存 ECC"
        # 形态词与代际不追加，交给 spec_match 逐行标注
        for token in ("RDIMM", "UDIMM", "LRDIMM", "DDR4", "DDR5"):
            assert token not in memory.keyword
        # 容量与频率由用户需求决定，不得写进关键词
        assert not re.search(r"\d+\s*G\b", memory.keyword)
        assert "3200MHz" not in memory.keyword

    def test_ecc_is_appended_to_servers_only(self) -> None:
        """台式机/笔记本内存不追加 ECC——它们不是必须 ECC，且召回基数本来就小。"""
        for keyword, memory_keyword in (
            ("联想 ThinkCentre M730q", "联想 M730q 内存"),
            ("联想 ThinkCentre M720t", "联想 M720t 内存"),
            ("戴尔 Precision 3660", "戴尔 Precision 3660 内存"),
        ):
            related = engine.build_match(keyword).related
            memory = next(r for r in related if "内存" in r.keyword)
            assert memory.keyword == memory_keyword, keyword

    def test_hardware_specs_are_not_appended_outside_memory(self) -> None:
        """盘位类的规格直接写在关键词里，不自动追加（自动追加会收窄召回）。"""
        related = engine.build_match("戴尔 PowerEdge R750").related
        caddy = next(r for r in related if "托架" in r.keyword)

        assert caddy.keyword == "戴尔 R750 硬盘托架 2.5寸"

    def test_short_alias_substring_needs_word_boundary(self) -> None:
        """「T27」是「deskjet2720」的子串，短别名不得在整机串里碰运气。

        曾因此把 ThinkVision T24 显示器及其配件当成 DeskJet 2720 的耗材返回，
        且 matched=true 让宿主 AI 不走兜底，直接报错耗材。
        """
        assert engine.build_match("惠普 DeskJet 2720").matched is False

        # 同一个别名带词边界时仍要命中，别把正常匹配一起修没了
        assert engine.build_match("T27 显示器").match_key == "ThinkVision T24"
        assert engine.build_match("T23 显示器").match_key == "ThinkVision T24"
        assert engine.build_match("E15 笔记本").match_key == "ThinkPad E14"
        assert engine.build_match("24/6 订书钉").match_key == "staples"

    def test_plan_puts_primary_first_and_honours_limit(self) -> None:
        match = engine.build_match("戴尔 PowerEdge R750")
        plan = match.plan(max_related=2)
        assert len(plan) == 3
        assert plan[0]["kind"] == "primary"
        assert plan[0]["keyword"] == "戴尔 PowerEdge R750"


class TestAttributes:
    def test_original_vs_compatible(self) -> None:
        assert engine.parse_attributes("惠普 88A 碳粉盒 原装")["supply_type"] == "原装"
        assert engine.parse_attributes("适用惠普 88A 碳粉盒 兼容")["supply_type"] == "第三方兼容"
        assert engine.parse_attributes("惠普 88A 碳粉盒")["supply_type"] == "未标注"
        # 标题同时含两种表述时必须显式提示，不能默认按原装处理
        conflicting = engine.parse_attributes("惠普 原装 88A 适用")
        assert conflicting["supply_type"] == "原装(标题含适用词)"

    def test_brand_and_model_extraction(self) -> None:
        attrs = engine.parse_attributes("兄弟 Brother TN-2325 粉盒 原装")
        assert attrs["brand"] == "兄弟"
        assert attrs["model_code"] == "TN-2325"

    def test_paper_specs(self) -> None:
        attrs = engine.parse_attributes("得力 A4 复印纸 70g 500张")
        assert attrs["paper_size"] == "A4"
        assert attrs["gram_weight"] == 70
        assert attrs["sheet_count"] == 500

    def test_implausible_gram_weight_ignored(self) -> None:
        # 500g 不是纸的克重，多半是别处的数字，不应被当作克重
        assert "gram_weight" not in engine.parse_attributes("A4纸 500g 包装")

    def test_collects_multiple_pack_options(self) -> None:
        attrs = engine.parse_attributes("得力A4纸70g整箱5包4包复印纸")
        assert {o["count"] for o in attrs["pack_options"]} == {5, 4}


class TestUnitPrice:
    """能算才算得准，算不准就如实标注存疑。"""

    def test_box_conversion(self, paper_match: engine.MatchResult) -> None:
        up = convert(paper_match, "A4复印纸70g 5包箱装 2500张", 92.5)
        assert up["basis"] == "box"
        assert up["unit"] == "包"
        assert up["unit_price"] == 18.5
        assert up["total_price"] == 92.5

    def test_sheet_count_conversion(self, paper_match: engine.MatchResult) -> None:
        up = convert(paper_match, "A4复印纸70g 2500张 整箱", 95.0)
        assert up["basis"] == "box"
        assert up["unit_price"] == 19.0

    def test_single_pack(self, paper_match: engine.MatchResult) -> None:
        up = convert(paper_match, "A4复印纸70g 500张 单包", 19.9)
        assert up["basis"] == "single_pack"
        assert up["unit_price"] == 19.9

    def test_multi_pack(self, paper_match: engine.MatchResult) -> None:
        up = convert(paper_match, "A4复印纸70g 20包", 380.0)
        assert up["basis"] == "multi_pack"
        assert up["unit_price"] == 19.0

    def test_partial_pack_falls_back_to_per_sheet(
        self, paper_match: engine.MatchResult
    ) -> None:
        up = convert(paper_match, "A4复印纸70g 100张 试用装", 8.0)
        assert up["basis"] == "per_sheet"
        assert up["unit"] == "张"
        assert up["unit_price"] == 0.08

    def test_no_spec_means_no_guess(self, paper_match: engine.MatchResult) -> None:
        up = convert(paper_match, "A4纸 70g", 25.0)
        assert up["basis"] == "basis_unknown"
        assert up["unit_price"] is None
        assert up["total_price"] == 25.0

    def test_conflicting_single_pack_and_box_is_ambiguous(
        self, paper_match: engine.MatchResult
    ) -> None:
        """「单包500张」与「整箱5包」同现时，两种解读并列，绝不二选一。"""
        up = convert(paper_match, "a4纸70g单包500张一包批发整箱5包装一箱", 61.9)

        assert up["basis"] == "ambiguous"
        assert up["unit_price"] is None
        assert {c["basis"] for c in up["candidates"]} == {"single_pack", "box"}
        assert {c["unit_price"] for c in up["candidates"]} == {61.9, 12.38}

    def test_conflicting_pack_counts_is_ambiguous(
        self, paper_match: engine.MatchResult
    ) -> None:
        up = convert(paper_match, "得力A4纸70g整箱5包4包复印纸", 61.9)
        assert up["basis"] == "ambiguous"
        assert up["unit_price"] is None
        assert {c["unit_price"] for c in up["candidates"]} == {12.38, 15.475}

    def test_conflicting_sheet_and_box_is_ambiguous(
        self, paper_match: engine.MatchResult
    ) -> None:
        up = convert(paper_match, "晨光a4打印纸70g 500页一包整箱2500页", 61.9)
        assert up["basis"] == "ambiguous"
        assert up["unit_price"] is None

    def test_non_multiple_sheet_count_is_ambiguous(
        self, paper_match: engine.MatchResult
    ) -> None:
        up = convert(paper_match, "A4复印纸70g 1200张 特惠装", 30.0)
        assert up["basis"] == "ambiguous"
        assert up["unit_price"] is None

    def test_invalid_price_short_circuits(self, paper_match: engine.MatchResult) -> None:
        assert convert(paper_match, "A4复印纸70g 500张 单包", 0)["basis"] == "no_price"

    def test_all_results_are_json_serializable(
        self, paper_match: engine.MatchResult
    ) -> None:
        up = convert(paper_match, "a4纸70g单包500张一包批发整箱5包装一箱")
        assert json.loads(json.dumps(up, ensure_ascii=False))["basis"] == "ambiguous"


class TestUnmatchedLog:
    def test_log_line_format(self, clean_unmatched_log: None) -> None:
        line = engine.log_unmatched("奔图 P2200X", "127.0.0.1", extra="source=0 page=1")
        assert "[UNMATCHED] Keyword: 奔图 P2200X | IP: 127.0.0.1 | source=0 page=1" in line
        assert line.startswith("[")

    def test_log_appends_and_stats_aggregate(self, clean_unmatched_log: None) -> None:
        engine.log_unmatched("型号X", "10.0.0.1")
        engine.log_unmatched("型号X", "10.0.0.2")
        engine.log_unmatched("型号Y", "10.0.0.3")

        stats = engine.unmatched_stats()
        assert stats["total"] == 3
        assert stats["unique"] == 2
        assert stats["top_keywords"][0] == {"keyword": "型号X", "count": 2}

    def test_keyword_with_pipe_does_not_break_stats(self, clean_unmatched_log: None) -> None:
        """关键词里的竖线不应让统计解析串行。"""
        engine.log_unmatched("奇怪的|关键词", "10.0.0.1")
        stats = engine.unmatched_stats()
        assert stats["total"] == 1
        assert stats["top_keywords"][0]["keyword"].startswith("奇怪的")

    def test_write_failure_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """日志目录不可写时只告警，不影响主流程。"""
        # 用一个已存在的普通文件充当日志目录，mkdir 必然失败
        blocked = tmp_path / "not-a-dir"
        blocked.write_text("x", encoding="utf-8")
        monkeypatch.setattr(engine, "settings", replace(engine.settings, log_dir=blocked))

        line = engine.log_unmatched("任意词", "127.0.0.1")
        assert "UNMATCHED" in line

    def test_stats_on_empty_log(self, clean_unmatched_log: None) -> None:
        stats = engine.unmatched_stats()
        assert stats["total"] == 0
        assert stats["top_keywords"] == []

    def test_overlong_keyword_is_truncated(self, clean_unmatched_log: None) -> None:
        """超长关键词必须截断：单行大小有界，否则一行就能把日志撑爆。"""
        line = engine.log_unmatched("长" * 5000, "127.0.0.1")
        assert len(line) < 400
        assert "(截断)" in line
        assert line.count("长") == engine.settings.unmatched_keyword_max_chars


class TestLogRotation:
    """未命中日志按大小滚动：单分片有上限、历史分片数量有上限。"""

    @staticmethod
    def _writer(tmp_path: Path, max_bytes: int, backups: int) -> engine.SizeRotatingLog:
        return engine.SizeRotatingLog(tmp_path / "u.log", max_bytes, backups)

    def test_rolls_over_when_exceeding_max_bytes(self, tmp_path: Path) -> None:
        log = self._writer(tmp_path, max_bytes=300, backups=2)
        for i in range(50):
            log.append(f"line-{i:03d}")

        assert log.path.exists()
        assert (tmp_path / "u.log.1").exists()
        # 当前分片必须回到阈值以内
        assert log.path.stat().st_size <= 300

    def test_keeps_only_backup_count_slices(self, tmp_path: Path) -> None:
        log = self._writer(tmp_path, max_bytes=200, backups=2)
        for i in range(200):
            log.append(f"line-{i:03d}")

        assert (tmp_path / "u.log.1").exists()
        assert (tmp_path / "u.log.2").exists()
        # 超出保留数量的最旧分片必须被丢弃，磁盘占用才有上界
        assert not (tmp_path / "u.log.3").exists()

    def test_no_backup_count_truncates_in_place(self, tmp_path: Path) -> None:
        """backup_count=0 表示不留历史：滚动即丢弃旧内容。"""
        log = self._writer(tmp_path, max_bytes=100, backups=0)
        for i in range(30):
            log.append(f"line-{i:03d}")

        assert not (tmp_path / "u.log.1").exists()
        assert log.path.stat().st_size <= 100

    def test_zero_max_bytes_disables_rotation(self, tmp_path: Path) -> None:
        """max_bytes=0 关闭滚动，只保留一个持续增长的文件。"""
        log = self._writer(tmp_path, max_bytes=0, backups=2)
        for i in range(50):
            log.append(f"line-{i:03d}")

        assert not (tmp_path / "u.log.1").exists()
        assert log.path.stat().st_size > 200

    def test_read_returns_oldest_to_newest_across_slices(self, tmp_path: Path) -> None:
        """跨分片读取必须按时间顺序拼接，否则取尾部拿到的不是最近记录。

        每行 10 字节、分片上限 200 字节：写到第 200 字节仍不滚动，下一行才触发，
        因此每个分片恰容纳 20 行；3 个备份共 4 个分片，容量 80 行。写入 120 行
        必然丢掉最旧的 40 行。
        """
        log = self._writer(tmp_path, max_bytes=200, backups=3)
        for i in range(120):
            log.append(f"line-{i:03d}")

        lines = log.read_lines()
        assert len(lines) == 80  # 最旧的记录已被丢弃
        assert lines[0] == "line-040"
        assert lines == sorted(lines)  # 字典序即写入顺序
        assert lines[-1] == "line-119"
        # 取尾部 N 条应恰为最近 N 条
        assert log.read_lines(limit=3) == ["line-117", "line-118", "line-119"]

    def test_lines_are_never_split(self, tmp_path: Path) -> None:
        """滚动必须按整行发生——不能出现读了半行的情况。"""
        log = self._writer(tmp_path, max_bytes=200, backups=3)
        for i in range(120):
            log.append(f"line-{i:03d}")

        for line in log.read_lines():
            assert line.startswith("line-")
            assert len(line) == len("line-000")

    def test_stats_include_rotated_slices(self, clean_unmatched_log: None) -> None:
        """补齐映射表要看全量历史，统计口径不能只算当前分片。"""
        with monkeypatch_settings(log_max_bytes=256, log_backup_count=2):
            for i in range(60):
                engine.log_unmatched(f"未命中机型{i:02d}", "10.0.0.1")

            rotated = engine.settings.unmatched_log.with_name(
                f"{engine.settings.unmatched_log.name}.1"
            )
            assert rotated.exists()

            stats = engine.unmatched_stats()
            assert stats["total"] > 1
            assert stats["log_files"]  # 至少有一个历史分片被统计进来
            # 最近写入的关键词必须在统计范围内
            assert any(
                item["keyword"] == "未命中机型59" for item in stats["top_keywords"]
            )

    def test_read_missing_log_returns_empty(self, tmp_path: Path) -> None:
        log = self._writer(tmp_path, max_bytes=100, backups=2)
        assert log.read_lines() == []

    def test_append_creates_missing_directory(self, tmp_path: Path) -> None:
        log = self._writer(tmp_path / "nested" / "deep", max_bytes=100, backups=2)
        log.append("hello")
        assert log.path.exists()
        assert log.read_lines() == ["hello"]
