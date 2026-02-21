from __future__ import annotations

import os
from urllib.error import URLError
from urllib.request import urlopen

DEFAULT_WEBHOOK_PATH = "/telegram/webhook"
RUN_MODE_WEBHOOK = "webhook"


def _normalize_webhook_path(value: str) -> str:
    path = value.strip()
    if not path:
        return DEFAULT_WEBHOOK_PATH
    if not path.startswith("/"):
        path = f"/{path}"
    return path


def _build_health_url(public_base_url: str, webhook_path: str) -> str | None:
    base = public_base_url.strip().rstrip("/")
    if not base:
        return None

    path = _normalize_webhook_path(webhook_path).rstrip("/")
    health_path = "/healthz" if path in ("", "/") else f"{path}/healthz"
    return f"{base}{health_path}"


def check_health() -> int:
    mode = os.getenv("BOT_MODE", "polling").strip().lower()
    if mode != RUN_MODE_WEBHOOK:
        return 0

    url = _build_health_url(
        public_base_url=os.getenv("WEBHOOK_PUBLIC_BASE_URL", ""),
        webhook_path=os.getenv("WEBHOOK_PATH", DEFAULT_WEBHOOK_PATH),
    )
    if url is None:
        return 1

    try:
        with urlopen(url, timeout=8) as response:
            status_code = getattr(response, "status", response.getcode())
            return 0 if status_code == 200 else 1
    except (OSError, URLError):
        return 1


def main() -> None:
    raise SystemExit(check_health())


if __name__ == "__main__":
    main()
