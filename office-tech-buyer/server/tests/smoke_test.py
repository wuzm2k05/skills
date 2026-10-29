"""端到端冒烟测试：启动真实 FastAPI 应用，并用 MockTransport 替换上游。

运行：``python -I tests/smoke_test.py``（在 server/ 目录下执行）

覆盖：
1. 映射命中场景（惠普 M126a）→ matched=true 且带出原装/兼容耗材查询
2. 未命中场景（未知型号）→ matched=false 且写入 logs/unmatched.log
3. 纸品单价换算（A4 复印纸 70g）
4. 详情接口的购买链接 / 专票 / 质保解析
5. 未授权渠道（buy_url 为空）时的 warnings 提示
6. 上游 5xx 重试耗尽后的 UPSTREAM_ERROR 降级
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import category_engine as engine  # noqa: E402
from app.services.maishou88 import maishou  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


# --------------------------------------------------------------------------- #
# 上游打桩
# --------------------------------------------------------------------------- #
def _search_payload(keyword: str, source: int, page: int) -> dict:
    """按关键词造稳定的假商品数据。

    与 tests/conftest.py 的 ``search_payload`` 保持同构：同一关键词下原装与兼容
    行混在一起，用于验证「供应类型由每行的 attributes 判定，不由查询词判定」。
    """
    rows = []
    platform_titles = {
        1: "淘宝/天猫",
        2: "京东",
        3: "拼多多",
    }
    for idx, source_type in enumerate((2, 1, 3), start=1):
        if "复印纸" in keyword:
            spec = {1: "5包箱装 2500张", 2: "1包 500张 单包", 3: "20包 整箱批发"}[idx]
            title = f"{keyword} {spec}"
        elif "88A" in keyword:
            title = {
                1: "惠普(HP) CC388A 88A 原装碳粉盒 硒鼓",
                2: "适用惠普 88A 碳粉盒 兼容 M126a M1136 国产",
                3: "惠普 88A 碳粉盒 原装 再生 得力388AXT",
            }[idx]
        else:
            title = f"{keyword} 商用办公款 {platform_titles.get(source_type, '')}"
        rows.append(
            {
                "goodsId": f"{source_type}{page}{idx}0001",
                "sourceType": source_type,
                "title": title,
                "shopName": "京东自营官方旗舰店" if source_type == 2 else "品牌专营店",
                "originalPrice": 1299 - idx * 10,
                "actualPrice": 92.5 if "复印纸" in keyword else 1099 - idx * 50,
                "couponPrice": None,
                "commission": 12.5,
                "monthSales": 3000 + idx,
                "picUrl": "https://example.com/p.jpg",
            }
        )
    return {"code": 200, "message": "ok", "data": rows}


def _detail_payload(goods_id: str, source: int) -> dict:
    return {
        "code": 200,
        "data": {
            # 刻意放一个与请求参数不同的 goodsId：上游会把天猫/京东的 id 重映射，
            # clean_detail 必须回填调用方传入的那个。
            "goodsId": "9999999999",
            "title": "惠普 M126a 多功能一体机 支持增值税专用发票 全国联保 1年上门服务",
            "shopName": "京东自营官方旗舰店",
            "originalPrice": 1299,
            "actualPrice": 1099,
            "monthSales": 5000,
            "picUrl": "https://example.com/p.jpg",
        },
    }


def _target_payload(goods_id: str, source: int) -> dict:
    if int(source) == 3:  # 拼多多：模拟未授权，拿不到转链
        return {"code": 200, "data": {}}
    return {
        "code": 200,
        "data": {"appUrl": f"https://item.example.com/{goods_id}", "kl": f"￥ABC{goods_id}￥"},
    }


def make_handler(fail_search_times: int = 0):
    state = {"search_fails": fail_search_times}

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "homepage/searchList" in url:
            if state["search_fails"] > 0:
                state["search_fails"] -= 1
                return httpx.Response(503, text="upstream busy")
            body = request.content.decode("utf-8")
            params = dict(httpx.QueryParams(body))
            return httpx.Response(
                200,
                json=_search_payload(
                    params.get("keyword", ""),
                    int(params.get("sourceType", 0)),
                    int(params.get("page", 1)),
                ),
                headers={"content-type": "application/json"},
            )
        if "goods/detail" in url:
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=_detail_payload(payload["goodsId"], payload["sourceType"]))
        if "share/getTargetUrl" in url:
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200, json=_target_payload(payload["goodsId"], payload["sourceType"])
            )
        return httpx.Response(404, text="not found")

    return handler


async def install_mock(fail_search_times: int = 0) -> None:
    """把上游 httpx 客户端替换为 MockTransport。"""
    await maishou.startup()
    await maishou.shutdown()
    maishou._client = httpx.AsyncClient(  # noqa: SLF001  (测试专用注入)
        timeout=httpx.Timeout(5.0),
        transport=httpx.MockTransport(make_handler(fail_search_times)),
    )


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
async def run() -> int:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.unmatched_log
    for stale in settings.log_dir.glob(f"{log_path.name}*"):
        stale.unlink()

    await install_mock()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        print("\n[1] 基础端点")
        r = await api.get("/")
        check("GET / 返回 200", r.status_code == 200, str(r.status_code))
        r = await api.get("/health")
        health = r.json()
        check(
            "GET /health 报告映射表条数",
            r.status_code == 200 and health["mappings"]["consumables"] > 0,
            json.dumps(health.get("mappings", {})),
        )

        print("\n[2] 映射命中：惠普 M126a")
        r = await api.get("/api/v1/search", params={"keyword": "惠普 M126a", "source": 0})
        body = r.json()
        check("HTTP 200", r.status_code == 200, str(r.status_code))
        check("matched=true", body.get("matched") is True, json.dumps(body)[:200])
        check("matched_by=consumables", body.get("matched_by") == "consumables")
        kinds = [q["kind"] for q in body.get("queries", [])]
        check("包含 primary 查询", "primary" in kinds, str(kinds))
        check("包含 consumable 耗材查询", "consumable" in kinds, str(kinds))
        check(
            "耗材关键词不带「原装/适用」后缀",
            all(
                "原装" not in q["keyword"] and "适用" not in q["keyword"]
                for q in body["queries"]
                if q["kind"] == "consumable"
            ),
            str([q["keyword"] for q in body["queries"] if q["kind"] == "consumable"]),
        )
        primary = next(q for q in body["queries"] if q["kind"] == "primary")
        check("主查询有结果", primary["count"] == 3, str(primary["count"]))
        check("主查询 status=OK", primary["status"] == "OK", primary["status"])
        first = primary["items"][0]
        check("商品含 platform 字段", first["platform"] == "京东", first["platform"])
        check("商品含 attributes.supply_type", "supply_type" in first["attributes"])
        check("商品含 coupon_desc 字段", "coupon_desc" in first)
        check("整机/耗材类不做单价折算", first["unit_price"] is None, str(first["unit_price"]))
        consumable_q = next(q for q in body["queries"] if q["kind"] == "consumable")
        tiers = [i["attributes"]["supply_type"] for i in consumable_q["items"]]
        check(
            "同批耗材结果里原装与兼容并存，按行 attributes 区分",
            "原装" in tiers and "第三方兼容" in tiers,
            str(tiers),
        )
        oem = [
            i["attributes"]
            for i in consumable_q["items"]
            if i["attributes"].get("supply_tier") == "oem_likely"
        ]
        check("带原厂前缀的原装款标注 supply_tier=oem_likely", len(oem) == 1, str(len(oem)))
        check(
            "标称原装的再生品 supply_tier 降级为 compatible",
            any(
                i["attributes"].get("supply_tier") == "compatible"
                for i in consumable_q["items"]
            ),
        )
        check(
            "channels_covered 由实际数据推导（source=0 实测只有 1/2/3）",
            {c["source"] for c in body["channels_covered"]} == {1, 2, 3},
            json.dumps(body["channels_covered"], ensure_ascii=False),
        )
        check("未命中日志未被写入", not log_path.exists())

        print("\n[3] 未命中：未知冷门设备型号123")
        r = await api.get(
            "/api/v1/search",
            params={"keyword": "未知冷门设备型号123"},
            headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"},
        )
        body = r.json()
        check("matched=false", body.get("matched") is False)
        check("返回 hint 兜底提示", "hint" in body and len(body["hint"]) > 10)
        check("记录客户端真实 IP", body.get("client_ip") == "203.0.113.7", str(body.get("client_ip")))
        check("返回 log_line", body.get("log_line", "").startswith("[") and "UNMATCHED" in body["log_line"])
        check("未命中时仍返回原始关键词查询", body["queries"][0]["label"] == "原始关键词直搜")
        check("未命中日志已落盘", log_path.exists())
        log_text = log_path.read_text(encoding="utf-8")
        check("日志含关键词", "未知冷门设备型号123" in log_text, log_text[:200])
        check("日志含 IP", "203.0.113.7" in log_text, log_text[:200])
        check(
            "日志格式符合规范",
            "[UNMATCHED] Keyword: 未知冷门设备型号123 | IP: 203.0.113.7" in log_text,
            log_text[:300],
        )

        print("\n[4] 纸品单价换算：A4 复印纸 70g")
        r = await api.get("/api/v1/search", params={"keyword": "A4 复印纸 70g"})
        body = r.json()
        check("matched=true", body.get("matched") is True)
        check(
            "unit_calculation=calculate_per_pack_price",
            body.get("unit_calculation") == "calculate_per_pack_price",
        )
        items = next(q for q in body["queries"] if q["kind"] == "primary")["items"]
        by_count = {i["attributes"].get("pack_count"): i["unit_price"] for i in items}
        check(
            "整箱条目折算为 ¥18.5/包",
            by_count.get(5, {}).get("unit_price") == 18.5,
            json.dumps(by_count.get(5), ensure_ascii=False),
        )
        check(
            "整箱条目 basis=box",
            by_count.get(5, {}).get("basis") == "box",
            str(by_count.get(5, {}).get("basis")),
        )
        check(
            "单包条目 basis=single_pack",
            by_count.get(1, {}).get("basis") == "single_pack",
            str(by_count.get(1, {}).get("basis")),
        )
        check(
            "单价换算不臆造：未标注规格时 basis=basis_unknown 且 unit_price 为空",
            all(
                i["unit_price"]["basis"] != "basis_unknown" or i["unit_price"]["unit_price"] is None
                for i in items
            ),
        )
        check(
            "每行都带 unit_calculation 与 unit_price_basis",
            body.get("unit_price_basis", {}).get("pieces_per_box") == 2500,
            json.dumps(body.get("unit_price_basis")),
        )

        print("\n[4b] 单价换算的保守性（直接调用引擎，覆盖矛盾标题）")
        paper_match = engine.build_match("A4 复印纸 70g")

        def convert(title: str, price: float = 61.9) -> dict:
            return engine.enrich_item({"title": title, "actualPrice": price}, paper_match)[
                "unit_price"
            ]

        # 同一标题里「单包 500 张」与「整箱 5 包」并存 → 必须判为歧义，不二选一
        conflict = convert("a4纸70g单包500张一包批发整箱5包装一箱")
        check("矛盾标题判为 ambiguous", conflict["basis"] == "ambiguous", conflict["basis"])
        check("未给出唯一单价", conflict["unit_price"] is None, str(conflict["unit_price"]))
        check(
            "列出两种解读候选",
            {c["basis"] for c in conflict["candidates"]} == {"single_pack", "box"},
            json.dumps(conflict["candidates"], ensure_ascii=False),
        )
        check(
            "候选单价分别为 ¥61.9（单包）与 ¥12.38（整箱）",
            {c["unit_price"] for c in conflict["candidates"]} == {61.9, 12.38},
            str([c["unit_price"] for c in conflict["candidates"]]),
        )

        two_counts = convert("得力A4纸70g整箱5包4包复印纸")
        check("多个冲突件数判为 ambiguous", two_counts["basis"] == "ambiguous", two_counts["basis"])

        check(
            "无规格信息时 basis_unknown",
            convert("A4纸 70g")["basis"] == "basis_unknown",
            convert("A4纸 70g")["basis"],
        )
        check(
            "整箱 2500 张折合 5 包 = ¥12.38/包",
            convert("A4复印纸70g 2500张 整箱")["unit_price"] == 12.38,
            str(convert("A4复印纸70g 2500张 整箱")["unit_price"]),
        )
        check(
            "不足一包按张折算",
            convert("A4复印纸70g 100张 试用装")["basis"] == "per_sheet",
            convert("A4复印纸70g 100张 试用装")["basis"],
        )
        check(
            "单包 500 张即单包价",
            convert("A4复印纸70g 500张 单包")["basis"] == "single_pack",
            convert("A4复印纸70g 500张 单包")["basis"],
        )

        print("\n[5] 详情接口：链接 / 专票 / 质保")
        r = await api.get("/api/v1/detail", params={"source": 2, "id": "210001"})
        body = r.json()
        check("status=OK", body.get("status") == "OK", str(body.get("status")))
        data = body["data"]
        check("解析出购买链接", data["buy_url"] == "https://item.example.com/210001", data["buy_url"])
        check("解析出淘口令", data["coupon_code"].startswith("￥"), data["coupon_code"])
        check("识别支持专票", data["invoice"]["supported"] is True, json.dumps(data["invoice"]))
        check("识别质保 1年", data["warranty"]["term"] == "1年", json.dumps(data["warranty"], ensure_ascii=False))
        check("warnings 为空", body["warnings"] == [], str(body["warnings"]))

        print("\n[6] 未授权渠道：拼多多拿不到转链")
        r = await api.get("/api/v1/detail", params={"source": 3, "id": "310001"})
        body = r.json()
        check("buy_url 为空", body["data"]["buy_url"] == "")
        check("warnings 提示未授权", any("未授权" in w for w in body["warnings"]), str(body["warnings"]))
        check("warnings 带上渠道名", any("拼多多" in w for w in body["warnings"]), str(body["warnings"]))
        check(
            "warnings 区分「下架/不支持转链」等其它可能原因",
            any("下架" in w for w in body["warnings"]),
            str(body["warnings"]),
        )

        print("\n[6b] 详情字段完整性")
        r = await api.get("/api/v1/detail", params={"source": 2, "id": "210001"})
        body = r.json()
        check(
            "返回调用方传入的 goodsId（上游会重映射，不得回显上游值）",
            body["goodsId"] == "210001" and body["data"]["goodsId"] == "210001",
            f'{body["goodsId"]} / {body["data"]["goodsId"]}',
        )
        check(
            "显式声明上游未提供开票资质字段",
            body["invoice_data_available"] is False,
            str(body.get("invoice_data_available")),
        )

        print("\n[6c] 服务器配件关键词：只追加 ECC、不写死容量/频率")
        r = await api.get("/api/v1/search", params={"keyword": "戴尔 PowerEdge R750"})
        body = r.json()
        acc = [q for q in body["queries"] if q["kind"] == "accessory"]
        check("命中 server_hardware", body["matched_by"] == "server_hardware", body["matched_by"])
        memory = next((q["keyword"] for q in acc if "内存" in q["keyword"]), "")
        check(
            "内存关键词规格后置、只追加 ECC",
            memory == "戴尔 R750 服务器内存 ECC",
            memory,
        )
        check(
            "内存关键词不含形态词与代际（只做 spec_match 标注）",
            not any(t in memory for t in ("RDIMM", "UDIMM", "LRDIMM", "DDR4", "DDR5")),
            memory,
        )
        check(
            "内存关键词不含容量与频率（由用户需求决定）",
            "32G" not in memory and "3200MHz" not in memory,
            memory,
        )
        check(
            "配件结果标注 spec_match 且未被过滤",
            all(
                i["attributes"]["spec_match"]["verdict"] in {"match", "conflict", "unknown"}
                for q in acc
                for i in q["items"]
            )
            and all(q["items"] for q in acc),
            str([(q["keyword"], q["count"]) for q in acc]),
        )

        print("\n[7] mapping 接口已下线（回归保护）")
        r = await api.get("/api/v1/consumable/mapping", params={"device": "R750"})
        check("GET /consumable/mapping 返回 404", r.status_code == 404, str(r.status_code))
        r = await api.get("/")
        check(
            "根端点清单不再列出 mapping",
            not any("consumable/mapping" in e for e in r.json()["endpoints"]),
            str(r.json()["endpoints"]),
        )

        print("\n[8] 未命中统计")
        r = await api.get("/api/v1/consumable/unmatched", params={"limit": 50})
        stats = r.json()["stats"]
        # 计数只来自 [3] 的 /search 未命中；原先 [7] 的 mapping 未命中也会写一条，该端点已下线
        check("统计 total >= 1", stats["total"] >= 1, str(stats["total"]))
        check(
            "Top 关键词含未知型号",
            any("未知冷门设备型号123" == k["keyword"] for k in stats["top_keywords"]),
            json.dumps(stats["top_keywords"], ensure_ascii=False),
        )

        print("\n[9] 上游 5xx：重试耗尽后降级为 UPSTREAM_ERROR")
        await install_mock(fail_search_times=99)
        r = await api.get("/api/v1/search", params={"keyword": "惠普 M126a"})
        body = r.json()
        primary = next(q for q in body["queries"] if q["kind"] == "primary")
        check("HTTP 仍为 200（不因单个查询失败而报错）", r.status_code == 200, str(r.status_code))
        check("primary.status=UPSTREAM_ERROR", primary["status"] == "UPSTREAM_ERROR", primary["status"])
        check("错误信息被捕获", bool(primary["error"]), str(primary["error"]))

        print("\n[10] 参数校验")
        r = await api.get("/api/v1/search", params={"keyword": ""})
        check("空关键词返回 422", r.status_code == 422, str(r.status_code))
        r = await api.get("/api/v1/detail", params={"source": 2})
        check("detail 缺 id 返回 422", r.status_code == 422, str(r.status_code))

        print("\n[11] 热重载映射表")
        r = await api.post("/api/v1/consumable/reload")
        check("reload 成功", r.status_code == 200 and r.json()["status"] == "OK")

    await maishou.shutdown()

    print("\n" + "=" * 62)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
