import unittest

from . import _host_bootstrap  # noqa: F401

from maibot_bilibili_live_adapter_copy.live2d_adaptive.bridge import InMemoryLive2DBridge
from maibot_bilibili_live_adapter_copy.live2d_adaptive.controller import Live2DController
from maibot_bilibili_live_adapter_copy.live2d_adaptive.embodied import EmbodiedParamDriver
from maibot_bilibili_live_adapter_copy.live2d_adaptive.profile import ParameterProfile


class _Logger:
    def warning(self, message: str) -> None:
        pass


class EmbodiedTextInferenceTest(unittest.TestCase):
    def test_real_chinese_reply_text_infers_emotion_intent(self) -> None:
        controller = Live2DController(
            bridge=InMemoryLive2DBridge(),
            profile=ParameterProfile.standard_fallback(),
            mouth_sync_mode="plugin_local",
            idle_motion_enabled=False,
            idle_sway_enabled=False,
            speech_sway_enabled=False,
            embodied_mode=True,
        )
        driver = EmbodiedParamDriver(
            controller=controller,
            logger=_Logger(),
            fallback_to_legacy=False,
        )

        self.assertEqual(driver._infer_reply_emotion_intent("\u54c8\u54c8\uff0c\u592a\u597d\u4e86\uff0c\u4eca\u5929\u771f\u5f00\u5fc3"), "react_happy")
        self.assertEqual(driver._infer_reply_emotion_intent("\u54c7\uff01\uff1f\u8fd9\u4e5f\u592a\u60ca\u8bb6\u4e86"), "react_surprised")
        self.assertEqual(driver._infer_reply_emotion_intent("\u6709\u70b9\u5bb3\u7f9e\uff0c\u8138\u7ea2\u4e86"), "react_shy")
        self.assertEqual(driver._infer_reply_emotion_intent("\u4e3a\u4ec0\u4e48\u4f1a\u8fd9\u6837\uff1f\u600e\u4e48\u56de\u4e8b"), "react_confused")
        self.assertEqual(driver._infer_reply_emotion_intent("\u592a\u96be\u8fc7\u4e86\uff0c\u771f\u7684\u597d\u60b2\u4f24"), "react_sad")
        self.assertEqual(driver._infer_reply_emotion_intent("\u6c14\u6b7b\u6211\u4e86\uff0c\u771f\u7684\u597d\u751f\u6c14"), "react_angry")
