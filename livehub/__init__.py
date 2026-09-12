"""livehub：MaiBot Bilibili 直播适配器的多 AI 共享直播间枢纽。

livehub 是独立于插件的服务端程序，负责：
- 采集 B 站直播间弹幕（danmaku / super_chat / gift / guard）
- 将事件汇聚为带 seq 的统一事件流，通过 HTTP 轮询与 WebSocket 广播
- 维护接入的 bot 参与者列表（presence 心跳）
- 提供跨 bot 的语音互斥协调（speak-request / speak-complete）
- 转发 bot 回复给其他接入的 bot

插件侧对应实现：hub_input_client.py（HubInputClient）与 plugin.py 中 hub 相关逻辑。
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
