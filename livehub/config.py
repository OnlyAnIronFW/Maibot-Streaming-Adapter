"""livehub 服务端配置。"""

from __future__ import annotations

import logging

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import tomllib

CONFIG_FILE_NAME = "config.toml"

# 合法日志级别
_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

# 敏感字段：to_dict() 输出时排除
_SENSITIVE_FIELDS = frozenset({"auth_token"})


def _bounded_int(value: Any, low: int, high: int, default: int) -> int:
    """将值钳制为 [low, high] 内的整数；类型非法或越界回退 default。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _bounded_float(value: Any, low: float, default: float) -> float:
    """将值钳制为不小于 low 的浮点数；类型非法回退 default。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, parsed)


@dataclass
class LiveHubConfig:
    """livehub 服务端配置。

    字段与 livehub/config.toml 一一对应；命令行参数可覆盖其中部分字段。
    非法 / 越界值在 ``__post_init__`` 中被钳制回默认，避免带病启动。
    """

    # HTTP / WebSocket 监听地址
    host: str = "127.0.0.1"
    port: int = 18190
    # 可选认证令牌；配置后所有 /api/* 与 /ws 请求必须携带 Authorization: Bearer <token>
    auth_token: str = ""

    # B 站直播间
    room_id: int = 0

    # 事件历史窗口：客户端 GET /api/events 与 WS snapshot 最多返回的事件条数
    history_limit: int = 200

    # 参与者（bot）presence 心跳超时秒数，超过后视为离线
    presence_timeout_sec: float = 60.0

    # 语音互斥：当前说话者超时释放的额外宽限秒数（基于 expected_duration_ms）
    speech_timeout_grace_sec: float = 15.0

    # B 站采集
    reconnect_delay_sec: float = 5.0
    heartbeat_interval_sec: float = 30.0
    parallel_ws_connections: int = 3
    connect_timeout_sec: float = 10.0

    log_level: str = "INFO"

    def __post_init__(self) -> None:
        """钳制非法 / 越界配置值，保证进程以合法配置启动。"""
        self.host = str(self.host or "127.0.0.1").strip() or "127.0.0.1"
        self.port = _bounded_int(self.port, 1, 65535, 18190)
        self.room_id = _bounded_int(self.room_id, 0, 2**31 - 1, 0)
        self.history_limit = _bounded_int(self.history_limit, 1, 10_000, 200)
        self.presence_timeout_sec = _bounded_float(self.presence_timeout_sec, 1.0, 60.0)
        self.speech_timeout_grace_sec = _bounded_float(self.speech_timeout_grace_sec, 0.0, 15.0)
        self.reconnect_delay_sec = _bounded_float(self.reconnect_delay_sec, 0.5, 5.0)
        self.heartbeat_interval_sec = _bounded_float(self.heartbeat_interval_sec, 5.0, 30.0)
        self.parallel_ws_connections = _bounded_int(self.parallel_ws_connections, 1, 16, 3)
        self.connect_timeout_sec = _bounded_float(self.connect_timeout_sec, 1.0, 10.0)
        normalized_level = str(self.log_level or "INFO").strip().upper()
        self.log_level = normalized_level if normalized_level in _VALID_LOG_LEVELS else "INFO"

    @classmethod
    def from_toml(cls, path: Path | str) -> "LiveHubConfig":
        """从 TOML 文件加载配置；缺失字段使用默认值，非法值由 __post_init__ 钳制。"""
        config_path = Path(path)
        if not config_path.is_file():
            return cls()
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
        known_keys = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in raw.items() if key in known_keys})

    def to_dict(self) -> dict[str, Any]:
        """导出为纯字典（供状态展示使用，排除敏感字段）。"""
        return {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if field.name not in _SENSITIVE_FIELDS
        }


def default_config_path() -> Path:
    """返回 livehub 包目录下的默认配置文件路径。"""
    return Path(__file__).resolve().parent / CONFIG_FILE_NAME


def load_config(override_path: str = "", *, logger: logging.Logger | None = None) -> LiveHubConfig:
    """加载配置：优先使用指定路径，其次使用包内默认 config.toml，最后使用内置默认值。

    显式指定的配置文件缺失或非法时记录 warning 并回退默认配置，避免无提示地静默启动。
    """
    warning_logger = logger or logging.getLogger("livehub")
    if override_path:
        config_path = Path(override_path)
        if not config_path.is_file():
            warning_logger.warning(f"配置文件不存在，使用默认配置: {override_path}")
            return LiveHubConfig()
        try:
            return LiveHubConfig.from_toml(config_path)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            warning_logger.warning(f"配置文件解析失败，使用默认配置: {override_path} ({exc})")
            return LiveHubConfig()
    return LiveHubConfig.from_toml(default_config_path())


# 供外部（如测试）使用的配置字段白名单
CONFIG_FIELDS: frozenset[str] = frozenset(field.name for field in fields(LiveHubConfig))
