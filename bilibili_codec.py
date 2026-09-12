"""B 站直播 WebSocket 包编解码与事件规范化（插件侧 API 兼容层）。

协议实现已统一迁移至 ``livehub/bilibili_protocol.py``（单一事实来源），
本模块仅作 re-export 以保持既有导入路径（bilibili_transport / tools 脚本）不变。

**维护约定**：协议级改动请直接修改 ``livehub/bilibili_protocol.py``，本模块无需改动；
API 兼容性由 ``tests/test_livehub_codec.py`` 的交叉用例锁定。
"""

from __future__ import annotations

from livehub.bilibili_protocol import (
    build_auth_packet,
    build_heartbeat_packet,
    build_packet,
    normalize_event,
    parse_packets,
)

__all__ = [
    "build_auth_packet",
    "build_heartbeat_packet",
    "build_packet",
    "normalize_event",
    "parse_packets",
]
