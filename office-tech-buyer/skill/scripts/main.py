# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""办公设备易采买 —— 技能端 CLI（极轻量 HTTP 转发器）。

设计原则：**零业务逻辑**。本脚本只做三件事：

1. 用 ``argparse`` 解析命令行参数；
2. 把请求转发到云端服务 ``OFFICE_BUYER_SERVER_URL``（默认指向生产环境）；
3. 把云端返回的 JSON 原样打印到 stdout。

所有请求头伪装、上游限流、品类与耗材映射、未命中日志落盘都在云端完成。
仅依赖 Python 标准库（``urllib``），无需安装任何三方包。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# 本机自测时改回这台地址（服务端默认监听 11443）
#DEFAULT_SERVER_URL = "http://localhost:11443"
DEFAULT_SERVER_URL = "https://api.ninenode.com/api/office-buyer"
API_PREFIX = "/api/v1"
TIMEOUT_SECONDS = 60.0


def _fix_stdout() -> None:
    """修复 Windows GBK 控制台下输出中文的 UnicodeEncodeError。"""
    encoding = (sys.stdout.encoding or "").upper()
    if encoding != "UTF-8":
        try:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace"
            )
        except (AttributeError, ValueError):
            pass


def server_url() -> str:
    """云端服务地址，环境变量优先，否则用 ``DEFAULT_SERVER_URL``。"""
    url = os.environ.get("OFFICE_BUYER_SERVER_URL", "").strip() or DEFAULT_SERVER_URL
    return url.rstrip("/")


def request(path: str, params: dict[str, str]) -> dict:
    """GET 云端接口并返回解析后的 JSON。"""
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{server_url()}{API_PREFIX}{path}?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "office-tech-buyer-skill/1.1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"status": "HTTP_ERROR", "message": body}
        payload.setdefault("http_status", exc.code)
        payload.setdefault("request_url", url)
        return payload
    except urllib.error.URLError as exc:
        return {
            "status": "SERVER_UNREACHABLE",
            "message": f"无法连接云端服务 {server_url()}：{exc.reason}",
            "hint": (
                "请确认云端服务已启动，或设置环境变量 "
                "OFFICE_BUYER_SERVER_URL 指向正确的服务地址。"
            ),
            "request_url": url,
        }

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "status": "INVALID_JSON",
            "message": "云端返回内容不是合法 JSON",
            "raw": raw[:2000],
            "request_url": url,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="办公设备易采买 —— 全品类办公设备与耗材比价（云端转发）",
    )
    sub = parser.add_subparsers(dest="command")

    search_parser = sub.add_parser("search", help="搜索办公设备、耗材或纸品文具")
    search_parser.add_argument("--keyword", required=True, help="搜索关键词，如 '惠普 M126a'")
    search_parser.add_argument(
        "--source",
        default="0",
        help="渠道 0:全部 1:淘宝/天猫 2:京东 3:拼多多 4:苏宁 5:唯品会 7:抖音 8:快手",
    )
    search_parser.add_argument("--page", default="1", help="页码，默认 1")

    detail_parser = sub.add_parser("detail", help="查询商品详情、购买链接与专票质保")
    detail_parser.add_argument("--id", required=True, help="商品 ID（search 结果中的 goodsId）")
    detail_parser.add_argument(
        "--source", required=True, help="渠道来源，必须与 search 结果中的 source 一致"
    )

    mapping_parser = sub.add_parser(
        "mapping", help="查询某设备对应的耗材/配件映射（映射未命中时提示 AI 推导）"
    )
    mapping_parser.add_argument("--device", required=True, help="设备型号，如 'M126a'")

    sub.add_parser("unmatched", help="查看云端未命中关键词日志统计（运维用）")

    return parser


def dispatch(args: argparse.Namespace) -> dict:
    if args.command == "search":
        return request(
            "/search",
            {"keyword": args.keyword, "source": str(args.source), "page": str(args.page)},
        )
    if args.command == "detail":
        return request("/detail", {"id": args.id, "source": str(args.source)})
    if args.command == "mapping":
        return request("/consumable/mapping", {"device": args.device})
    if args.command == "unmatched":
        return request("/consumable/unmatched", {"limit": "200"})
    return {
        "status": "NO_COMMAND",
        "message": "请指定子命令：search / detail / mapping / unmatched",
        "server": server_url(),
    }


def main() -> int:
    _fix_stdout()
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    result = dispatch(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # 网络层错误以非零码退出，便于宿主 AI 识别
    if isinstance(result, dict) and result.get("status") in {
        "SERVER_UNREACHABLE",
        "INVALID_JSON",
        "INTERNAL_ERROR",
        "UPSTREAM_ERROR",
    }:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
