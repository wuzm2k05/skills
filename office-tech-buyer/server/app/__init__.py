"""办公设备易采买 —— 云端服务包。"""

from .config import settings  # noqa: F401  (便捷导入：from app import settings)

__all__ = ["settings"]
__version__ = settings.app_version
