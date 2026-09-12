import asyncio
import unittest
import importlib.util
import sys

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from . import _host_bootstrap  # noqa: F401

from plugins.maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from plugins.maibot_bilibili_live_adapter_copy.plugin import (
    BilibiliLiveAdapterPlugin,
    PendingSoundboardTrigger,
    _filter_unavailable_tool_definitions,
    _inject_soundboard_tool_hints,
    _sanitize_soundboard_reply_segment_text,
    _should_auto_open_soundboard_webui,
)
from plugins.maibot_bilibili_live_adapter_copy.soundboard import (
    SoundboardService,
    list_soundboard_cues,
    load_soundboard_cues_from_files,
    match_soundboard_keyword,
    resolve_soundboard_cached_audio_path,
    resolve_soundboard_cue,
)
from plugins.maibot_bilibili_live_adapter_copy.soundboard_selection_client import SoundboardAutoSelectLLMResult


def tool_definition(name: str) -> dict:
    return {"type": "function", "function": {"name": name}}


async def _async_noop(*args, **kwargs) -> None:
    del args, kwargs


def _write_soundboard_asset(base_dir: Path, cue_id: str, relative_name: str = "audio.wav", data: bytes = b"wav") -> Path:
    cue_dir = base_dir / "cues" / cue_id
    cue_dir.mkdir(parents=True, exist_ok=True)
    asset_path = cue_dir / relative_name
    asset_path.write_bytes(data)
    return asset_path


_IMPORT_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "import_soundboard_cue.py"
)
_IMPORT_SCRIPT_SPEC = importlib.util.spec_from_file_location("soundboard_import_script", _IMPORT_SCRIPT_PATH)
assert _IMPORT_SCRIPT_SPEC is not None and _IMPORT_SCRIPT_SPEC.loader is not None
_IMPORT_SCRIPT_MODULE = importlib.util.module_from_spec(_IMPORT_SCRIPT_SPEC)
sys.modules[_IMPORT_SCRIPT_SPEC.name] = _IMPORT_SCRIPT_MODULE
_IMPORT_SCRIPT_SPEC.loader.exec_module(_IMPORT_SCRIPT_MODULE)


class _SoundboardServiceStub:
    def __init__(self, response: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.response = dict(response or {"success": True, "cue_id": "laugh"})

    async def trigger(
        self,
        cue: str,
        *,
        repeat_count: int = 1,
        reason: str = "",
        source_text: str = "",
        triggered_by: str = "",
    ) -> dict:
        self.calls.append(
            {
                "method": "trigger",
                "cue": cue,
                "repeat_count": repeat_count,
                "reason": reason,
                "source_text": source_text,
                "triggered_by": triggered_by,
            }
        )
        return dict(self.response)

    async def trigger_for_text(self, text: str, *, reason: str = "", triggered_by: str = "") -> dict:
        self.calls.append(
            {
                "method": "trigger_for_text",
                "text": text,
                "reason": reason,
                "triggered_by": triggered_by,
            }
        )
        return dict(self.response)

    async def wait_until_idle(self, *, timeout_sec: float | None = None) -> bool:
        self.calls.append({"method": "wait_until_idle", "timeout_sec": timeout_sec})
        return True


class SoundboardConfigTest(unittest.TestCase):
    def test_auto_open_guard_skips_duplicate_url(self) -> None:
        self.assertTrue(_should_auto_open_soundboard_webui(True, "http://127.0.0.1:18184/", ""))
        self.assertFalse(
            _should_auto_open_soundboard_webui(
                True,
                "http://127.0.0.1:18184/",
                "http://127.0.0.1:18184/",
            )
        )
        self.assertFalse(_should_auto_open_soundboard_webui(False, "http://127.0.0.1:18184/", ""))

    def test_keyword_matching_prefers_priority(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "soundboard": {
                    "enabled": True,
                    "cues": [
                        {
                            "id": "low",
                            "keywords": ["wow"],
                            "priority": 1,
                        },
                        {
                            "id": "high",
                            "keywords": ["wow"],
                            "priority": 9,
                        },
                    ],
                }
            }
        )

        matched = match_soundboard_keyword(settings.soundboard, "wow that was close")

        self.assertIsNotNone(matched)
        self.assertEqual(matched.cue_id, "high")

    def test_ascii_contains_keyword_uses_word_boundaries(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "soundboard": {
                    "enabled": True,
                    "cues": [
                        {
                            "id": "aughhhh_snore",
                            "keywords": ["augh"],
                        }
                    ],
                }
            }
        )

        matched_false_positive = match_soundboard_keyword(settings.soundboard, "your code caught up with me")
        matched_real_keyword = match_soundboard_keyword(settings.soundboard, "augh, here we go again")

        self.assertIsNone(matched_false_positive)
        self.assertIsNotNone(matched_real_keyword)
        self.assertEqual(matched_real_keyword.cue_id, "aughhhh_snore")

    def test_resolve_cue_by_id_and_lists_public_metadata(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "cues": [
                            {
                                "id": "laugh",
                                "label": "Laugh",
                                "keywords": ["lol"],
                                "audio_path": "laugh.wav",
                                "media_path": "laugh.gif",
                                "usage_hint": "Use when the room is laughing at a fail.",
                            }
                        ],
                    }
                }
            )

            resolved = resolve_soundboard_cue(settings.soundboard, "laugh", plugin_dir=temp_dir)
            public_cues = list_soundboard_cues(settings.soundboard, plugin_dir=temp_dir)

            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.cue_id, "laugh")
            self.assertEqual(public_cues[0]["id"], "laugh")
            self.assertEqual(public_cues[0]["label"], "Laugh")
            self.assertEqual(public_cues[0]["usage_hint"], "Use when the room is laughing at a fail.")
            self.assertTrue(public_cues[0]["has_media"])
            self.assertEqual(public_cues[0]["media_kind"], "image")
            self.assertNotIn("effect", public_cues[0])

    def test_soundboard_auto_discovers_cue_toml_files(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            cue_dir = soundboard_dir / "cues" / "laugh"
            cue_dir.mkdir(parents=True, exist_ok=True)
            (cue_dir / "audio.wav").write_bytes(b"wav")
            (cue_dir / "media.mp4").write_bytes(b"mp4")
            (cue_dir / "cue.toml").write_text(
                "\n".join(
                    [
                        'id = "laugh"',
                        'label = "Laugh"',
                        'keywords = ["lol"]',
                        'audio_path = "audio.wav"',
                        'media_path = "media.mp4"',
                        'usage_hint = "Use when chat is laughing."',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                        "cues": [],
                    }
                }
            )

            hydrated = load_soundboard_cues_from_files(settings.soundboard, plugin_dir=temp_dir)
            resolved = resolve_soundboard_cue(settings.soundboard, "laugh", plugin_dir=temp_dir)

            self.assertEqual(len(hydrated.cues), 1)
            self.assertEqual(hydrated.cues[0].id, "laugh")
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.audio_path, (cue_dir / "audio.wav").resolve())
            self.assertEqual(resolved.media_path, (cue_dir / "media.mp4").resolve())

    def test_video_media_without_audio_file_uses_embedded_browser_audio(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            media_path = temp_dir / "soundboard" / "cues" / "pipe" / "media.mp4"
            media_path.parent.mkdir(parents=True, exist_ok=True)
            media_path.write_bytes(b"mp4")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "audio_base_dir": str(temp_dir / "soundboard"),
                        "browser_audio_enabled": True,
                        "cues": [
                            {
                                "id": "pipe",
                                "label": "Pipe",
                                "media_path": "cues/pipe/media.mp4",
                                "play_audio": True,
                            }
                        ],
                    }
                }
            )
            service = SoundboardService(settings.soundboard, plugin_dir=temp_dir)
            resolved = resolve_soundboard_cue(settings.soundboard, "pipe", plugin_dir=temp_dir)

            self.assertIsNotNone(resolved)
            payload = service._build_trigger_payload(
                resolved,
                reason="tool",
                source_text="",
                triggered_by="test",
                repeat_count=3,
            )

            self.assertEqual(payload["audio_url"], "")
            self.assertEqual(payload["media_url"], "/media/pipe")
            self.assertEqual(payload["media_kind"], "video")
            self.assertTrue(payload["media_audio_enabled"])
            self.assertEqual(payload["repeat_count"], 3)
            self.assertNotIn("effect", payload)
            self.assertNotIn("effect_text", payload)

    def test_video_media_prefers_cached_extracted_audio_file(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            cue_dir = temp_dir / "soundboard" / "cues" / "pipe"
            media_path = cue_dir / "media.mp4"
            audio_path = cue_dir / "audio.wav"
            cue_dir.mkdir(parents=True, exist_ok=True)
            media_path.write_bytes(b"mp4")
            audio_path.write_bytes(b"wav")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "audio_base_dir": str(temp_dir / "soundboard"),
                        "browser_audio_enabled": True,
                        "cues": [
                            {
                                "id": "pipe",
                                "label": "Pipe",
                                "media_path": "cues/pipe/media.mp4",
                                "play_audio": True,
                            }
                        ],
                    }
                }
            )

            resolved_audio = resolve_soundboard_cached_audio_path(
                settings.soundboard,
                settings.soundboard.cues[0],
                plugin_dir=temp_dir,
            )

            self.assertEqual(resolved_audio, audio_path.resolve())

    def test_soundboard_stage_direction_strip_preserves_remaining_speech(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "soundboard": {
                    "enabled": True,
                    "cues": [
                        {
                            "id": "metal_pipe_drop",
                            "label": "Metal Pipe Drop",
                        }
                    ],
                }
            }
        )

        sanitized = _sanitize_soundboard_reply_segment_text(
            "*plays metal pipe drop sound effect* I guess I'm a catgirl now.",
            settings=settings,
            planned_triggers=None,
        )

        self.assertEqual(sanitized, "I guess I'm a catgirl now.")

    def test_soundboard_stage_direction_strip_can_leave_empty_segment(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "soundboard": {
                    "enabled": True,
                    "cues": [
                        {
                            "id": "metal_pipe_drop",
                            "label": "Metal Pipe Drop",
                        }
                    ],
                }
            }
        )

        sanitized = _sanitize_soundboard_reply_segment_text(
            "*播放金属管掉落音效*",
            settings=settings,
            planned_triggers=[PendingSoundboardTrigger("t1", "metal_pipe_drop", 1, "tool", "", "tool", 0.0)],
        )

        self.assertEqual(sanitized, "")

    def test_tool_hints_add_enum_and_usage_text(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "laugh")
            _write_soundboard_asset(soundboard_dir, "surprise")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "expose_tool": True,
                        "audio_base_dir": str(soundboard_dir),
                        "cues": [
                            {
                                "id": "laugh",
                                "label": "Laugh",
                                "audio_path": "cues/laugh/audio.wav",
                                "usage_hint": "Use when chat is laughing at a miss.",
                            },
                            {
                                "id": "surprise",
                                "label": "Surprise",
                                "audio_path": "cues/surprise/audio.wav",
                                "usage_hint": "Use when something wild happens on stream.",
                            },
                        ],
                    }
                }
            )

            hinted = _inject_soundboard_tool_hints(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "play_sound_effect",
                            "description": "Play a configured live soundboard cue.",
                            "parameters": {
                                "type": "object",
                                "properties": {"cue": {"type": "string"}},
                                "required": ["cue"],
                            },
                        },
                    }
                ],
                settings.soundboard,
            )

            function = hinted[0]["function"]
            self.assertIn("laugh: Use when chat is laughing at a miss.", function["description"])
            self.assertEqual(function["parameters"]["properties"]["cue"]["enum"], ["laugh", "surprise"])
            self.assertIn(
                "Options: laugh: Use when chat is laughing at a miss.",
                function["parameters"]["properties"]["cue"]["description"],
            )

    def test_auto_tool_hints_add_available_reaction_text(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "metal_pipe_drop")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "expose_auto_tool": True,
                        "audio_base_dir": str(soundboard_dir),
                        "cues": [
                            {
                                "id": "metal_pipe_drop",
                                "label": "Pipe Drop",
                                "audio_path": "cues/metal_pipe_drop/audio.wav",
                                "usage_hint": "Use when something lands with a dramatic thud.",
                            }
                        ],
                    }
                }
            )

            hinted = _inject_soundboard_tool_hints(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "send_sound_effect",
                            "description": "Automatically choose a sound effect.",
                            "parameters": {
                                "type": "object",
                                "properties": {"intent": {"type": "string"}},
                            },
                        },
                    }
                ],
                settings.soundboard,
            )

            function = hinted[0]["function"]
            self.assertIn("metal_pipe_drop: Use when something lands with a dramatic thud.", function["description"])
            self.assertIn(
                "Available reactions: metal_pipe_drop: Use when something lands with a dramatic thud.",
                function["parameters"]["properties"]["intent"]["description"],
            )

    def test_tool_filtering_hides_soundboard_tool_when_disabled(self) -> None:
        settings = LiveAdapterSettings()
        settings.soundboard.enabled = False

        filtered = _filter_unavailable_tool_definitions(
            [
                tool_definition("play_sound_effect"),
                tool_definition("send_sound_effect"),
                tool_definition("unrelated_tool"),
            ],
            settings,
        )

        self.assertEqual([item["function"]["name"] for item in filtered], ["unrelated_tool"])

    def test_tool_filtering_keeps_soundboard_tools_when_enabled(self) -> None:
        settings = LiveAdapterSettings()
        settings.soundboard.enabled = True
        settings.soundboard.expose_tool = True
        settings.soundboard.expose_auto_tool = True

        filtered = _filter_unavailable_tool_definitions(
            [
                tool_definition("play_sound_effect"),
                tool_definition("send_sound_effect"),
                tool_definition("unrelated_tool"),
            ],
            settings,
        )

        self.assertEqual(
            [item["function"]["name"] for item in filtered],
            ["play_sound_effect", "send_sound_effect", "unrelated_tool"],
        )


class SoundboardPluginTest(unittest.IsolatedAsyncioTestCase):
    async def test_soundboard_service_start_keeps_queue_alive_when_webui_fails(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "soundboard": {
                    "enabled": True,
                    "audio_playback_enabled": False,
                    "webui_enabled": True,
                }
            }
        )
        service = SoundboardService(settings.soundboard)

        async def fake_start_webui() -> None:
            raise OSError("bind failed")

        service._start_webui = fake_start_webui  # type: ignore[method-assign]

        await service.start()

        self.assertIsNotNone(service._queue_worker_task)
        self.assertFalse(service._queue_worker_task.done())

        await service.stop()

    async def test_browser_audio_cue_skips_local_audio_playback_task(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            cue_dir = soundboard_dir / "cues" / "pipe"
            cue_dir.mkdir(parents=True, exist_ok=True)
            (cue_dir / "audio.wav").write_bytes(b"wav")
            (cue_dir / "media.mp4").write_bytes(b"mp4")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                        "webui_enabled": True,
                        "browser_audio_enabled": True,
                        "audio_playback_enabled": True,
                        "cues": [
                            {
                                "id": "pipe",
                                "label": "Pipe",
                                "audio_path": "cues/pipe/audio.wav",
                                "media_path": "cues/pipe/media.mp4",
                                "show_effect": True,
                                "play_audio": True,
                            }
                        ],
                    }
                }
            )
            service = SoundboardService(settings.soundboard, plugin_dir=temp_dir)
            resolved = resolve_soundboard_cue(settings.soundboard, "pipe", plugin_dir=temp_dir)
            assert resolved is not None
            queued = type("Queued", (), {
                "resolved": resolved,
                "reason": "tool",
                "source_text": "",
                "triggered_by": "tool",
                "repeat_count": 1,
            })()
            started_local_audio = False

            def fake_start_audio(*args, **kwargs):
                nonlocal started_local_audio
                started_local_audio = True
                return None

            service._start_audio_playback_task = fake_start_audio  # type: ignore[method-assign]
            service._broadcast = AsyncMock()  # type: ignore[method-assign]
            service._wait_for_trigger_completion = AsyncMock()  # type: ignore[method-assign]

            await service._play_queued_trigger(queued)  # type: ignore[arg-type]

            self.assertFalse(started_local_audio)

    async def test_explicit_trigger_queues_repeated_manual_requests(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "laugh")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                        "global_cooldown_sec": 30.0,
                        "cues": [
                            {
                                "id": "laugh",
                                "label": "Laugh",
                                "audio_path": "cues/laugh/audio.wav",
                                "cooldown_sec": 30.0,
                            }
                        ],
                    }
                }
            )
            service = SoundboardService(settings.soundboard)
            played_cues: list[str] = []
            queue_drained = asyncio.Event()

            async def fake_play(queued_trigger) -> None:
                played_cues.append(queued_trigger.resolved.cue_id)
                if len(played_cues) >= 2:
                    queue_drained.set()

            service._play_queued_trigger = fake_play  # type: ignore[method-assign]

            first = await service.trigger("laugh", repeat_count=1, triggered_by="tool")
            second = await service.trigger("laugh", repeat_count=1, triggered_by="tool")
            await asyncio.wait_for(queue_drained.wait(), timeout=1.0)

            self.assertTrue(first["success"])
            self.assertTrue(second["success"])
            self.assertTrue(first["queued"])
            self.assertTrue(second["queued"])
            self.assertEqual(played_cues, ["laugh", "laugh"])

            await service.stop()

    async def test_render_prepared_local_reply_waits_for_soundboard_before_publish(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "soundboard": {
                    "enabled": True,
                }
            }
        )
        plugin = BilibiliLiveAdapterPlugin()
        plugin._soundboard = _SoundboardServiceStub()  # type: ignore[assignment]
        events: list[str] = []

        async def fake_publish(*args, **kwargs):
            events.append("publish")
            return False, False

        async def fake_wait(*, settings=None, timeout_sec: float | None = None) -> bool:
            del settings
            del timeout_sec
            events.append("wait")
            return True

        plugin._publish_reply_to_webui = fake_publish  # type: ignore[method-assign]
        plugin._create_audio_playback_task = lambda *args, **kwargs: None  # type: ignore[method-assign]
        plugin._build_local_voice_echo_start_callback = lambda *args, downstream=None: downstream  # type: ignore[method-assign]
        plugin._trigger_soundboard_for_bot_output = AsyncMock(return_value=False)  # type: ignore[method-assign]
        plugin._wait_for_soundboard_idle = fake_wait  # type: ignore[method-assign]

        result = await plugin._render_prepared_local_reply(
            "source text",
            speech_text="hello there",
            subtitle_text="hello there",
            audio_timeline=None,
            synthesized_speech=None,
            settings=settings,
            source_platform="bilibili_live",
            metadata={},
            allow_soundboard_bot_output_trigger=False,
            wait_for_soundboard_before_reply=True,
        )

        self.assertEqual(events, ["wait", "publish"])
        self.assertEqual(result["subtitle_text"], "hello there")

    async def test_keyword_trigger_still_respects_cooldown_before_enqueue(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "laugh")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                        "keyword_triggers_enabled": True,
                        "global_cooldown_sec": 30.0,
                        "cues": [
                            {
                                "id": "laugh",
                                "label": "Laugh",
                                "audio_path": "cues/laugh/audio.wav",
                                "keywords": ["lol"],
                                "cooldown_sec": 30.0,
                            }
                        ],
                    }
                }
            )
            service = SoundboardService(settings.soundboard)
            service._play_queued_trigger = _async_noop  # type: ignore[method-assign]

            first = await service.trigger_for_text("lol", reason="bot_output_keyword", triggered_by="bot_output")
            second = await service.trigger_for_text("lol", reason="bot_output_keyword", triggered_by="bot_output")

            self.assertIsNotNone(first)
            self.assertTrue(first["success"])
            self.assertIsNotNone(second)
            self.assertFalse(second["success"])
            self.assertTrue(second["skipped"])
            self.assertEqual(second["reason"], "cooldown")

            await service.stop()

    async def test_play_sound_effect_tool_queues_trigger_for_reply_render(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "laugh")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                        "cues": [
                            {
                                "id": "laugh",
                                "label": "Laugh",
                                "audio_path": "cues/laugh/audio.wav",
                            }
                        ],
                    }
                }
            )
            service = _SoundboardServiceStub()
            plugin = BilibiliLiveAdapterPlugin()
            plugin._soundboard = service  # type: ignore[assignment]
            plugin._load_settings = lambda: settings  # type: ignore[method-assign]
            plugin._schedule_soundboard_trigger_fallback = lambda plan, settings: None  # type: ignore[method-assign]

            result = await plugin.play_sound_effect("laugh", repeat_count=2, reason="chat laughed", text="lol")

            self.assertTrue(result["success"])
            self.assertTrue(result["queued"])
            self.assertEqual(result["cue_id"], "laugh")
            pending_plans = plugin._consume_pending_soundboard_triggers()
            self.assertEqual(len(pending_plans), 1)
            self.assertEqual(pending_plans[0].cue, "laugh")
            self.assertEqual(pending_plans[0].repeat_count, 2)
            self.assertEqual(service.calls, [])

    async def test_send_sound_effect_auto_selects_cue_and_reuses_existing_queue_logic(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "laugh")
            _write_soundboard_asset(soundboard_dir, "metal_pipe_drop")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                        "cues": [
                            {
                                "id": "laugh",
                                "label": "Laugh",
                                "audio_path": "cues/laugh/audio.wav",
                                "usage_hint": "Use when chat is laughing at a miss.",
                            },
                            {
                                "id": "metal_pipe_drop",
                                "label": "Pipe Drop",
                                "audio_path": "cues/metal_pipe_drop/audio.wav",
                                "usage_hint": "Use when something lands with a dramatic thud.",
                                "keywords": ["metal pipe drop", "dramatic thud"],
                            },
                        ],
                    }
                }
            )
            service = _SoundboardServiceStub()
            plugin = BilibiliLiveAdapterPlugin()
            plugin._soundboard = service  # type: ignore[assignment]
            plugin._load_settings = lambda: settings  # type: ignore[method-assign]
            plugin._schedule_soundboard_trigger_fallback = lambda plan, settings: None  # type: ignore[method-assign]

            result = await plugin.send_sound_effect(
                intent="dramatic thud",
                repeat_count=2,
                reason="something landed hard",
                text="that landed with a dramatic thud",
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["cue_id"], "metal_pipe_drop")
            self.assertEqual(result["selected_by"], "auto_heuristic")
            pending_plans = plugin._consume_pending_soundboard_triggers()
            self.assertEqual(len(pending_plans), 1)
            self.assertEqual(pending_plans[0].cue, "metal_pipe_drop")
            self.assertEqual(pending_plans[0].repeat_count, 2)
            self.assertEqual(service.calls, [])

    async def test_send_sound_effect_prefers_llm_selector_when_enabled(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "laugh")
            _write_soundboard_asset(soundboard_dir, "metal_pipe_drop")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                        "auto_select_llm": {
                            "enabled": True,
                            "api_provider": "DeepSeek",
                            "model_name": "deepseek-v4-flash",
                            "model_identifier": "deepseek-v4-flash",
                        },
                        "cues": [
                            {
                                "id": "laugh",
                                "label": "Laugh",
                                "audio_path": "cues/laugh/audio.wav",
                                "usage_hint": "Use when chat is laughing at a miss.",
                            },
                            {
                                "id": "metal_pipe_drop",
                                "label": "Pipe Drop",
                                "audio_path": "cues/metal_pipe_drop/audio.wav",
                                "usage_hint": "Use when something lands with a dramatic thud.",
                                "keywords": ["metal pipe drop", "dramatic thud"],
                            },
                        ],
                    }
                }
            )
            service = _SoundboardServiceStub()
            plugin = BilibiliLiveAdapterPlugin()
            plugin._soundboard = service  # type: ignore[assignment]
            plugin._load_settings = lambda: settings  # type: ignore[method-assign]
            plugin._schedule_soundboard_trigger_fallback = lambda plan, settings: None  # type: ignore[method-assign]

            with patch(
                "plugins.maibot_bilibili_live_adapter_copy.plugin.SoundboardAutoSelectClient.select_cue",
                new=AsyncMock(
                    return_value=SoundboardAutoSelectLLMResult(
                        cue_id="metal_pipe_drop",
                        reason="Best fit for a dramatic thud.",
                    )
                ),
            ) as mocked_select:
                result = await plugin.send_sound_effect(
                    intent="dramatic thud",
                    repeat_count=2,
                    reason="something landed hard",
                    text="that landed with a dramatic thud",
                )

            self.assertTrue(result["success"])
            self.assertEqual(result["cue_id"], "metal_pipe_drop")
            self.assertEqual(result["selected_by"], "auto_llm")
            self.assertEqual(result["selection_reason"], "Best fit for a dramatic thud.")
            mocked_select.assert_awaited_once()
            pending_plans = plugin._consume_pending_soundboard_triggers()
            self.assertEqual(len(pending_plans), 1)
            self.assertEqual(pending_plans[0].cue, "metal_pipe_drop")
            self.assertEqual(service.calls, [])

    async def test_send_sound_effect_returns_available_cues_when_auto_selection_fails(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "laugh")
            _write_soundboard_asset(soundboard_dir, "metal_pipe_drop")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                        "cues": [
                            {
                                "id": "laugh",
                                "label": "Laugh",
                                "audio_path": "cues/laugh/audio.wav",
                                "usage_hint": "Use when chat is laughing at a miss.",
                            },
                            {
                                "id": "metal_pipe_drop",
                                "label": "Pipe Drop",
                                "audio_path": "cues/metal_pipe_drop/audio.wav",
                                "usage_hint": "Use when something lands with a dramatic thud.",
                            },
                        ],
                    }
                }
            )
            plugin = BilibiliLiveAdapterPlugin()
            plugin._load_settings = lambda: settings  # type: ignore[method-assign]

            result = await plugin.send_sound_effect(
                intent="gentle applause",
                reason="this was nice and polite",
                text="soft clap",
            )

            self.assertFalse(result["success"])
            self.assertEqual(result["error"], "no matching sound cue found for automatic selection")
            self.assertEqual([cue["id"] for cue in result["available_cues"]], ["laugh", "metal_pipe_drop"])

    def test_build_soundboard_segment_schedule_prefers_matching_replyer_segment(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "soundboard": {
                    "enabled": True,
                    "cues": [
                        {
                            "id": "metal_pipe_drop",
                            "label": "钢管落地",
                            "keywords": ["钢管落地"],
                        }
                    ],
                }
            }
        )
        plugin = BilibiliLiveAdapterPlugin()
        schedule = plugin._build_soundboard_segment_schedule(
            plans=[
                PendingSoundboardTrigger(
                    trigger_id="t1",
                    cue="metal_pipe_drop",
                    repeat_count=1,
                    reason="",
                    source_text="",
                    triggered_by="maibot_tool",
                    created_at=0.0,
                )
            ],
            segments=["Okay", "hold on", "*plays metal pipe drop sound effect*"],
            settings=settings,
        )

        self.assertEqual(list(schedule), [3])
        self.assertEqual(schedule[3][0].cue, "metal_pipe_drop")

    def test_build_soundboard_segment_schedule_falls_back_to_middle_segment(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "soundboard": {
                    "enabled": True,
                    "cues": [
                        {
                            "id": "metal_pipe_drop",
                            "label": "钢管落地",
                            "keywords": ["钢管落地"],
                        }
                    ],
                }
            }
        )
        plugin = BilibiliLiveAdapterPlugin()
        schedule = plugin._build_soundboard_segment_schedule(
            plans=[
                PendingSoundboardTrigger(
                    trigger_id="t1",
                    cue="metal_pipe_drop",
                    repeat_count=1,
                    reason="",
                    source_text="",
                    triggered_by="maibot_tool",
                    created_at=0.0,
                )
            ],
            segments=["one", "two", "three", "four"],
            settings=settings,
        )

        self.assertEqual(list(schedule), [2])
        self.assertEqual(schedule[2][0].cue, "metal_pipe_drop")

    def test_tool_hints_hide_cues_without_assets(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "real_cue")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "expose_tool": True,
                        "audio_base_dir": str(soundboard_dir),
                        "cues": [
                            {
                                "id": "ghost_cue",
                                "label": "Ghost",
                                "audio_path": "cues/ghost_cue/audio.wav",
                                "usage_hint": "Should never be shown.",
                            },
                            {
                                "id": "real_cue",
                                "label": "Real",
                                "audio_path": "cues/real_cue/audio.wav",
                                "usage_hint": "Use this one.",
                            },
                        ],
                    }
                }
            )

            hinted = _inject_soundboard_tool_hints(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "play_sound_effect",
                            "description": "Play a configured live soundboard cue.",
                            "parameters": {
                                "type": "object",
                                "properties": {"cue": {"type": "string"}},
                                "required": ["cue"],
                            },
                        },
                    }
                ],
                settings.soundboard,
            )

            cue_enum = hinted[0]["function"]["parameters"]["properties"]["cue"]["enum"]
            self.assertEqual(cue_enum, ["real_cue"])

    async def test_trigger_rejects_cues_without_assets(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "real_cue")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                        "cues": [
                            {
                                "id": "ghost_cue",
                                "label": "Ghost",
                                "audio_path": "cues/ghost_cue/audio.wav",
                            },
                            {
                                "id": "real_cue",
                                "label": "Real",
                                "audio_path": "cues/real_cue/audio.wav",
                            },
                        ],
                    }
                }
            )
            service = SoundboardService(settings.soundboard)

            result = await service.trigger("ghost_cue", triggered_by="tool")

            self.assertFalse(result["success"])
            self.assertEqual(result["error"], "unknown sound cue: ghost_cue")
            self.assertEqual([cue["id"] for cue in result["available_cues"]], ["real_cue"])

            await service.stop()

    async def test_keyword_event_dispatches_to_service(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "soundboard": {
                    "enabled": True,
                    "keyword_triggers_enabled": True,
                }
            }
        )
        service = _SoundboardServiceStub()
        plugin = BilibiliLiveAdapterPlugin()
        plugin._soundboard = service  # type: ignore[assignment]
        plugin._load_settings = lambda: settings  # type: ignore[method-assign]

        result = await plugin._handle_soundboard_keyword_event({"type": "bot_output", "text": "lol", "user_id": "u1"})

        self.assertTrue(result)
        self.assertEqual(service.calls[0]["method"], "trigger_for_text")
        self.assertEqual(service.calls[0]["text"], "lol")
        self.assertEqual(service.calls[0]["reason"], "bot_output_keyword")
        self.assertEqual(service.calls[0]["triggered_by"], "bot_output")

    async def test_bot_output_explicit_cue_mention_prefers_direct_trigger(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "metal_pipe_drop")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "keyword_triggers_enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                        "cues": [
                            {
                                "id": "metal_pipe_drop",
                                "label": "Pipe Drop",
                                "audio_path": "cues/metal_pipe_drop/audio.wav",
                            }
                        ],
                    }
                }
            )
            service = _SoundboardServiceStub(response={"success": True, "cue_id": "metal_pipe_drop"})
            plugin = BilibiliLiveAdapterPlugin()
            plugin._soundboard = service  # type: ignore[assignment]
            plugin._load_settings = lambda: settings  # type: ignore[method-assign]

            result = await plugin._trigger_soundboard_for_bot_output(source_text="okay, play metal pipe drop now")

            self.assertTrue(result)
            self.assertEqual(service.calls[0]["method"], "trigger")
            self.assertEqual(service.calls[0]["cue"], "metal_pipe_drop")
            self.assertEqual(service.calls[0]["reason"], "bot_output_mention")

    async def test_danmaku_event_does_not_trigger_keyword_soundboard_match(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "soundboard": {
                    "enabled": True,
                    "keyword_triggers_enabled": True,
                }
            }
        )
        service = _SoundboardServiceStub()
        plugin = BilibiliLiveAdapterPlugin()
        plugin._soundboard = service  # type: ignore[assignment]
        plugin._load_settings = lambda: settings  # type: ignore[method-assign]

        result = await plugin._handle_soundboard_keyword_event({"type": "danmaku", "text": "lol", "user_id": "u1"})

        self.assertFalse(result)
        self.assertEqual(service.calls, [])

    async def test_danmaku_explicit_cue_mention_queues_soundboard_plan(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "metal_pipe_drop")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "keyword_triggers_enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                        "cues": [
                            {
                                "id": "metal_pipe_drop",
                                "label": "Pipe Drop",
                                "audio_path": "cues/metal_pipe_drop/audio.wav",
                            }
                        ],
                    }
                }
            )
            service = _SoundboardServiceStub()
            plugin = BilibiliLiveAdapterPlugin()
            plugin._soundboard = service  # type: ignore[assignment]
            plugin._load_settings = lambda: settings  # type: ignore[method-assign]
            plugin._schedule_soundboard_trigger_fallback = lambda plan, settings: None  # type: ignore[method-assign]

            result = await plugin._handle_soundboard_keyword_event(
                {"type": "danmaku", "text": "放个 metal pipe drop", "user_id": "u1"}
            )

            self.assertTrue(result)
            self.assertEqual(service.calls, [])
            pending_plans = plugin._consume_pending_soundboard_triggers()
            self.assertEqual(len(pending_plans), 1)
            self.assertEqual(pending_plans[0].cue, "metal_pipe_drop")
            self.assertEqual(pending_plans[0].triggered_by, "danmaku_mention")

    async def test_generic_danmaku_soundboard_request_marks_event_for_forced_reply(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "soundboard": {
                    "enabled": True,
                    "keyword_triggers_enabled": True,
                }
            }
        )
        plugin = BilibiliLiveAdapterPlugin()
        plugin._load_settings = lambda: settings  # type: ignore[method-assign]

        event = {"type": "danmaku", "text": "来个音效", "user_id": "u1"}
        result = await plugin._handle_soundboard_keyword_event(event)

        self.assertTrue(result)
        self.assertTrue(event["_soundboard_request_detected"])
        self.assertEqual(event["_soundboard_request_mode"], "generic")
        self.assertEqual(event["_soundboard_request_text"], "来个音效")
        self.assertEqual(plugin._consume_pending_soundboard_triggers(), [])

    def test_reply_auto_soundboard_plan_targets_matching_segment(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "metal_pipe_drop")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "reply_auto_triggers_enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                        "cues": [
                            {
                                "id": "metal_pipe_drop",
                                "label": "Pipe Drop",
                                "audio_path": "cues/metal_pipe_drop/audio.wav",
                                "usage_hint": "Use when something lands with a dramatic thud.",
                            }
                        ],
                    }
                }
            )
            plugin = BilibiliLiveAdapterPlugin()
            plugin._load_settings = lambda: settings  # type: ignore[method-assign]

            plans = plugin._build_reply_auto_soundboard_plans(
                text="Okay. That landed with a dramatic thud. Moving on.",
                segments=["Okay.", "That landed with a dramatic thud.", "Moving on."],
                metadata={
                    "message_info": {
                        "additional_config": {
                            "live_event_type": "danmaku",
                            "soundboard_request_detected": True,
                            "soundboard_request_mode": "generic",
                            "soundboard_request_text": "来个 dramatic thud 的音效",
                        }
                    }
                },
                settings=settings,
                existing_plans=[],
            )

            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0].cue, "metal_pipe_drop")
            self.assertEqual(plans[0].triggered_by, "reply_auto_request")
            self.assertEqual(plans[0].target_segment_index, 2)

    def test_reply_auto_soundboard_skips_low_confidence_non_request_match(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "metal_pipe_drop")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "reply_auto_triggers_enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                        "cues": [
                            {
                                "id": "metal_pipe_drop",
                                "label": "Pipe Drop",
                                "audio_path": "cues/metal_pipe_drop/audio.wav",
                                "usage_hint": "Use when something lands with a dramatic thud.",
                            }
                        ],
                    }
                }
            )
            plugin = BilibiliLiveAdapterPlugin()
            plugin._load_settings = lambda: settings  # type: ignore[method-assign]

            plans = plugin._build_reply_auto_soundboard_plans(
                text="Okay. That landed with a dramatic thud. Moving on.",
                segments=["Okay.", "That landed with a dramatic thud.", "Moving on."],
                metadata={
                    "message_info": {
                        "additional_config": {
                            "live_event_type": "danmaku",
                        }
                    }
                },
                settings=settings,
                existing_plans=[],
            )

            self.assertEqual(plans, [])

    def test_reply_auto_soundboard_skips_when_reply_already_contains_directive(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            _write_soundboard_asset(soundboard_dir, "metal_pipe_drop")
            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "reply_auto_triggers_enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                        "cues": [
                            {
                                "id": "metal_pipe_drop",
                                "label": "Pipe Drop",
                                "audio_path": "cues/metal_pipe_drop/audio.wav",
                                "keywords": ["dramatic thud"],
                            }
                        ],
                    }
                }
            )
            plugin = BilibiliLiveAdapterPlugin()
            plugin._load_settings = lambda: settings  # type: ignore[method-assign]

            plans = plugin._build_reply_auto_soundboard_plans(
                text="*plays metal pipe drop sound effect*",
                segments=["*plays metal pipe drop sound effect*"],
                metadata={
                    "message_info": {
                        "additional_config": {
                            "live_event_type": "danmaku",
                        }
                    }
                },
                settings=settings,
                existing_plans=[],
            )

            self.assertEqual(plans, [])


class SoundboardImportScriptTest(unittest.TestCase):
    def test_import_script_copies_assets_and_updates_config(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            audio_path = temp_dir / "clip.wav"
            media_path = temp_dir / "clip.gif"
            audio_path.write_bytes(b"wav")
            media_path.write_bytes(b"gif")

            request = _IMPORT_SCRIPT_MODULE.CueImportRequest(
                cue_id="laugh",
                label="Laugh",
                audio_source=audio_path,
                media_source=media_path,
                usage_hint="Use when chat is laughing at a miss.",
                keywords=["lol", "哈哈"],
                match_mode="contains",
                priority=8,
                duration_ms=1500,
                cooldown_sec=4.0,
                volume=1.0,
                enabled=True,
                play_audio=True,
                show_effect=True,
            )
            result = _IMPORT_SCRIPT_MODULE.import_soundboard_cue(
                soundboard_dir=soundboard_dir,
                request=request,
            )

            cue_config_path = soundboard_dir / "cues" / "laugh" / "cue.toml"
            updated_text = cue_config_path.read_text(encoding="utf-8")
            self.assertIn('audio_path = "audio.wav"', updated_text)
            self.assertIn('media_path = "media.gif"', updated_text)
            self.assertIn('usage_hint = "Use when chat is laughing at a miss."', updated_text)
            self.assertTrue((soundboard_dir / "cues" / "laugh" / "audio.wav").exists())
            self.assertTrue((soundboard_dir / "cues" / "laugh" / "media.gif").exists())
            self.assertEqual(result["cue_id"], "laugh")
            self.assertEqual(result["cue_config_path"], str(cue_config_path))

    def test_import_script_persists_per_cue_volume(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            audio_path = temp_dir / "clip.wav"
            audio_path.write_bytes(b"wav")

            request = _IMPORT_SCRIPT_MODULE.CueImportRequest(
                cue_id="quiet_laugh",
                label="Quiet Laugh",
                audio_source=audio_path,
                media_source=None,
                usage_hint="Use when the laugh should stay in the background.",
                keywords=["quiet laugh"],
                match_mode="contains",
                priority=1,
                duration_ms=1200,
                cooldown_sec=2.0,
                volume=0.35,
                enabled=True,
                play_audio=True,
                show_effect=True,
            )
            result = _IMPORT_SCRIPT_MODULE.import_soundboard_cue(
                soundboard_dir=soundboard_dir,
                request=request,
            )

            cue_config_path = soundboard_dir / "cues" / "quiet_laugh" / "cue.toml"
            updated_text = cue_config_path.read_text(encoding="utf-8")
            self.assertIn('volume = 0.35', updated_text)
            self.assertEqual(result["volume"], 0.35)

            settings = LiveAdapterSettings.model_validate(
                {
                    "soundboard": {
                        "enabled": True,
                        "audio_base_dir": str(soundboard_dir),
                    }
                }
            )
            loaded_settings = load_soundboard_cues_from_files(settings.soundboard)
            self.assertEqual(len(loaded_settings.cues), 1)
            self.assertAlmostEqual(loaded_settings.cues[0].volume, 0.35)

    def test_import_script_accepts_video_only_cues(self) -> None:
        with TemporaryDirectory() as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            soundboard_dir = temp_dir / "soundboard"
            media_path = temp_dir / "clip.mp4"
            media_path.write_bytes(b"mp4")

            request = _IMPORT_SCRIPT_MODULE.CueImportRequest(
                cue_id="pipe",
                label="Pipe Drop",
                audio_source=None,
                media_source=media_path,
                usage_hint="Use when something lands with a dramatic thud.",
                keywords=["pipe"],
                match_mode="contains",
                priority=5,
                duration_ms=1600,
                cooldown_sec=4.0,
                volume=1.0,
                enabled=True,
                play_audio=True,
                show_effect=True,
            )
            result = _IMPORT_SCRIPT_MODULE.import_soundboard_cue(
                soundboard_dir=soundboard_dir,
                request=request,
            )

            cue_config_path = soundboard_dir / "cues" / "pipe" / "cue.toml"
            updated_text = cue_config_path.read_text(encoding="utf-8")
            self.assertIn('audio_path = ""', updated_text)
            self.assertIn('media_path = "media.mp4"', updated_text)
            self.assertTrue((soundboard_dir / "cues" / "pipe" / "media.mp4").exists())
            self.assertEqual(result["audio_path"], "")
            self.assertEqual(result["cue_id"], "pipe")

    def test_normalize_sources_promotes_video_to_media(self) -> None:
        audio_source, media_source = _IMPORT_SCRIPT_MODULE.normalize_sources(Path("clip.mp4"), None)

        self.assertIsNone(audio_source)
        self.assertEqual(media_source, Path("clip.mp4"))


if __name__ == "__main__":
    unittest.main()
