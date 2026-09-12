"""livehub 事件去重单元测试（多路 WS 连接收到相同弹幕时应只上报一次）。"""

from __future__ import annotations

import logging

from livehub.capture import BilibiliCapture


class _FakeCapture(BilibiliCapture):
    """不启动网络的捕获器，只暴露去重逻辑。"""

    def __init__(self) -> None:
        super().__init__(
            room_id=4538234,
            on_event=lambda record: None,
            logger=logging.getLogger("test_livehub_dedup"),
        )


def test_duplicate_event_id_dropped_within_window() -> None:
    capture = _FakeCapture()
    event = {"event_id": "bilibili-abc", "type": "danmaku", "text": "dup"}
    assert capture._is_duplicate_event(event) is False
    assert capture._is_duplicate_event(event) is True


def test_distinct_event_ids_pass() -> None:
    capture = _FakeCapture()
    assert capture._is_duplicate_event({"event_id": "bilibili-1", "type": "danmaku"}) is False
    assert capture._is_duplicate_event({"event_id": "bilibili-2", "type": "danmaku"}) is False


def test_event_without_event_id_not_deduped() -> None:
    capture = _FakeCapture()
    assert capture._is_duplicate_event({"type": "danmaku", "text": "no-id"}) is False
    assert capture._is_duplicate_event({"type": "danmaku", "text": "no-id"}) is False


def test_old_duplicate_expires_after_window() -> None:
    import time

    capture = _FakeCapture()
    event = {"event_id": "bilibili-old", "type": "danmaku"}
    assert capture._is_duplicate_event(event) is False
    # 手动把窗口时间拨到过去，模拟窗口过期
    now = time.time()
    capture._recent_event_ids[0] = (capture._recent_event_ids[0][0], now - 30.0)
    assert capture._is_duplicate_event(event) is False
