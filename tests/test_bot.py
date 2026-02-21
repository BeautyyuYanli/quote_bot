import unittest
from io import BytesIO
from unittest.mock import patch

from PIL import Image
from quote_bot.bot import (
    _build_webhook_health_path,
    _build_webhook_url,
    _build_inline_photo_result,
    _contains_emoji,
    _load_font,
    _normalize_run_mode,
    _normalize_webhook_public_base_url,
    _normalize_webhook_path,
    extract_inline_query,
    extract_text_message,
    render_text_to_png,
)


class BotTestCase(unittest.TestCase):
    def test_render_text_to_png_returns_png(self) -> None:
        image = render_text_to_png("hello")
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(image), 100)

    def test_extract_text_message(self) -> None:
        update = {
            "update_id": 1,
            "message": {
                "chat": {"id": 123},
                "text": "hello",
            },
        }
        self.assertEqual(extract_text_message(update), (123, "hello"))

    def test_extract_text_message_ignores_non_text(self) -> None:
        update = {
            "update_id": 2,
            "message": {
                "chat": {"id": 123},
                "photo": [],
            },
        }
        self.assertIsNone(extract_text_message(update))

    def test_extract_inline_query(self) -> None:
        update = {
            "update_id": 3,
            "inline_query": {
                "id": "q1",
                "from": {"id": 888},
                "query": "hello",
            },
        }
        self.assertEqual(extract_inline_query(update), ("q1", 888, "hello"))

    def test_extract_inline_query_ignores_invalid_payload(self) -> None:
        update = {
            "update_id": 4,
            "inline_query": {
                "id": "q2",
                "from": {"id": "not-int"},
                "query": "hello",
            },
        }
        self.assertIsNone(extract_inline_query(update))

    def test_build_inline_photo_result(self) -> None:
        result = _build_inline_photo_result("hello", "file_id_123")
        self.assertEqual(result["type"], "photo")
        self.assertEqual(result["photo_file_id"], "file_id_123")
        self.assertNotIn("caption", result)

    def test_contains_emoji(self) -> None:
        self.assertTrue(_contains_emoji("hello 😀"))
        self.assertFalse(_contains_emoji("hello"))

    def test_load_font_fallback_honors_size(self) -> None:
        with patch("quote_bot.bot.os.getenv", return_value=None), patch(
            "quote_bot.bot.os.path.isfile", return_value=False
        ):
            font = _load_font(24)
        self.assertEqual(getattr(font, "size", None), 24)

    def test_render_text_to_png_limits_aspect_ratio(self) -> None:
        text = "x" * 500
        image_data = render_text_to_png(text)
        with Image.open(BytesIO(image_data)) as image:
            width, height = image.size
        self.assertLessEqual(width, height * 3)

    def test_normalize_run_mode(self) -> None:
        self.assertEqual(_normalize_run_mode("polling"), "polling")
        self.assertEqual(_normalize_run_mode(" WEBHOOK "), "webhook")
        with self.assertRaises(SystemExit):
            _normalize_run_mode("invalid")

    def test_normalize_webhook_path(self) -> None:
        self.assertEqual(_normalize_webhook_path("/a/b"), "/a/b")
        self.assertEqual(_normalize_webhook_path("a/b"), "/a/b")
        self.assertEqual(_normalize_webhook_path(""), "/telegram/webhook")

    def test_normalize_webhook_public_base_url(self) -> None:
        self.assertEqual(_normalize_webhook_public_base_url("https://bot.example.com/"), "https://bot.example.com")
        with self.assertRaises(SystemExit):
            _normalize_webhook_public_base_url("")
        with self.assertRaises(SystemExit):
            _normalize_webhook_public_base_url("bot.example.com")

    def test_build_webhook_url(self) -> None:
        self.assertEqual(
            _build_webhook_url("https://bot.example.com/", "/telegram/webhook"),
            "https://bot.example.com/telegram/webhook",
        )

    def test_build_webhook_health_path(self) -> None:
        self.assertEqual(_build_webhook_health_path("/telegram/webhook"), "/telegram/webhook/healthz")
        self.assertEqual(_build_webhook_health_path("telegram/webhook/"), "/telegram/webhook/healthz")
        self.assertEqual(_build_webhook_health_path("/"), "/healthz")


if __name__ == "__main__":
    unittest.main()
