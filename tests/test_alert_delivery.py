import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import alert_delivery


class AlertDeliveryTests(unittest.TestCase):
    def test_resolve_env_strips_blank_wrapping(self) -> None:
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": " 12345 "}, clear=False):
            self.assertEqual(alert_delivery.resolve_env("TELEGRAM_CHAT_ID"), "12345")

    def test_get_updates_returns_empty_without_token(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(alert_delivery.telegram_api_get_updates(), [])

    def test_send_telegram_http_applies_prefix_and_truncates_long_message(self) -> None:
        response = Mock()
        response.json.return_value = {"ok": True}
        long_message = "x" * 3600

        with (
            patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_CHAT_ID": "12345",
                    "TELEGRAM_MESSAGE_PREFIX": "[prefix]",
                },
                clear=True,
            ),
            patch.object(alert_delivery.requests, "post", return_value=response) as post,
        ):
            alert_delivery.send_telegram_http(long_message)

        response.raise_for_status.assert_called_once()
        payload = post.call_args.kwargs["data"]
        self.assertEqual(payload["chat_id"], "12345")
        self.assertTrue(payload["text"].startswith("[prefix] "))
        self.assertLessEqual(len(payload["text"]), 3500)
        self.assertEqual(payload["disable_web_page_preview"], "true")

    def test_send_telegram_uses_http_fallback_when_script_missing(self) -> None:
        with patch.object(alert_delivery, "send_telegram_http") as fallback:
            alert_delivery.send_telegram(Path("missing-script.ps1"), "hello")

        fallback.assert_called_once_with("hello")


if __name__ == "__main__":
    unittest.main()
