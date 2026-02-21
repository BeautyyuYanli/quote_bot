from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
from contextlib import nullcontext
from typing import Any, Sequence

import emoji
import httpx
from PIL import Image, ImageDraw, ImageFont

try:
    from pilmoji import Pilmoji
    from pilmoji.source import GoogleEmojiSource
except Exception:
    Pilmoji = None
    GoogleEmojiSource = None

API_ROOT = "https://api.telegram.org"
DEFAULT_POLL_TIMEOUT = 30
DEFAULT_RETRY_DELAY = 3.0
DEFAULT_INLINE_CACHE_TIME = 60
DEFAULT_INLINE_DEBOUNCE_SECONDS = 0.8
DEFAULT_WORKER_CONCURRENCY = 4
DEFAULT_FONT_SIZE = 44
MIN_FONT_SIZE = 16
PADDING = 40
LINE_SPACING = 10
MAX_WIDTH_TO_HEIGHT_RATIO = 3
MIN_IMAGE_WIDTH = 240
MIN_IMAGE_HEIGHT = 120
MAX_IMAGE_WIDTH = 1600
MAX_IMAGE_HEIGHT = 4096
DEFAULT_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
)


def _load_font(size: int) -> ImageFont.ImageFont:
    font_candidates: list[str] = []
    env_font = os.getenv("QUOTE_BOT_FONT_PATH")
    if env_font:
        font_candidates.append(env_font)
    font_candidates.extend(DEFAULT_FONT_PATHS)

    for font_path in font_candidates:
        if not os.path.isfile(font_path):
            continue
        try:
            return ImageFont.truetype(font_path, size=size)
        except OSError:
            logging.warning("Cannot load font: %s", font_path)

    return ImageFont.load_default()


def _contains_emoji(text: str) -> bool:
    return emoji.emoji_count(text) > 0


def _open_google_pilmoji(image: Image.Image, draw: ImageDraw.ImageDraw) -> Any:
    if Pilmoji is None or GoogleEmojiSource is None:
        return nullcontext(None)
    return Pilmoji(image, source=GoogleEmojiSource, draw=draw, cache=True)


def _measure_text_width(
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont,
    text: str,
    pilmoji_renderer: Any | None = None,
) -> int:
    if pilmoji_renderer is not None:
        try:
            width, _ = pilmoji_renderer.getsize(text or " ", font=font, spacing=LINE_SPACING)
            return max(1, int(width))
        except Exception:
            pass

    left, _, right, _ = draw.textbbox((0, 0), text or " ", font=font)
    return max(1, right - left)


def _line_height(
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont,
    pilmoji_renderer: Any | None = None,
) -> int:
    if pilmoji_renderer is not None:
        try:
            _, text_height = pilmoji_renderer.getsize("Hg", font=font, spacing=LINE_SPACING)
            _, emoji_height = pilmoji_renderer.getsize("😀", font=font, spacing=LINE_SPACING)
            return max(1, max(int(text_height), int(emoji_height)) + LINE_SPACING)
        except Exception:
            pass

    _, top, _, bottom = draw.textbbox((0, 0), "Hg", font=font)
    return max(1, bottom - top + LINE_SPACING)


def _wrap_text_line(
    line: str,
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont,
    max_width: int,
    pilmoji_renderer: Any | None = None,
) -> list[str]:
    if line == "":
        return [""]

    wrapped: list[str] = []
    current = ""
    for char in line:
        candidate = f"{current}{char}"
        if _measure_text_width(draw, font, candidate, pilmoji_renderer=pilmoji_renderer) <= max_width or not current:
            current = candidate
        else:
            wrapped.append(current)
            current = char

    wrapped.append(current)
    return wrapped


def _layout_text(
    text: str,
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont,
    pilmoji_renderer: Any | None = None,
) -> tuple[list[str], int, int, int]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    raw_lines = normalized.split("\n") or [" "]

    max_text_width = MAX_IMAGE_WIDTH - 2 * PADDING
    lines: list[str] = []
    for line in raw_lines:
        lines.extend(_wrap_text_line(line, draw, font, max_text_width, pilmoji_renderer=pilmoji_renderer))

    if not lines:
        lines = [" "]

    line_height = _line_height(draw, font, pilmoji_renderer=pilmoji_renderer)
    widths = [_measure_text_width(draw, font, line, pilmoji_renderer=pilmoji_renderer) for line in lines]
    text_width = max(widths) if widths else 1
    text_height = max(1, len(lines) * line_height - LINE_SPACING)
    return lines, text_width, text_height, line_height


def _draw_text_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    position: tuple[int, int],
    font: ImageFont.ImageFont,
    pilmoji_renderer: Any | None = None,
) -> None:
    if pilmoji_renderer is not None:
        try:
            pilmoji_renderer.text(position, text, fill="black", font=font, embedded_color=True)
            return
        except Exception:
            logging.warning("Falling back to plain text draw for emoji line.")

    draw.text(position, text, fill="black", font=font)


def _truncate_lines(lines: list[str], max_lines: int) -> list[str]:
    if len(lines) <= max_lines:
        return lines
    if max_lines <= 1:
        return ["..."]
    return lines[: max_lines - 1] + ["..."]


def render_text_to_png(text: str) -> bytes:
    content = text.strip("\n") or " "
    use_google_emoji = _contains_emoji(content)
    measure_image = Image.new("RGB", (1, 1), "white")
    measure_draw = ImageDraw.Draw(measure_image)

    best_lines: list[str] = [" "]
    best_font: ImageFont.ImageFont = _load_font(MIN_FONT_SIZE)
    best_line_height = _line_height(measure_draw, best_font)
    image_width = MIN_IMAGE_WIDTH
    image_height = MIN_IMAGE_HEIGHT

    with _open_google_pilmoji(measure_image, measure_draw) if use_google_emoji else nullcontext(None) as measure_pilmoji:
        for font_size in range(DEFAULT_FONT_SIZE, MIN_FONT_SIZE - 1, -4):
            font = _load_font(font_size)
            lines, text_width, text_height, line_height = _layout_text(
                content,
                measure_draw,
                font,
                pilmoji_renderer=measure_pilmoji,
            )

            candidate_width = max(MIN_IMAGE_WIDTH, text_width + 2 * PADDING)
            candidate_height = max(MIN_IMAGE_HEIGHT, text_height + 2 * PADDING)

            best_lines = lines
            best_font = font
            best_line_height = line_height
            image_width = candidate_width
            image_height = candidate_height
            if candidate_width <= MAX_IMAGE_WIDTH and candidate_height <= MAX_IMAGE_HEIGHT:
                break

        if image_height > MAX_IMAGE_HEIGHT:
            max_lines = max(1, (MAX_IMAGE_HEIGHT - 2 * PADDING + LINE_SPACING) // best_line_height)
            best_lines = _truncate_lines(best_lines, max_lines)

            truncated_width = max(
                _measure_text_width(measure_draw, best_font, line, pilmoji_renderer=measure_pilmoji)
                for line in best_lines
            )
            image_width = min(MAX_IMAGE_WIDTH, max(MIN_IMAGE_WIDTH, truncated_width + 2 * PADDING))
            image_height = MAX_IMAGE_HEIGHT

        # Keep image from becoming too wide: width <= height * 3.
        ratio_target_height = (image_width + MAX_WIDTH_TO_HEIGHT_RATIO - 1) // MAX_WIDTH_TO_HEIGHT_RATIO
        if ratio_target_height > image_height:
            image_height = min(MAX_IMAGE_HEIGHT, max(image_height, ratio_target_height))

    image = Image.new("RGB", (image_width, image_height), "white")
    draw = ImageDraw.Draw(image)

    with _open_google_pilmoji(image, draw) if use_google_emoji else nullcontext(None) as pilmoji_renderer:
        y = PADDING
        for line in best_lines:
            if y + best_line_height > image_height - PADDING + LINE_SPACING:
                break
            _draw_text_line(
                draw,
                line,
                (PADDING, y),
                best_font,
                pilmoji_renderer=pilmoji_renderer,
            )
            y += best_line_height

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def extract_text_message(update: dict[str, Any]) -> tuple[int, str] | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None

    text = message.get("text")
    if not isinstance(text, str):
        return None

    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None

    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return None

    return chat_id, text


def extract_inline_query(update: dict[str, Any]) -> tuple[str, int, str] | None:
    inline_query = update.get("inline_query")
    if not isinstance(inline_query, dict):
        return None

    inline_query_id = inline_query.get("id")
    if not isinstance(inline_query_id, str):
        return None

    query_text = inline_query.get("query")
    if not isinstance(query_text, str):
        return None

    from_user = inline_query.get("from")
    if not isinstance(from_user, dict):
        return None

    from_user_id = from_user.get("id")
    if not isinstance(from_user_id, int):
        return None

    return inline_query_id, from_user_id, query_text


class TelegramApi:
    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._client = client
        self._base_url = f"{API_ROOT}/bot{token}"

    async def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"{self._base_url}/getUpdates",
            params={
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": json.dumps(["message", "inline_query"]),
            },
            timeout=timeout + 10,
        )
        response.raise_for_status()

        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {payload}")

        updates = payload.get("result")
        if not isinstance(updates, list):
            raise RuntimeError(f"Unexpected getUpdates payload: {payload}")

        return updates

    async def send_photo(self, chat_id: int, image_data: bytes) -> dict[str, Any]:
        response = await self._client.post(
            f"{self._base_url}/sendPhoto",
            data={"chat_id": str(chat_id)},
            files={"photo": ("quote.png", image_data, "image/png")},
            timeout=30,
        )
        response.raise_for_status()

        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram sendPhoto failed: {payload}")

        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected sendPhoto payload: {payload}")
        return result

    async def answer_inline_query(
        self,
        inline_query_id: str,
        results: list[dict[str, Any]],
        cache_time: int,
        is_personal: bool = True,
    ) -> None:
        response = await self._client.post(
            f"{self._base_url}/answerInlineQuery",
            data={
                "inline_query_id": inline_query_id,
                "results": json.dumps(results, ensure_ascii=False),
                "cache_time": str(cache_time),
                "is_personal": "true" if is_personal else "false",
            },
            timeout=20,
        )
        response.raise_for_status()

        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram answerInlineQuery failed: {payload}")


def _extract_photo_file_id(message: dict[str, Any]) -> str:
    photos = message.get("photo")
    if not isinstance(photos, list) or not photos:
        raise RuntimeError(f"sendPhoto response missing photo info: {message}")

    largest = photos[-1]
    if not isinstance(largest, dict):
        raise RuntimeError(f"sendPhoto response has malformed photo info: {message}")

    file_id = largest.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        raise RuntimeError(f"sendPhoto response missing file_id: {message}")
    return file_id


def _build_inline_placeholder_result() -> dict[str, Any]:
    return {
        "type": "article",
        "id": "placeholder",
        "title": "输入文字后可生成图片",
        "description": "在机器人用户名后输入任意文字",
        "input_message_content": {
            "message_text": "请先输入要转成图片的文字。",
        },
    }


def _build_inline_error_result() -> dict[str, Any]:
    return {
        "type": "article",
        "id": "error",
        "title": "生成失败，请重试",
        "description": "稍后再试一次",
        "input_message_content": {
            "message_text": "图片生成失败，请稍后重试。",
        },
    }


def _build_inline_photo_result(query_text: str, file_id: str) -> dict[str, Any]:
    result_id = hashlib.sha1(query_text.encode("utf-8")).hexdigest()
    title = query_text if len(query_text) <= 80 else f"{query_text[:77]}..."
    return {
        "type": "photo",
        "id": result_id,
        "photo_file_id": file_id,
        "title": title,
        "description": "白底黑字",
    }


def _resolve_upload_chat_candidates(from_user_id: int, inline_cache_chat_id: int | None) -> list[int]:
    candidates: list[int] = []
    if inline_cache_chat_id is not None:
        candidates.append(inline_cache_chat_id)
    if from_user_id not in candidates:
        candidates.append(from_user_id)
    return candidates


async def _process_text_message(
    api: TelegramApi,
    chat_id: int,
    text: str,
    update_id: int | None,
    processing_semaphore: asyncio.Semaphore,
) -> None:
    try:
        async with processing_semaphore:
            image_data = await asyncio.to_thread(render_text_to_png, text)
            await api.send_photo(chat_id=chat_id, image_data=image_data)
    except Exception:
        logging.exception("Failed to process update_id=%s", update_id)


async def _process_inline_query(
    api: TelegramApi,
    *,
    inline_query_id: str,
    from_user_id: int,
    query_text: str,
    inline_file_cache: dict[str, str],
    inline_cache_time: int,
    inline_cache_chat_id: int | None,
    processing_semaphore: asyncio.Semaphore,
) -> None:
    query_text = query_text.strip()
    try:
        if not query_text:
            await api.answer_inline_query(
                inline_query_id=inline_query_id,
                results=[_build_inline_placeholder_result()],
                cache_time=1,
                is_personal=True,
            )
            return

        async with processing_semaphore:
            file_id = inline_file_cache.get(query_text)
            if not file_id:
                image_data = await asyncio.to_thread(render_text_to_png, query_text)
                upload_error: Exception | None = None
                for chat_candidate in _resolve_upload_chat_candidates(
                    from_user_id=from_user_id,
                    inline_cache_chat_id=inline_cache_chat_id,
                ):
                    try:
                        sent = await api.send_photo(chat_id=chat_candidate, image_data=image_data)
                        file_id = _extract_photo_file_id(sent)
                        inline_file_cache[query_text] = file_id
                        break
                    except Exception as exc:
                        upload_error = exc
                        logging.warning(
                            "Inline upload failed in chat_id=%s: %s",
                            chat_candidate,
                            exc,
                        )

                if not file_id:
                    if upload_error is not None:
                        raise upload_error
                    raise RuntimeError("Failed to upload inline image")

            await api.answer_inline_query(
                inline_query_id=inline_query_id,
                results=[_build_inline_photo_result(query_text, file_id)],
                cache_time=inline_cache_time,
                is_personal=True,
            )
    except asyncio.CancelledError:
        return
    except Exception:
        logging.exception("Failed to process inline query")
        try:
            await api.answer_inline_query(
                inline_query_id=inline_query_id,
                results=[_build_inline_error_result()],
                cache_time=1,
                is_personal=True,
            )
        except Exception:
            logging.exception("Failed to answer inline error response")


async def _debounced_inline_dispatch(
    *,
    from_user_id: int,
    inline_debounce_seconds: float,
    latest_inline_queries: dict[int, tuple[str, str]],
    inline_debounce_tasks: dict[int, asyncio.Task[None]],
    inline_processing_tasks: dict[int, asyncio.Task[None]],
    api: TelegramApi,
    inline_file_cache: dict[str, str],
    inline_cache_time: int,
    inline_cache_chat_id: int | None,
    processing_semaphore: asyncio.Semaphore,
) -> None:
    try:
        await asyncio.sleep(inline_debounce_seconds)
        latest_query = latest_inline_queries.get(from_user_id)
        if latest_query is None:
            return

        inline_query_id, query_text = latest_query
        process_task = asyncio.create_task(
            _process_inline_query(
                api,
                inline_query_id=inline_query_id,
                from_user_id=from_user_id,
                query_text=query_text,
                inline_file_cache=inline_file_cache,
                inline_cache_time=inline_cache_time,
                inline_cache_chat_id=inline_cache_chat_id,
                processing_semaphore=processing_semaphore,
            )
        )
        inline_processing_tasks[from_user_id] = process_task

        def _on_done(done_task: asyncio.Task[None]) -> None:
            if inline_processing_tasks.get(from_user_id) is done_task:
                inline_processing_tasks.pop(from_user_id, None)

        process_task.add_done_callback(_on_done)

        if latest_inline_queries.get(from_user_id) == latest_query:
            latest_inline_queries.pop(from_user_id, None)
    except asyncio.CancelledError:
        return
    except Exception:
        logging.exception("Debounced inline dispatch failed")
    finally:
        current_task = asyncio.current_task()
        if inline_debounce_tasks.get(from_user_id) is current_task:
            inline_debounce_tasks.pop(from_user_id, None)


def _schedule_inline_query(
    *,
    inline_query_id: str,
    from_user_id: int,
    query_text: str,
    inline_debounce_seconds: float,
    latest_inline_queries: dict[int, tuple[str, str]],
    inline_debounce_tasks: dict[int, asyncio.Task[None]],
    inline_processing_tasks: dict[int, asyncio.Task[None]],
    api: TelegramApi,
    inline_file_cache: dict[str, str],
    inline_cache_time: int,
    inline_cache_chat_id: int | None,
    processing_semaphore: asyncio.Semaphore,
) -> None:
    latest_inline_queries[from_user_id] = (inline_query_id, query_text)

    previous_task = inline_debounce_tasks.get(from_user_id)
    if previous_task is not None and not previous_task.done():
        previous_task.cancel()

    previous_processing_task = inline_processing_tasks.get(from_user_id)
    if previous_processing_task is not None and not previous_processing_task.done():
        previous_processing_task.cancel()

    task = asyncio.create_task(
        _debounced_inline_dispatch(
            from_user_id=from_user_id,
            inline_debounce_seconds=inline_debounce_seconds,
            latest_inline_queries=latest_inline_queries,
            inline_debounce_tasks=inline_debounce_tasks,
            inline_processing_tasks=inline_processing_tasks,
            api=api,
            inline_file_cache=inline_file_cache,
            inline_cache_time=inline_cache_time,
            inline_cache_chat_id=inline_cache_chat_id,
            processing_semaphore=processing_semaphore,
        )
    )
    inline_debounce_tasks[from_user_id] = task


async def _dispatch_update(
    *,
    api: TelegramApi,
    update: dict[str, Any],
    inline_cache_time: int,
    inline_cache_chat_id: int | None,
    inline_debounce_seconds: float,
    inline_file_cache: dict[str, str],
    latest_inline_queries: dict[int, tuple[str, str]],
    inline_debounce_tasks: dict[int, asyncio.Task[None]],
    inline_processing_tasks: dict[int, asyncio.Task[None]],
    processing_semaphore: asyncio.Semaphore,
) -> None:
    try:
        update_id = update.get("update_id")

        extracted = extract_text_message(update)
        if extracted is not None:
            chat_id, text = extracted
            await _process_text_message(
                api=api,
                chat_id=chat_id,
                text=text,
                update_id=update_id if isinstance(update_id, int) else None,
                processing_semaphore=processing_semaphore,
            )
            return

        inline_extracted = extract_inline_query(update)
        if inline_extracted is None:
            return

        inline_query_id, from_user_id, query_text = inline_extracted
        _schedule_inline_query(
            inline_query_id=inline_query_id,
            from_user_id=from_user_id,
            query_text=query_text,
            inline_debounce_seconds=max(0.0, inline_debounce_seconds),
            latest_inline_queries=latest_inline_queries,
            inline_debounce_tasks=inline_debounce_tasks,
            inline_processing_tasks=inline_processing_tasks,
            api=api,
            inline_file_cache=inline_file_cache,
            inline_cache_time=inline_cache_time,
            inline_cache_chat_id=inline_cache_chat_id,
            processing_semaphore=processing_semaphore,
        )
    except asyncio.CancelledError:
        return
    except Exception:
        logging.exception("Dispatch update failed")


async def _poll_updates_loop(
    api: TelegramApi,
    *,
    poll_timeout: int,
    retry_delay: float,
    inline_cache_time: int,
    inline_cache_chat_id: int | None,
    inline_debounce_seconds: float,
    inline_file_cache: dict[str, str],
    latest_inline_queries: dict[int, tuple[str, str]],
    inline_debounce_tasks: dict[int, asyncio.Task[None]],
    inline_processing_tasks: dict[int, asyncio.Task[None]],
    processing_semaphore: asyncio.Semaphore,
) -> None:
    offset = 0
    while True:
        try:
            updates = await api.get_updates(offset=offset, timeout=poll_timeout)
        except Exception:
            logging.exception("Failed to pull Telegram updates")
            await asyncio.sleep(retry_delay)
            continue

        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = max(offset, update_id + 1)
            asyncio.create_task(
                _dispatch_update(
                    api=api,
                    update=update,
                    inline_cache_time=inline_cache_time,
                    inline_cache_chat_id=inline_cache_chat_id,
                    inline_debounce_seconds=inline_debounce_seconds,
                    inline_file_cache=inline_file_cache,
                    latest_inline_queries=latest_inline_queries,
                    inline_debounce_tasks=inline_debounce_tasks,
                    inline_processing_tasks=inline_processing_tasks,
                    processing_semaphore=processing_semaphore,
                )
            )


async def run(
    token: str,
    poll_timeout: int,
    retry_delay: float,
    inline_cache_time: int,
    inline_cache_chat_id: int | None,
    inline_debounce_seconds: float,
    worker_concurrency: int,
) -> None:
    inline_file_cache: dict[str, str] = {}
    latest_inline_queries: dict[int, tuple[str, str]] = {}
    inline_debounce_tasks: dict[int, asyncio.Task[None]] = {}
    inline_processing_tasks: dict[int, asyncio.Task[None]] = {}
    processing_semaphore = asyncio.Semaphore(max(1, worker_concurrency))

    async with httpx.AsyncClient() as client:
        api = TelegramApi(token=token, client=client)
        await _poll_updates_loop(
            api=api,
            poll_timeout=max(1, poll_timeout),
            retry_delay=max(0.1, retry_delay),
            inline_cache_time=max(0, inline_cache_time),
            inline_cache_chat_id=inline_cache_chat_id,
            inline_debounce_seconds=max(0.0, inline_debounce_seconds),
            inline_file_cache=inline_file_cache,
            latest_inline_queries=latest_inline_queries,
            inline_debounce_tasks=inline_debounce_tasks,
            inline_processing_tasks=inline_processing_tasks,
            processing_semaphore=processing_semaphore,
        )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_optional_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return int(raw)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Telegram bot that turns received text into black text on white image."
    )
    parser.add_argument("--token", default=os.getenv("TELEGRAM_BOT_TOKEN"), help="Telegram bot token")
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("POLL_TIMEOUT", DEFAULT_POLL_TIMEOUT)),
        help=f"Long polling timeout in seconds (default: {DEFAULT_POLL_TIMEOUT})",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=float(os.getenv("RETRY_DELAY_SECONDS", DEFAULT_RETRY_DELAY)),
        help=f"Delay before retry after network errors (default: {DEFAULT_RETRY_DELAY})",
    )
    parser.add_argument(
        "--inline-cache-time",
        type=int,
        default=_env_int("INLINE_CACHE_TIME", DEFAULT_INLINE_CACHE_TIME),
        help=f"Inline answer cache time in seconds (default: {DEFAULT_INLINE_CACHE_TIME})",
    )
    parser.add_argument(
        "--inline-cache-chat-id",
        type=int,
        default=_env_optional_int("INLINE_CACHE_CHAT_ID"),
        help="Optional chat ID for inline image caching uploads (e.g., private channel/group).",
    )
    parser.add_argument(
        "--inline-debounce-seconds",
        type=float,
        default=_env_float("INLINE_DEBOUNCE_SECONDS", DEFAULT_INLINE_DEBOUNCE_SECONDS),
        help=f"Delay before processing inline query (default: {DEFAULT_INLINE_DEBOUNCE_SECONDS})",
    )
    parser.add_argument(
        "--worker-concurrency",
        type=int,
        default=_env_int("WORKER_CONCURRENCY", DEFAULT_WORKER_CONCURRENCY),
        help=f"Max concurrent processing tasks (default: {DEFAULT_WORKER_CONCURRENCY})",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Python logging level (DEBUG, INFO, WARNING, ERROR)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if not args.token:
        raise SystemExit("Missing bot token. Set TELEGRAM_BOT_TOKEN or pass --token.")

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        asyncio.run(
            run(
                token=args.token,
                poll_timeout=args.timeout,
                retry_delay=args.retry_delay,
                inline_cache_time=max(0, int(args.inline_cache_time)),
                inline_cache_chat_id=args.inline_cache_chat_id,
                inline_debounce_seconds=max(0.0, float(args.inline_debounce_seconds)),
                worker_concurrency=max(1, int(args.worker_concurrency)),
            )
        )
    except KeyboardInterrupt:
        logging.info("Bot stopped")


if __name__ == "__main__":
    main()
