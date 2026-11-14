# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""办公设备易采买 —— 商品缩略图构造 + 逐条校验。

WorkBuddy 的 Markdown 渲染器**不支持行内 HTML**，所以 `<img width="28">` 那条路走不通：
尺寸没法靠标签控制，只能让**图片本身就是小的**。于是本脚本按各平台 CDN 的变形规则，
用搜索结果里原有的 ``picUrl`` 现构造一个 24px 缩略图地址，再逐条实测校验。

**为什么要校验**：HTTP 200 不等于拿到了小图。CDN 遇到不认识的参数往往不报错，直接
静默返回原图（实测 ``pddpic`` 的 ``?x-oss-process=...`` 返回 200 但仍是 800×800）。
所以每一条都要过下面三关，任一不过就判定为"没有缩略图"：

1. HTTP 状态是 200 / 206（Range 响应）；
2. **读图片头得到的真实宽高必须正好是 24×24** —— 这是唯一的硬证据；
3. 字节数 ≤ 32KB（实测真缩略图在 388B~4072B，原图 50KB~900KB，用作兜底粗筛）。

**在发请求之前还有一道主机白名单**：``picUrl`` 来自第三方上游数据，不能拿它当可信输入。
只接受 hostname 命中已登记 CDN（锚定后缀匹配）、协议为 http/https、无 userinfo、端口为
默认端口的地址，并且**按校验过的部件重建 URL** 后再请求 —— 只校验原串不重建是不够的，
因为 ``urlsplit`` 与 ``urllib`` 对反斜杠/userinfo 的解析并不一致。没通过的行直接
降级为纯文字，**一个字节都不发**。

拿不到缩略图的行，宿主 AI 必须**降级为纯文字**（去掉图片），**绝不退回原图**——
800×800 的原图会把表格撑成一屏高，正是要避免的问题。

请求一律带 ``Range: bytes=0-65535``，即使某个 CDN 忽略 Range，也只读前 64KB 就停，
不会为了校验而下一张完整原图。

用法::

    # 单个
    uv run scripts/thumb.py --url 'https://img.alicdn.com/.../xx.png'

    # 整表一次调用（推荐；同一答案内的重复 URL 只会请求一次）
    uv run scripts/thumb.py --url '<picUrl1>' --url '<picUrl2>' --url '<picUrl3>'

输出 JSON，``results`` 以**原始 picUrl 为键**，方便按行回填：

.. code-block:: json

    {
      "status": "OK",
      "size": 24,
      "results": {
        "https://img.alicdn.com/.../xx.png": {
          "ok": true,
          "thumb_url": "https://img.alicdn.com/.../xx.png_24x24.jpg",
          "w": 24, "h": 24, "bytes": 734, "reason": null
        },
        "https://p3-aio.ecombdimg.com/...": {
          "ok": false, "thumb_url": null,
          "w": null, "h": null, "bytes": 0, "reason": "no_rule"
        }
      }
    }

``reason`` 取值：``no_rule``（该 CDN 无已知变形规则，或主机未通过白名单——两种都
**不会发出请求**）、``http_error``、``too_large``、``size_mismatch``（尺寸不对，多半是
静默回原图）、``unreadable``（读不出图片头）。
退出码：``0`` 只要请求处理完成（含全部失败）；``2`` 输入有问题。仅依赖标准库。
"""

from __future__ import annotations

import argparse
import io
import json
import re
import struct
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
SIZE = 24

#: 只读前 64KB。真缩略图 0.4~4KB，图片头更靠前；这个上限足够，又不会为校验下原图。
MAX_READ = 65536

#: 真缩略图的字节上限（实测 388B~4072B）。用于拦"静默回原图"的兜底粗筛。
MAX_BYTES = 32768

TIMEOUT_SECONDS = 10.0
MAX_WORKERS = 8

USER_AGENT = "Mozilla/5.0"

# --------------------------------------------------------------------------- #
# 各 CDN 的缩略图构造规则（全部经实测验证）
# --------------------------------------------------------------------------- #
#: 域名字串 → 构造候选缩略图地址的函数（返回候选列表，按顺序试到第一个通过校验的）
RE_VIP_SIZE_SUFFIX = re.compile(r"_\d+x\d+(_\d+)?(\.\w+)?$")
RE_SUNING_SUFFIX = re.compile(r"_\d+w_\d+h_\d+e$")


def _alicdn(url: str) -> list[str]:
    """淘宝/天猫：在文件名后追加 ``_24x24.jpg``。只认预设尺寸，48/50/64/80 也在白名单。"""
    return [f"{url}_{SIZE}x{SIZE}.jpg"]


def _pddpic(url: str) -> list[str]:
    """拼多多：追加 ``?imageMogr2/thumbnail/24x24``（URL 已有 ? 时用 &）。"""
    sep = "&" if "?" in url else "?"
    return [f"{url}{sep}imageMogr2/thumbnail/{SIZE}x{SIZE}"]


def _jd(url: str) -> list[str]:
    """京东：把路径里的 ``/jfs/`` 换成 ``/s24x24_jfs/``（尺寸段挂在 jfs 这一段上）。

    实测 ``/pop/s24x24_jfs/`` 与 ``/n1/s24x24_jfs/`` 都生效，而在 host 根上直接写
    ``/s24x24_jfs/`` 会 400，所以第二个候选是把 jfs 之后的路径挂到 ``/n1/s24x24_jfs/``。
    """
    candidates = [url.replace("/jfs/", f"/s{SIZE}x{SIZE}_jfs/", 1)]
    if "/jfs/" in url:
        tail = url.split("/jfs/", 1)[1]
        host = url.split("//", 1)[0] + "//" + url.split("/")[2]
        candidates.append(f"{host}/n1/s{SIZE}x{SIZE}_jfs/{tail}")
    return candidates


def _suning(url: str) -> list[str]:
    """苏宁：把结尾的 ``_800w_800h_4e`` 换成 ``_24w_24h_4e``；无该后缀时按追加试一次。"""
    candidates = []
    if RE_SUNING_SUFFIX.search(url):
        candidates.append(RE_SUNING_SUFFIX.sub(f"_{SIZE}w_{SIZE}h_4e", url))
    candidates.append(f"{url}_{SIZE}w_{SIZE}h.jpg")
    return candidates


def _vip(url: str) -> list[str]:
    """唯品会：把结尾的 ``_750x750_50.jpg`` 换成 ``_24x24.jpg``。"""
    if not RE_VIP_SIZE_SUFFIX.search(url):
        return []
    return [RE_VIP_SIZE_SUFFIX.sub(lambda m: f"_{SIZE}x{SIZE}" + (m.group(2) or ""), url)]


def _kuaishou(url: str) -> list[str]:
    """快手：``?x-oss-process=image/resize,w_24``；部分节点只认这个，不认 imageMogr2。"""
    sep = "&" if "?" in url else "?"
    return [
        f"{url}{sep}x-oss-process=image/resize,w_{SIZE}",
        f"{url}{sep}imageMogr2/thumbnail/{SIZE}x{SIZE}",
    ]


#: 各 CDN 的**主机白名单**：锚定正则（匹配解析后的 hostname）+ 构造函数。
#: 用正则锚定 hostname，而不是拿整个 URL 做子串包含——子串匹配能被
#: ``https://evil.com/?ref=alicdn.com``、``http://127.0.0.1/alicdn.com`` 这类 URL 绕过，
#: 而绕过之后是会**真的发出请求**的（这是 SSRF 面）。
#:
#: 注意：``ecombdimg.com``（抖音）实测 ``_24x24`` / ``imageMogr2`` / ``x-oss-process``
#: 全部静默返回原图，**没有可用规则**，故意不登记——这类平台一律降级为纯文字。
HOST_RULES: tuple[tuple[re.Pattern[str], object], ...] = (
    (re.compile(r"^[a-z0-9-]+\.alicdn\.com$"), _alicdn),
    (re.compile(r"^[a-z0-9-]+\.pddpic\.com$"), _pddpic),
    (re.compile(r"^[a-z0-9-]+\.yangkeduo\.com$"), _pddpic),
    (re.compile(r"^[a-z0-9-]+\.360buyimg\.com$"), _jd),
    (re.compile(r"^[a-z0-9-]+\.suning\.cn$"), _suning),
    (re.compile(r"^a\.vpimg[0-9]+\.com$"), _vip),
    (re.compile(r"^p[0-9]+-ec\.ecukwai\.com$"), _kuaishou),
)

#: 只允许这两个协议；``ftp:`` / ``file:`` / 相对协议一律拒绝。
ALLOWED_SCHEMES = frozenset({"http", "https"})
#: 只允许默认端口（显式写 80/443 也接受，重建时会归一化掉）。
ALLOWED_PORTS = frozenset({None, 80, 443})


def _is_ip_literal(host: str) -> bool:
    """host 是否是 IP 字面量（IPv4 点分或含冒号的 IPv6）。CDN 主机名不该是 IP。"""
    if ":" in host:
        return True
    parts = host.split(".")
    return len(parts) == 4 and all(part.isdigit() for part in parts)


def prepare(pic_url: str) -> tuple[str | None, object | None]:
    """校验并归一化 ``picUrl``，返回 ``(可安全请求的 URL, 构造函数)``；不通过则 ``(None, None)``。

    四道校验，任一不过就拒绝（``reason`` 会落到 ``no_rule``，且**不发出任何请求**）：

    1. 协议必须是 http/https；
    2. 不得带 userinfo —— 挡掉 ``http://evil.com\\@img.alicdn.com/`` 这类解析歧义；
    3. 端口必须是默认端口；
    4. hostname 必须命中锚定白名单，且不是 IP 字面量。

    **通过后从校验过的部件重建 URL**，不把原始字符串交给 ``urllib``。因为 ``urlsplit``
    与 ``urllib`` 对反斜杠和 userinfo 的解析并不一致（实测 ``http://evil.com\\@img.alicdn.com/``：
    ``urlsplit`` 认为 host 是 ``img.alicdn.com``，``urllib`` 认为 host 是
    ``evil.com\\@img.alicdn.com``）。只校验原串而不重建，等于校验的和实际请求的不是同一个地址。
    """
    try:
        parts = urlsplit(pic_url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None, None

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return None, None
    if parts.username or parts.password:
        return None, None
    if port not in ALLOWED_PORTS:
        return None, None
    if not host:
        return None, None

    host = host.rstrip(".").lower()
    if _is_ip_literal(host):
        return None, None

    for pattern, build in HOST_RULES:
        if pattern.match(host):
            rebuilt = urlunsplit((parts.scheme.lower(), host, parts.path, parts.query, ""))
            return rebuilt, build
    return None, None


def rule_for(url: str) -> object | None:
    """按主机挑构造规则；主机不在白名单内返回 ``None``（调用方降级为纯文字）。"""
    return prepare(url)[1]


# --------------------------------------------------------------------------- #
# 图片头解析：拿真实宽高
# --------------------------------------------------------------------------- #
SOF_MARKERS = frozenset(
    (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
)


def _dims_jpeg(data: bytes) -> tuple[int, int] | None:
    i, n = 2, len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if marker in SOF_MARKERS:
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return (width, height)
        i += 2 + length
    return None


def _dims_png(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24:
        return None
    return struct.unpack(">II", data[16:24])


def _dims_webp(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        return (
            int.from_bytes(data[24:27], "little") + 1,
            int.from_bytes(data[27:30], "little") + 1,
        )
    if chunk == b"VP8 ":
        return struct.unpack("<HH", data[26:30])
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    return None


def image_dims(data: bytes) -> tuple[int, int] | None:
    """读图片头拿真实宽高。JPEG / PNG / WebP；认不出来返回 ``None``。

    注意 alicdn 的缩略图 URL 写的是 ``.jpg``，**实际返回 webp**，所以不能靠
    ``Content-Type`` 或扩展名判断，只能按字节头解析。
    """
    if data[:2] == b"\xff\xd8":
        return _dims_jpeg(data)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _dims_png(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _dims_webp(data)
    return None


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def fetch_head(url: str) -> tuple[int, bytes]:
    """只拉前 ``MAX_READ`` 字节。返回 ``(status, body)``；异常时 status 非 2xx。"""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Range": f"bytes=0-{MAX_READ - 1}",
            "Accept": "image/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read(MAX_READ)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, OSError, ValueError):
        return -1, b""


def verify_candidate(url: str) -> tuple[bool, dict]:
    """校验一个候选缩略图地址。返回 ``(是否通过, 详情)``。"""
    status, body = fetch_head(url)
    detail = {"w": None, "h": None, "bytes": len(body), "status": status}

    if status not in (200, 206) or not body:
        detail["reason"] = "http_error"
        return False, detail

    if len(body) > MAX_BYTES:
        # 原图动辄 50KB~900KB，这一条基本就能判掉"静默回原图"
        detail["reason"] = "too_large"
        return False, detail

    dims = image_dims(body)
    if dims is None:
        detail["reason"] = "unreadable"
        return False, detail

    detail["w"], detail["h"] = dims
    if dims != (SIZE, SIZE):
        # 唯一的硬证据：尺寸不对就是没用上缩略图规则
        detail["reason"] = "size_mismatch"
        return False, detail

    detail["reason"] = None
    return True, detail


def resolve(pic_url: str) -> dict:
    """把一条 ``picUrl`` 解析为「通过校验的缩略图地址」或「没有」。"""
    result = {
        "ok": False,
        "thumb_url": None,
        "w": None,
        "h": None,
        "bytes": 0,
        "reason": "no_rule",
    }

    build = rule_for(pic_url)
    if build is None:
        return result

    # 用**校验并重建过**的 URL 去构造候选，不把原始字符串交给 urllib
    base, _ = prepare(pic_url)
    if base is None:  # 理论上不可达：rule_for 非 None 时 prepare 必成功
        return result

    for candidate in build(base):
        passed, detail = verify_candidate(candidate)
        result["w"] = detail.get("w")
        result["h"] = detail.get("h")
        result["bytes"] = detail.get("bytes", 0)
        result["reason"] = detail.get("reason")
        if passed:
            result["ok"] = True
            result["thumb_url"] = candidate
            return result

    # 所有候选都不过：把最后一个失败原因留下，便于排查
    return result


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def fix_stdout() -> None:
    """修复 Windows GBK 控制台下输出中文的 UnicodeEncodeError。"""
    encoding = (sys.stdout.encoding or "").upper()
    if encoding != "UTF-8":
        try:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace"
            )
        except (AttributeError, ValueError):
            pass


def read_urls(args: argparse.Namespace) -> tuple[list[str], str | None]:
    urls = list(args.url or [])
    if args.stdin:
        data = sys.stdin.read()
        if not data.strip():
            return [], "stdin 为空：请传入一个 picUrl 数组（JSON）或每行一个地址。"
        stripped = data.lstrip()
        if stripped.startswith("["):
            try:
                loaded = json.loads(data)
            except json.JSONDecodeError as exc:
                return [], f"stdin 不是合法 JSON 数组：{exc}"
            if not isinstance(loaded, list):
                return [], "stdin 的 JSON 顶层必须是数组。"
            urls.extend(str(item).strip() for item in loaded if str(item).strip())
        else:
            urls.extend(line.strip() for line in data.splitlines() if line.strip())

    seen: set[str] = set()
    unique = [u for u in urls if u and not (u in seen or seen.add(u))]
    if not unique:
        return [], (
            "未提供 picUrl：用 --url 传入（可重复），或 --stdin 从管道读取"
            "（JSON 数组或每行一个地址）。"
        )
    return unique, None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="thumb.py",
        description=(
            "办公设备易采买 —— 构造并校验商品缩略图地址（按 CDN 规则改 picUrl，"
            "并读图片头确认真的缩到 24×24）"
        ),
    )
    parser.add_argument(
        "--url",
        action="append",
        metavar="PIC_URL",
        help="商品原始图片地址（picUrl），可重复传入",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="从标准输入读取：JSON 数组，或每行一个地址",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help=f"并发数（默认 {MAX_WORKERS}）",
    )
    return parser


def main() -> int:
    fix_stdout()
    args = build_parser().parse_args()

    urls, error = read_urls(args)
    if error:
        print(json.dumps({"status": "INPUT_ERROR", "message": error},
                         ensure_ascii=False, indent=2))
        return 2

    workers = max(1, min(args.workers, 16))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        resolved = list(pool.map(resolve, urls))

    results = {url: item for url, item in zip(urls, resolved)}
    passed = sum(1 for item in resolved if item["ok"])

    print(
        json.dumps(
            {
                "status": "OK",
                "size": SIZE,
                "counts": {"total": len(urls), "ok": passed, "failed": len(urls) - passed},
                "results": results,
                "instruction": (
                    "ok 为 true 的行，【预览】列写 `![商品名](thumb_url)`；"
                    "ok 为 false 的行**不要放图片，降级为纯文字**，"
                    "严禁退回原 picUrl（原图会把表格撑成一屏高）。"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
