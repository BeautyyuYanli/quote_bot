import unittest
from unittest.mock import patch

from quote_bot.healthcheck import _build_health_url, check_health


class HealthcheckTestCase(unittest.TestCase):
    def test_build_health_url(self) -> None:
        self.assertEqual(
            _build_health_url("https://bot.example.com/", "/telegram/webhook"),
            "https://bot.example.com/telegram/webhook/healthz",
        )
        self.assertEqual(
            _build_health_url("https://bot.example.com", "telegram/webhook/"),
            "https://bot.example.com/telegram/webhook/healthz",
        )
        self.assertIsNone(_build_health_url("", "/telegram/webhook"))

    @patch("quote_bot.healthcheck.urlopen")
    def test_check_health_returns_ok_in_non_webhook_mode(self, mocked_urlopen) -> None:
        with patch.dict("os.environ", {"BOT_MODE": "polling"}, clear=False):
            self.assertEqual(check_health(), 0)
        mocked_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
