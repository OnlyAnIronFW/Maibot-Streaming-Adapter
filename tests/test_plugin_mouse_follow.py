import sys
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from maibot_bilibili_live_adapter_copy.plugin import BilibiliLiveAdapterPlugin


class _EmbodiedRuntimeStub:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def set_mouse_follow_enabled(self, enabled: bool) -> None:
        self.calls.append(bool(enabled))


class PluginMouseFollowPatchTest(unittest.TestCase):
    def test_control_state_patch_updates_embodied_runtime_mouse_follow_without_restart(self) -> None:
        plugin = BilibiliLiveAdapterPlugin()
        runtime = _EmbodiedRuntimeStub()
        plugin._embodied_live2d_runtime = runtime  # type: ignore[assignment]

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("maibot_bilibili_live_adapter_copy.plugin._plugin_data_dir", return_value=Path(temp_dir)):
                updated_state = plugin._handle_live2d_control_state_patch({"mouse_follow_enabled": True})
                updated_state = plugin._handle_live2d_control_state_patch({"mouse_follow_enabled": False})

        self.assertEqual(updated_state.mouse_follow_enabled, False)
        self.assertEqual(runtime.calls, [True, False])


if __name__ == "__main__":
    unittest.main()
