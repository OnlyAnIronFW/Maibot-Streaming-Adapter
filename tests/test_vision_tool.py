import unittest

from PIL import Image

import _host_bootstrap  # noqa: F401

from maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from maibot_bilibili_live_adapter_copy.vision_tool import (
    VisionDesktopInspector,
    compress_image_for_vision,
)


class VisionConfigTest(unittest.TestCase):
    def test_vision_defaults_to_fast_low_cost_bailian_model(self) -> None:
        settings = LiveAdapterSettings()

        self.assertFalse(settings.vision.enabled)
        self.assertTrue(settings.vision.expose_tool)
        self.assertEqual(settings.vision.api_provider, "BaiLian")
        self.assertEqual(settings.vision.model_identifier, "qwen3-vl-flash")
        self.assertFalse(settings.vision.command.enabled)
        self.assertEqual(settings.vision.command.prefix, "/vision")
        self.assertEqual(settings.vision.command.authorized_identities, [])
        self.assertTrue(settings.vision.command.allow_hub_local_input)
        self.assertEqual(settings.vision.command.poll_interval_sec, 8.0)
        self.assertEqual(settings.vision.command.stop_aliases, ["stop", "off"])
        self.assertLessEqual(settings.vision.max_image_edge_px, 960)
        self.assertLessEqual(settings.vision.jpeg_quality, 70)

    def test_vision_config_clamps_compression_settings(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "vision": {
                    "enabled": True,
                    "max_image_edge_px": "128",
                    "jpeg_quality": "101",
                    "timeout_sec": "0",
                }
            }
        )

        self.assertTrue(settings.vision.enabled)
        self.assertEqual(settings.vision.max_image_edge_px, 256)
        self.assertEqual(settings.vision.jpeg_quality, 95)
        self.assertEqual(settings.vision.timeout_sec, 20.0)

    def test_vision_command_normalizes_prefix_and_identity_list(self) -> None:
        settings = LiveAdapterSettings.model_validate(
            {
                "vision": {
                    "command": {
                        "enabled": True,
                        "prefix": "vision",
                        "authorized_identities": [" Vedal ", "", "27853192"],
                        "poll_interval_sec": "2.5",
                        "stop_aliases": [" stop ", "", "OFF"],
                    }
                }
            }
        )

        self.assertTrue(settings.vision.command.enabled)
        self.assertEqual(settings.vision.command.prefix, "/vision")
        self.assertEqual(settings.vision.command.authorized_identities, ["Vedal", "27853192"])
        self.assertEqual(settings.vision.command.poll_interval_sec, 2.5)
        self.assertEqual(settings.vision.command.stop_aliases, ["stop", "OFF"])


class VisionCompressionTest(unittest.TestCase):
    def test_compresses_screenshot_to_small_jpeg_data_url(self) -> None:
        image = Image.new("RGBA", (2400, 1200), (24, 40, 64, 255))

        payload = compress_image_for_vision(image, max_image_edge_px=600, jpeg_quality=50)

        self.assertEqual(payload.width, 600)
        self.assertEqual(payload.height, 300)
        self.assertEqual(payload.original_width, 2400)
        self.assertEqual(payload.original_height, 1200)
        self.assertEqual(payload.mime_type, "image/jpeg")
        self.assertTrue(payload.data_url.startswith("data:image/jpeg;base64,"))
        self.assertLess(payload.byte_size, 80_000)


class VisionDesktopInspectorTest(unittest.IsolatedAsyncioTestCase):
    async def test_inspector_captures_compresses_and_returns_model_summary(self) -> None:
        settings = LiveAdapterSettings()
        settings.vision.enabled = True
        calls: list[dict] = []

        async def describe(payload, question: str) -> str:
            calls.append({"payload": payload, "question": question})
            return "桌面上有一个游戏窗口和直播控制面板。"

        inspector = VisionDesktopInspector(
            settings.vision,
            screenshot_provider=lambda: Image.new("RGB", (1600, 900), "navy"),
            describe_image=describe,
        )

        result = await inspector.inspect("看看当前桌面有什么")

        self.assertTrue(result["success"])
        self.assertEqual(result["summary"], "桌面上有一个游戏窗口和直播控制面板。")
        self.assertEqual(result["model"], "qwen3-vl-flash")
        self.assertEqual(result["image"]["width"], 960)
        self.assertEqual(result["image"]["height"], 540)
        self.assertEqual(calls[0]["question"], "看看当前桌面有什么")
        self.assertLess(calls[0]["payload"].byte_size, 150_000)


if __name__ == "__main__":
    unittest.main()
