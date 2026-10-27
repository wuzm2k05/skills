"""全局配置中心。

所有可变项均可通过环境变量覆盖，默认值保证在本地开发机上零配置直接启动。
技能端（skill/scripts/main.py）不持有任何业务配置，全部逻辑收敛在这里。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------- #
# 目录常量
# --------------------------------------------------------------------------- #
APP_DIR = Path(__file__).resolve().parent            # server/app
SERVER_DIR = APP_DIR.parent                          # server
DEFAULT_DATA_DIR = APP_DIR / "data"
DEFAULT_LOG_DIR = SERVER_DIR / "logs"


# --------------------------------------------------------------------------- #
# 环境变量读取助手
# --------------------------------------------------------------------------- #
def _str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _list(name: str, default: tuple[str, ...]) -> list[str]:
    raw = os.getenv(name)
    if raw in (None, ""):
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------- #
# 平台渠道定义（与 maishou88 上游 sourceType 对齐）
# --------------------------------------------------------------------------- #
SOURCE_NAMES: dict[int, str] = {
    0: "全部",
    1: "淘宝/天猫",
    2: "京东",
    3: "拼多多",
    4: "苏宁",
    5: "唯品会",
    7: "抖音",
    8: "快手",
}


@dataclass(frozen=True)
class Settings:
    """运行期配置（进程启动时从环境变量固化）。"""

    # ---- 服务自身 ----
    app_name: str = "office-tech-buyer-server"
    app_version: str = "1.1.0"
    host: str = field(default_factory=lambda: _str("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("PORT", 11443))
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = field(
        default_factory=lambda: _list("OFFICE_BUYER_CORS_ORIGINS", ("*",))
    )
    trust_forwarded_for: bool = field(
        default_factory=lambda: _bool("OFFICE_BUYER_TRUST_FORWARDED_FOR", True)
    )

    # ---- 数据与日志 ----
    data_dir: Path = field(
        default_factory=lambda: Path(_str("OFFICE_BUYER_DATA_DIR", str(DEFAULT_DATA_DIR)))
    )
    log_dir: Path = field(
        default_factory=lambda: Path(_str("OFFICE_BUYER_LOG_DIR", str(DEFAULT_LOG_DIR)))
    )
    unmatched_log_name: str = "unmatched.log"
    #: 单个日志分片的大小上限（字节），超出即滚动；设为 0 关闭滚动（不推荐）
    log_max_bytes: int = field(
        default_factory=lambda: _int("OFFICE_BUYER_LOG_MAX_BYTES", 5 * 1024 * 1024)
    )
    #: 保留的历史分片数；总占用上限约为 log_max_bytes * (log_backup_count + 1)
    log_backup_count: int = field(
        default_factory=lambda: _int("OFFICE_BUYER_LOG_BACKUP_COUNT", 5)
    )

    # ---- 上游 maishou88 ----
    upstream_search_url: str = field(
        default_factory=lambda: _str(
            "OFFICE_BUYER_UPSTREAM_SEARCH_URL",
            "https://appapi.maishou88.com/api/v1/homepage/searchList",
        )
    )
    upstream_detail_url: str = field(
        default_factory=lambda: _str(
            "OFFICE_BUYER_UPSTREAM_DETAIL_URL",
            "https://appapi.maishou88.com/api/v3/goods/detail",
        )
    )
    upstream_target_url: str = field(
        default_factory=lambda: _str(
            "OFFICE_BUYER_UPSTREAM_TARGET_URL",
            "https://msapi.maishou88.com/api/v1/share/getTargetUrl",
        )
    )
    upstream_openid: str = field(
        default_factory=lambda: _str(
            "OFFICE_BUYER_UPSTREAM_OPENID", "564bdce0fa408fc9e1d5d42fd022ef0b"
        )
    )
    upstream_version: str = field(
        default_factory=lambda: _str("OFFICE_BUYER_UPSTREAM_VERSION", "3.7.7.2")
    )
    upstream_invite_code: str = field(
        default_factory=lambda: _str("OFFICE_BUYER_UPSTREAM_INVITE_CODE", "95953233")
    )
    upstream_app_ua: str = field(
        default_factory=lambda: _str(
            "OFFICE_BUYER_UPSTREAM_APP_UA",
            "MaiShouApp/3.7.7 (iPhone; iOS 26.3; Scale/3.00)",
        )
    )
    upstream_web_ua: str = field(
        default_factory=lambda: _str(
            "OFFICE_BUYER_UPSTREAM_WEB_UA",
            "Mozilla/5.0 AppleWebKit/537 Chrome/143 Safari/537",
        )
    )
    upstream_referer: str = field(
        default_factory=lambda: _str(
            "OFFICE_BUYER_UPSTREAM_REFERER", "https://hnbc018.kuaizhan.com/"
        )
    )

    # ---- 上游容错与限流 ----
    upstream_timeout: float = field(
        default_factory=lambda: _float("OFFICE_BUYER_UPSTREAM_TIMEOUT", 15.0)
    )
    upstream_connect_timeout: float = field(
        default_factory=lambda: _float("OFFICE_BUYER_UPSTREAM_CONNECT_TIMEOUT", 5.0)
    )
    upstream_retries: int = field(
        default_factory=lambda: _int("OFFICE_BUYER_UPSTREAM_RETRIES", 3)
    )
    upstream_backoff: float = field(
        default_factory=lambda: _float("OFFICE_BUYER_UPSTREAM_BACKOFF", 0.4)
    )
    upstream_concurrency: int = field(
        default_factory=lambda: _int("OFFICE_BUYER_UPSTREAM_CONCURRENCY", 5)
    )
    upstream_min_interval: float = field(
        default_factory=lambda: _float("OFFICE_BUYER_UPSTREAM_MIN_INTERVAL", 0.15)
    )

    # ---- 业务策略 ----
    max_related_queries: int = field(
        default_factory=lambda: _int("OFFICE_BUYER_MAX_RELATED_QUERIES", 2)
    )
    max_items_per_query: int = field(
        default_factory=lambda: _int("OFFICE_BUYER_MAX_ITEMS_PER_QUERY", 20)
    )
    default_packs_per_box: int = field(
        default_factory=lambda: _int("OFFICE_BUYER_DEFAULT_PACKS_PER_BOX", 5)
    )
    default_sheets_per_pack: int = field(
        default_factory=lambda: _int("OFFICE_BUYER_DEFAULT_SHEETS_PER_PACK", 500)
    )

    # ---- 派生路径 ----
    @property
    def unmatched_log(self) -> Path:
        return self.log_dir / self.unmatched_log_name

    @property
    def unmatched_keyword_max_chars(self) -> int:
        """写入日志时关键词保留的最大字符数，超出部分丢弃。

        关键词来自用户输入（接口侧限制 200 字），再截断一次是为了保证单行日志
        大小有界：无论调用方是谁，都不会因为一个超长关键词把日志撑大。
        """
        return 120

    @property
    def consumables_file(self) -> Path:
        return self.data_dir / "consumables.json"

    @property
    def server_hardware_file(self) -> Path:
        return self.data_dir / "server_hardware.json"

    @property
    def paper_spec_file(self) -> Path:
        return self.data_dir / "paper_spec.json"


settings = Settings()
