import unittest

from . import _host_bootstrap  # noqa: F401

from maibot_bilibili_live_adapter_copy.config import LiveAdapterSettings
from maibot_bilibili_live_adapter_copy.message_codec import build_message_dict


class MessageCodecIdentityTest(unittest.TestCase):
    def test_build_message_dict_aliases_known_vedal_account_display_name(self) -> None:
        settings = LiveAdapterSettings()

        message = build_message_dict(
            {
                "event_id": "evt-vedal",
                "type": "danmaku",
                "text": "hello",
                "summary": "hello",
                "user_id": "27853192",
                "username": "IDKWhatID2Use",
            },
            settings,
        )

        self.assertEqual(message["message_info"]["user_info"]["user_id"], "Vedal")
        self.assertEqual(message["message_info"]["user_info"]["user_nickname"], "Vedal")
        self.assertEqual(
            message["message_info"]["additional_config"]["maibot_memory_user_id"],
            "Vedal",
        )
        self.assertEqual(
            message["message_info"]["additional_config"]["live_source_user_id"],
            "27853192",
        )
        self.assertEqual(
            message["message_info"]["additional_config"]["live_source_username"],
            "IDKWhatID2Use",
        )

    def test_build_message_dict_keeps_other_users_unchanged(self) -> None:
        settings = LiveAdapterSettings()

        message = build_message_dict(
            {
                "event_id": "evt-other",
                "type": "danmaku",
                "text": "hello",
                "summary": "hello",
                "user_id": "123456",
                "username": "ordinary-user",
            },
            settings,
        )

        self.assertEqual(message["message_info"]["user_info"]["user_id"], "123456")
        self.assertEqual(message["message_info"]["user_info"]["user_nickname"], "ordinary-user")


if __name__ == "__main__":
    unittest.main()
