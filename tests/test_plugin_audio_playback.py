import asyncio
import contextlib
import unittest

from . import _host_bootstrap  # noqa: F401

from plugins.maibot_bilibili_live_adapter_copy.constants import PLATFORM_NAME
from plugins.maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from plugins.maibot_bilibili_live_adapter_copy.plugin import (
    BilibiliLiveAdapterPlugin,
    _infer_emotion_intent,
    _should_expose_webui_audio,
    _should_play_local_audio,
)
from plugins.maibot_bilibili_live_adapter_copy.tts_provider import SynthesizedSpeech


class _AudioPlayerStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def play(self, audio_ref: str, *, duration_ms: int = 0, on_audio_start=None) -> bool:
        self.calls.append({"audio_ref": audio_ref, "duration_ms": duration_ms})
        if on_audio_start is not None:
            on_audio_start()
        return True


class _HubInputClientStub:
    def __init__(self, response: dict[str, object] | None = None) -> None:
        self.requests: list[dict[str, object]] = []
        self.response = dict(response or {})

    async def request_speak_turn(self, payload: dict[str, object]) -> dict[str, object]:
        self.requests.append(dict(payload))
        return dict(self.response)


class _SubtitleWebUIStub:
    def __init__(self) -> None:
        self.has_clients = False
        self.replies: list[dict[str, object]] = []

    async def publish_reply(self, *, reply_id: str, text: str, segments, source_platform: str = "") -> None:
        self.replies.append(
            {
                "reply_id": reply_id,
                "text": text,
                "segments": list(segments),
                "source_platform": source_platform,
            }
        )


class _ShellRuntimePayloadStub:
    def __init__(self) -> None:
        self.payloads = [{"type": "set_interactive", "interactive": False}]


class PluginAudioPlaybackTest(unittest.IsolatedAsyncioTestCase):
    def test_chinese_reply_text_infers_live2d_emotion_intent(self) -> None:
        self.assertEqual(_infer_emotion_intent("\u54c8\u54c8\uff0c\u592a\u597d\u4e86\uff0c\u4eca\u5929\u771f\u5f00\u5fc3"), "react_happy")
        self.assertEqual(_infer_emotion_intent("\u54c7\uff01\uff1f\u8fd9\u4e5f\u592a\u60ca\u8bb6\u4e86"), "react_surprised")
        self.assertEqual(_infer_emotion_intent("\u6709\u70b9\u5bb3\u7f9e\uff0c\u8138\u7ea2\u4e86"), "react_shy")
        self.assertEqual(_infer_emotion_intent("\u4e3a\u4ec0\u4e48\u4f1a\u8fd9\u6837\uff1f\u600e\u4e48\u56de\u4e8b"), "react_confused")
        self.assertEqual(_infer_emotion_intent("\u592a\u96be\u8fc7\u4e86\uff0c\u771f\u7684\u597d\u60b2\u4f24"), "react_sad")
        self.assertEqual(_infer_emotion_intent("\u6c14\u6b7b\u6211\u4e86\uff0c\u771f\u7684\u597d\u751f\u6c14"), "react_angry")

    def test_local_audio_playback_helper_prefers_plugin_local_mode(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "live2d": {"sync": {"mouth_sync_mode": "plugin_local"}},
                "tts": {"enabled": True, "audio_playback_enabled": True},
            }
        )

        self.assertTrue(_should_play_local_audio(settings))

    def test_webui_audio_is_hidden_when_local_audio_playback_is_enabled(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "live2d": {"sync": {"mouth_sync_mode": "plugin_local"}},
                "tts": {"enabled": True, "audio_playback_enabled": True},
            }
        )

        self.assertFalse(_should_expose_webui_audio(settings))

    async def test_plugin_creates_local_audio_playback_task_for_plugin_local_reply(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        player = _AudioPlayerStub()
        plugin._audio_output_player = player
        callback_markers: list[str] = []
        settings = LiveAdapterSettings.model_validate(
            {
                "live2d": {"sync": {"mouth_sync_mode": "plugin_local"}},
                "tts": {
                    "enabled": True,
                    "audio_playback_enabled": True,
                    "audio_output_device": "",
                },
            }
        )
        speech = SynthesizedSpeech(
            provider="test",
            text="hello",
            audio_ref="F:/tmp/test.wav",
            audio_duration_ms=320,
            sample_rate=32000,
            amplitudes=[{"offset_ms": 0, "value": 0.5}],
        )

        task = plugin._create_audio_playback_task(
            speech,
            settings=settings,
            enabled=_should_play_local_audio(settings),
            on_audio_start=lambda: callback_markers.append("started"),
        )

        self.assertIsNotNone(task)
        self.assertTrue(await task)
        self.assertEqual(len(player.calls), 1)
        self.assertEqual(player.calls[0]["audio_ref"], "F:/tmp/test.wav")
        self.assertEqual(callback_markers, ["started"])

    async def test_mirror_hook_does_not_block_on_local_audio_playback_completion(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = LiveAdapterSettings.model_validate(
            {
                "plugin": {"enabled": True},
                "live2d": {
                    "enabled": True,
                    "send_bot_replies": True,
                    "mirror_other_platform_replies": True,
                    "sync": {"mouth_sync_mode": "plugin_local"},
                },
                "tts": {"enabled": True, "audio_playback_enabled": True},
                "webui": {"enabled": False},
            }
        )
        plugin.set_plugin_config(settings.model_dump(mode="python"))
        plugin._live2d_controller = type(
            "_Live2DStub",
            (),
            {
                "play_reply": staticmethod(
                    lambda *args, **kwargs: _completed_future(type("_Timeline", (), {"timeline_id": "tl-test"})())
                )
            },
        )()
        plugin._prepare_reply_delivery = lambda *args, **kwargs: _completed_future(
            (
                "hello",
                "hello",
                {"audio_duration_ms": 60000},
                SynthesizedSpeech(
                    provider="test",
                    text="hello",
                    audio_ref="F:/tmp/long.wav",
                    audio_duration_ms=60000,
                    sample_rate=32000,
                    amplitudes=[{"offset_ms": 0, "value": 0.5}],
                ),
            )
        )
        plugin._publish_reply_to_webui = lambda *args, **kwargs: _completed_future((False, False))
        pending_audio_gate = asyncio.Event()
        pending_task: asyncio.Task[bool] | None = None

        def _create_pending_audio_task(*args, **kwargs):
            nonlocal pending_task
            pending_task = asyncio.create_task(_await_event(pending_audio_gate), name="test.pending_audio")
            return pending_task

        plugin._create_audio_playback_task = _create_pending_audio_task
        message = {
            "platform": "qq",
            "processed_plain_text": "hello",
            "raw_message": [{"type": "text", "data": "hello"}],
        }

        result = await asyncio.wait_for(plugin.mirror_outbound_reply_to_live2d(message=message), timeout=0.2)
        self.assertEqual(result["action"], "continue")
        self.assertTrue(result["custom_result"]["live2d_synchronized"])
        self.assertFalse(result["custom_result"]["audio_played_to_vts"])
        self.assertIsNotNone(pending_task)
        self.assertFalse(pending_task.done())
        self.assertIn(pending_task, plugin._background_audio_playback_tasks)
        pending_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending_task
        self.assertNotIn(pending_task, plugin._background_audio_playback_tasks)

    async def test_serialized_live_delivery_does_not_block_on_local_audio_playback_completion(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = LiveAdapterSettings.model_validate(
            {
                "plugin": {"enabled": True},
                "live2d": {
                    "enabled": True,
                    "send_bot_replies": True,
                    "sync": {"mouth_sync_mode": "plugin_local"},
                },
                "tts": {"enabled": True, "audio_playback_enabled": True},
                "webui": {"enabled": False},
            }
        )
        plugin.set_plugin_config(settings.model_dump(mode="python"))
        plugin._live2d_controller = type(
            "_Live2DStub",
            (),
            {
                "is_speaking": False,
                "play_reply": staticmethod(
                    lambda *args, **kwargs: _completed_future(type("_Timeline", (), {"timeline_id": "tl-live"})())
                ),
            },
        )()
        plugin._prepare_reply_delivery = lambda *args, **kwargs: _completed_future(
            (
                "hello",
                "hello",
                {"audio_duration_ms": 60000},
                SynthesizedSpeech(
                    provider="test",
                    text="hello",
                    audio_ref="F:/tmp/long.wav",
                    audio_duration_ms=60000,
                    sample_rate=32000,
                    amplitudes=[{"offset_ms": 0, "value": 0.5}],
                ),
            )
        )
        plugin._publish_reply_to_webui = lambda *args, **kwargs: _completed_future((False, False))
        plugin._schedule_hub_output_forward = lambda *args, **kwargs: None
        pending_audio_gate = asyncio.Event()
        pending_task: asyncio.Task[bool] | None = None

        def _create_pending_audio_task(*args, **kwargs):
            nonlocal pending_task
            pending_task = asyncio.create_task(_await_event(pending_audio_gate), name="test.pending_live_audio")
            return pending_task

        plugin._create_audio_playback_task = _create_pending_audio_task

        result = await asyncio.wait_for(
            plugin._deliver_text_reply_serialized(
                "hello",
                settings=settings,
                source_platform=PLATFORM_NAME,
                metadata={},
                kwargs={},
            ),
            timeout=0.2,
        )

        self.assertTrue(result["live2d_synchronized"])
        self.assertFalse(result["audio_played_to_vts"])
        self.assertFalse(result["delivery_waited"])
        self.assertIsNotNone(pending_task)
        self.assertFalse(pending_task.done())
        self.assertIn(pending_task, plugin._background_audio_playback_tasks)
        pending_wait_task = plugin._pending_local_delivery_wait_task
        if pending_wait_task is not None:
            pending_wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending_wait_task
        pending_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending_task
        self.assertNotIn(pending_task, plugin._background_audio_playback_tasks)

    async def test_mirror_hook_skips_live2d_when_controller_drops_during_publish(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = LiveAdapterSettings.model_validate(
            {
                "plugin": {"enabled": True},
                "live2d": {
                    "enabled": True,
                    "send_bot_replies": True,
                    "mirror_other_platform_replies": True,
                    "sync": {"mouth_sync_mode": "plugin_local"},
                },
                "tts": {"enabled": True, "audio_playback_enabled": False},
                "webui": {"enabled": False},
            }
        )
        plugin.set_plugin_config(settings.model_dump(mode="python"))
        plugin._live2d_controller = type(
            "_Live2DStub",
            (),
            {
                "play_reply": staticmethod(
                    lambda *args, **kwargs: _completed_future(type("_Timeline", (), {"timeline_id": "tl-test"})())
                )
            },
        )()
        plugin._prepare_reply_delivery = lambda *args, **kwargs: _completed_future(
            (
                "hello",
                "hello",
                {"audio_duration_ms": 1000},
                SynthesizedSpeech(
                    provider="test",
                    text="hello",
                    audio_ref="F:/tmp/test.wav",
                    audio_duration_ms=1000,
                    sample_rate=32000,
                    amplitudes=[{"offset_ms": 0, "value": 0.5}],
                ),
            )
        )

        async def _drop_controller(*args, **kwargs):
            plugin._live2d_controller = None
            return (False, False)

        plugin._publish_reply_to_webui = _drop_controller
        message = {
            "platform": "qq",
            "processed_plain_text": "hello",
            "raw_message": [{"type": "text", "data": "hello"}],
        }

        result = await plugin.mirror_outbound_reply_to_live2d(message=message)
        self.assertEqual(result["action"], "continue")
        self.assertFalse(result["custom_result"]["live2d_synchronized"])
        self.assertEqual(result["custom_result"]["timeline_id"], "")

    async def test_render_prepared_local_reply_skips_live2d_when_controller_drops_during_publish(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = LiveAdapterSettings.model_validate(
            {
                "plugin": {"enabled": True},
                "live2d": {
                    "enabled": True,
                    "send_bot_replies": True,
                    "sync": {"mouth_sync_mode": "plugin_local"},
                },
                "tts": {"enabled": True, "audio_playback_enabled": False},
                "webui": {"enabled": False},
            }
        )
        plugin.set_plugin_config(settings.model_dump(mode="python"))
        plugin._live2d_controller = type(
            "_Live2DStub",
            (),
            {
                "play_reply": staticmethod(
                    lambda *args, **kwargs: _completed_future(type("_Timeline", (), {"timeline_id": "tl-render"})())
                )
            },
        )()

        async def _drop_controller(*args, **kwargs):
            plugin._live2d_controller = None
            return (False, False)

        plugin._publish_reply_to_webui = _drop_controller
        speech = SynthesizedSpeech(
            provider="test",
            text="hello",
            audio_ref="F:/tmp/test.wav",
            audio_duration_ms=1000,
            sample_rate=32000,
            amplitudes=[{"offset_ms": 0, "value": 0.5}],
        )

        result = await plugin._render_prepared_local_reply(
            "hello",
            speech_text="hello",
            subtitle_text="hello",
            audio_timeline={"audio_duration_ms": 1000},
            synthesized_speech=speech,
            settings=settings,
            source_platform="qq",
            metadata={},
        )
        self.assertFalse(result["live2d_synchronized"])
        self.assertNotEqual(result["timeline_id"], "tl-render")

    async def test_publish_reply_to_webui_keeps_subtitle_payloads_out_of_shell_runtime(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = LiveAdapterSettings.model_validate(
            {
                "plugin": {"enabled": True},
                "webui": {"enabled": True},
                "tts": {"enabled": False, "audio_playback_enabled": False},
            }
        )
        plugin.set_plugin_config(settings.model_dump(mode="python"))
        plugin._subtitle_webui = _SubtitleWebUIStub()
        plugin._soullink_shell_runtime = _ShellRuntimePayloadStub()

        published, audio_started = await plugin._publish_reply_to_webui(
            "hello world",
            settings=settings,
            source_platform="qq",
            audio_timeline=None,
            synthesized_speech=None,
            speech_text="hello world",
        )

        self.assertTrue(published)
        self.assertFalse(audio_started)
        self.assertEqual(len(plugin._subtitle_webui.replies), 1)
        self.assertFalse(
            any("subtitle" in str(payload.get("type", "")) for payload in plugin._soullink_shell_runtime.payloads)
        )

    async def test_hub_speech_coordination_is_skipped_when_only_self_is_in_hub(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = LiveAdapterSettings.model_validate(
            {
                "plugin": {"enabled": True},
                "identity": {"bot_user_id": "maibot-live", "bot_nickname": "MaiBot Live"},
                "hub_input": {
                    "enabled": True,
                    "use_as_primary_source": True,
                    "speech_coordination_enabled": True,
                },
                "hub_output": {"client_id": "maibot-r-devold", "bot_name": "MaiBot-r-devold"},
            }
        )
        client = _HubInputClientStub(response={"granted": False})
        plugin._hub_input_client = client
        plugin._hub_participants = [
            {
                "client_id": "maibot-r-devold",
                "bot_name": "MaiBot-r-devold",
                "forward_user_id": "neuro-sama",
                "forward_username": "Neuro-sama",
            }
        ]

        lease = await plugin._acquire_hub_speech_turn(
            settings=settings,
            source_platform=PLATFORM_NAME,
            metadata={},
            reply_text="hello",
            expected_duration_ms=1200,
        )

        self.assertIsNone(lease)
        self.assertEqual(client.requests, [])

    async def test_hub_speech_coordination_still_requests_when_other_clients_are_present(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        settings = LiveAdapterSettings.model_validate(
            {
                "plugin": {"enabled": True},
                "identity": {"bot_user_id": "maibot-live", "bot_nickname": "MaiBot Live"},
                "hub_input": {
                    "enabled": True,
                    "use_as_primary_source": True,
                    "speech_coordination_enabled": True,
                },
                "hub_output": {"client_id": "maibot-r-devold", "bot_name": "MaiBot-r-devold"},
            }
        )
        client = _HubInputClientStub(response={"granted": True})
        plugin._hub_input_client = client
        plugin._hub_participants = [
            {
                "client_id": "maibot-r-devold",
                "bot_name": "MaiBot-r-devold",
                "forward_user_id": "neuro-sama",
                "forward_username": "Neuro-sama",
            },
            {
                "client_id": "other-bot",
                "bot_name": "Other Bot",
                "forward_user_id": "other-bot",
                "forward_username": "Other Bot",
            },
        ]

        lease = await plugin._acquire_hub_speech_turn(
            settings=settings,
            source_platform=PLATFORM_NAME,
            metadata={},
            reply_text="hello",
            expected_duration_ms=1200,
        )

        self.assertIsNotNone(lease)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0]["client_id"], "maibot-r-devold")


def _completed_future(value):
    async def _coro():
        return value

    return _coro()


async def _await_event(event: asyncio.Event) -> bool:
    await event.wait()
    return True
