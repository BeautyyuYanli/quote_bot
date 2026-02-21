from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, Sequence

import emoji
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from PIL import Image, ImageDraw, ImageFont

try:
    from pilmoji import Pilmoji
    from pilmoji.source import GoogleEmojiSource
except Exception:
    Pilmoji = None
    GoogleEmojiSource = None

API_ROOT = "https://api.telegram.org"
DEFAULT_RUN_MODE = "polling"
RUN_MODE_POLLING = "polling"
RUN_MODE_WEBHOOK = "webhook"
SUPPORTED_RUN_MODES = (RUN_MODE_POLLING, RUN_MODE_WEBHOOK)
DEFAULT_POLL_TIMEOUT = 30
DEFAULT_RETRY_DELAY = 3.0
DEFAULT_INLINE_CACHE_TIME = 60
DEFAULT_INLINE_DEBOUNCE_SECONDS = 0.8
DEFAULT_WORKER_CONCURRENCY = 4
DEFAULT_BIND_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_WEBHOOK_PATH = "/telegram/webhook"
DEFAULT_FONT_SIZE = 44
MIN_FONT_SIZE = 28
PADDING = 40
LINE_SPACING = 10
MAX_WIDTH_TO_HEIGHT_RATIO = 2.5
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

    async def set_webhook(
        self,
        *,
        url: str,
        secret_token: str,
        allowed_updates: list[str] | None = None,
    ) -> None:
        payload: dict[str, str] = {
            "url": url,
            "secret_token": secret_token,
        }
        if allowed_updates is not None:
            payload["allowed_updates"] = json.dumps(allowed_updates)

        response = await self._client.post(
            f"{self._base_url}/setWebhook",
            data=payload,
            timeout=20,
        )
        response.raise_for_status()

        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram setWebhook failed: {body}")

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> None:
        response = await self._client.post(
            f"{self._base_url}/deleteWebhook",
            data={
                "drop_pending_updates": "true" if drop_pending_updates else "false",
            },
            timeout=20,
        )
        response.raise_for_status()

        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram deleteWebhook failed: {body}")


@dataclass
class RuntimeState:
    inline_cache_time: int
    inline_cache_chat_id: int | None
    inline_debounce_seconds: float
    worker_concurrency: int
    inline_file_cache: dict[str, str] = field(default_factory=dict)
    latest_inline_queries: dict[int, tuple[str, str]] = field(default_factory=dict)
    inline_debounce_tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict)
    inline_processing_tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict)
    processing_semaphore: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self.processing_semaphore = asyncio.Semaphore(max(1, self.worker_concurrency))


def _build_runtime_state(
    inline_cache_time: int,
    inline_cache_chat_id: int | None,
    inline_debounce_seconds: float,
    worker_concurrency: int,
) -> RuntimeState:
    return RuntimeState(
        inline_cache_time=max(0, inline_cache_time),
        inline_cache_chat_id=inline_cache_chat_id,
        inline_debounce_seconds=max(0.0, inline_debounce_seconds),
        worker_concurrency=max(1, worker_concurrency),
    )


async def _cancel_runtime_tasks(runtime: RuntimeState) -> None:
    tasks = [*runtime.inline_debounce_tasks.values(), *runtime.inline_processing_tasks.values()]
    for task in tasks:
        if not task.done():
            task.cancel()

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    runtime.inline_debounce_tasks.clear()
    runtime.inline_processing_tasks.clear()
    runtime.latest_inline_queries.clear()


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
    runtime: RuntimeState,
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
                processing_semaphore=runtime.processing_semaphore,
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
            inline_debounce_seconds=runtime.inline_debounce_seconds,
            latest_inline_queries=runtime.latest_inline_queries,
            inline_debounce_tasks=runtime.inline_debounce_tasks,
            inline_processing_tasks=runtime.inline_processing_tasks,
            api=api,
            inline_file_cache=runtime.inline_file_cache,
            inline_cache_time=runtime.inline_cache_time,
            inline_cache_chat_id=runtime.inline_cache_chat_id,
            processing_semaphore=runtime.processing_semaphore,
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
    runtime: RuntimeState,
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
                    runtime=runtime,
                )
            )


async def run_polling(
    token: str,
    poll_timeout: int,
    retry_delay: float,
    inline_cache_time: int,
    inline_cache_chat_id: int | None,
    inline_debounce_seconds: float,
    worker_concurrency: int,
) -> None:
    runtime = _build_runtime_state(
        inline_cache_time=inline_cache_time,
        inline_cache_chat_id=inline_cache_chat_id,
        inline_debounce_seconds=inline_debounce_seconds,
        worker_concurrency=worker_concurrency,
    )

    async with httpx.AsyncClient() as client:
        api = TelegramApi(token=token, client=client)
        try:
            await _ensure_polling_mode(api=api, retry_delay=retry_delay)
            await _poll_updates_loop(
                api=api,
                poll_timeout=max(1, poll_timeout),
                retry_delay=max(0.1, retry_delay),
                runtime=runtime,
            )
        finally:
            await _cancel_runtime_tasks(runtime)


async def run(
    token: str,
    poll_timeout: int,
    retry_delay: float,
    inline_cache_time: int,
    inline_cache_chat_id: int | None,
    inline_debounce_seconds: float,
    worker_concurrency: int,
) -> None:
    await run_polling(
        token=token,
        poll_timeout=poll_timeout,
        retry_delay=retry_delay,
        inline_cache_time=inline_cache_time,
        inline_cache_chat_id=inline_cache_chat_id,
        inline_debounce_seconds=inline_debounce_seconds,
        worker_concurrency=worker_concurrency,
    )


def _normalize_run_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    if mode not in SUPPORTED_RUN_MODES:
        supported = ", ".join(SUPPORTED_RUN_MODES)
        raise SystemExit(f"Invalid run mode: {value!r}. Supported modes: {supported}.")
    return mode


def _normalize_webhook_path(value: str) -> str:
    path = (value or "").strip()
    if not path:
        return DEFAULT_WEBHOOK_PATH
    if not path.startswith("/"):
        return f"/{path}"
    return path


def _normalize_webhook_public_base_url(value: str) -> str:
    base_url = (value or "").strip().rstrip("/")
    if not base_url:
        raise SystemExit("Missing webhook public URL. Set WEBHOOK_PUBLIC_BASE_URL or pass --webhook-public-base-url.")
    if not (base_url.startswith("https://") or base_url.startswith("http://")):
        raise SystemExit("Invalid webhook public URL. Must start with http:// or https://.")
    return base_url


def _build_webhook_url(public_base_url: str, webhook_path: str) -> str:
    return f"{_normalize_webhook_public_base_url(public_base_url)}{_normalize_webhook_path(webhook_path)}"


def _build_webhook_health_path(webhook_path: str) -> str:
    normalized = _normalize_webhook_path(webhook_path).rstrip("/")
    if normalized == "":
        return "/healthz"
    return f"{normalized}/healthz"


async def _ensure_polling_mode(api: TelegramApi, retry_delay: float) -> None:
    delay = max(0.1, retry_delay)
    while True:
        try:
            await api.delete_webhook(drop_pending_updates=False)
            logging.info("Telegram webhook disabled, polling mode ready.")
            return
        except Exception:
            logging.exception("Failed to disable webhook, retrying in %.1fs", delay)
            await asyncio.sleep(delay)


async def _ensure_webhook_mode(
    api: TelegramApi,
    *,
    webhook_url: str,
    secret_token: str,
    retry_delay: float,
) -> None:
    delay = max(0.1, retry_delay)
    while True:
        try:
            await api.set_webhook(
                url=webhook_url,
                secret_token=secret_token,
                allowed_updates=["message", "inline_query"],
            )
            logging.info("Telegram webhook set to %s", webhook_url)
            return
        except Exception:
            logging.exception("Failed to set webhook, retrying in %.1fs", delay)
            await asyncio.sleep(delay)


def _setup_uvloop_for_polling() -> None:
    try:
        import uvloop  # type: ignore
    except Exception:
        logging.info("uvloop unavailable, falling back to default asyncio loop.")
        return

    try:
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except Exception:
        logging.exception("Failed to enable uvloop, using default asyncio loop.")


def create_webhook_app(
    *,
    token: str,
    inline_cache_time: int,
    inline_cache_chat_id: int | None,
    inline_debounce_seconds: float,
    worker_concurrency: int,
    retry_delay: float,
    webhook_public_base_url: str,
    webhook_path: str,
) -> FastAPI:
    runtime = _build_runtime_state(
        inline_cache_time=inline_cache_time,
        inline_cache_chat_id=inline_cache_chat_id,
        inline_debounce_seconds=inline_debounce_seconds,
        worker_concurrency=worker_concurrency,
    )
    normalized_webhook_path = _normalize_webhook_path(webhook_path)
    webhook_health_path = _build_webhook_health_path(normalized_webhook_path)
    webhook_url = _build_webhook_url(webhook_public_base_url, normalized_webhook_path)
    expected_secret = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with httpx.AsyncClient() as client:
            api = TelegramApi(token=token, client=client)
            await _ensure_webhook_mode(
                api=api,
                webhook_url=webhook_url,
                secret_token=expected_secret,
                retry_delay=retry_delay,
            )
            yield {"api": api, "runtime": runtime}
            await _cancel_runtime_tasks(runtime)

    app = FastAPI(lifespan=lifespan)

    @app.post(normalized_webhook_path)
    async def telegram_webhook(request: Request) -> dict[str, bool]:
        got_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if got_secret != expected_secret:
            raise HTTPException(status_code=403, detail="Forbidden")

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid Telegram update payload")

        asyncio.create_task(
            _dispatch_update(
                api=request.state.api,
                update=payload,
                runtime=request.state.runtime,
            )
        )
        return {"ok": True}

    @app.get(webhook_health_path)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def run_webhook(
    *,
    token: str,
    inline_cache_time: int,
    inline_cache_chat_id: int | None,
    inline_debounce_seconds: float,
    worker_concurrency: int,
    retry_delay: float,
    port: int,
    webhook_public_base_url: str,
    webhook_path: str,
    log_level: str,
) -> None:
    app = create_webhook_app(
        token=token,
        inline_cache_time=inline_cache_time,
        inline_cache_chat_id=inline_cache_chat_id,
        inline_debounce_seconds=inline_debounce_seconds,
        worker_concurrency=worker_concurrency,
        retry_delay=max(0.1, retry_delay),
        webhook_public_base_url=webhook_public_base_url,
        webhook_path=webhook_path,
    )

    config = uvicorn.Config(
        app=app,
        host=DEFAULT_BIND_HOST,
        port=max(1, int(port)),
        log_level=str(log_level).lower(),
    )
    server = uvicorn.Server(config)
    server.run()


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
        "--mode",
        default=os.getenv("BOT_MODE", DEFAULT_RUN_MODE),
        help=f"Run mode: {RUN_MODE_POLLING} (default) or {RUN_MODE_WEBHOOK}",
    )
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
    parser.add_argument(
        "--port",
        type=int,
        default=_env_int("PORT", DEFAULT_PORT),
        help=f"Bind port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--webhook-path",
        default=os.getenv("WEBHOOK_PATH", DEFAULT_WEBHOOK_PATH),
        help=f"Webhook route path (default: {DEFAULT_WEBHOOK_PATH})",
    )
    parser.add_argument(
        "--webhook-public-base-url",
        default=os.getenv("WEBHOOK_PUBLIC_BASE_URL", ""),
        help="Public base URL for Telegram webhook registration (e.g., https://bot.example.com).",
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

    mode = _normalize_run_mode(str(args.mode))

    try:
        if mode == RUN_MODE_WEBHOOK:
            run_webhook(
                token=args.token,
                inline_cache_time=max(0, int(args.inline_cache_time)),
                inline_cache_chat_id=args.inline_cache_chat_id,
                inline_debounce_seconds=max(0.0, float(args.inline_debounce_seconds)),
                worker_concurrency=max(1, int(args.worker_concurrency)),
                retry_delay=max(0.1, float(args.retry_delay)),
                port=max(1, int(args.port)),
                webhook_public_base_url=str(args.webhook_public_base_url),
                webhook_path=_normalize_webhook_path(str(args.webhook_path)),
                log_level=str(args.log_level),
            )
        else:
            _setup_uvloop_for_polling()
            asyncio.run(
                run_polling(
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
