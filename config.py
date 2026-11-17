from __future__ import annotations

import os
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _load_local_env() -> None:
    path = BASE / ".secrets" / "trailer.env"
    if not path.is_file():
        return
    for line in path.read_text("utf-8").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


_load_local_env()


def _text(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int(name: str, default: int = 0) -> int:
    try:
        return int(_text(name) or default)
    except (TypeError, ValueError):
        return default


def _token() -> str:
    value = _text("BOT_TOKEN")
    if value:
        return value
    path = BASE / ".secrets" / "trailer-bot-token.txt"
    return path.read_text("utf-8").strip() if path.exists() else ""


DATA_DIR = Path(_text("DATA_DIR") or str(BASE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = _token()
API_ID = _int("TELEGRAM_API_ID")
API_HASH = _text("TELEGRAM_API_HASH")
TRAILER_CHAT_ID = _int("TRAILER_CHAT_ID")
ADMIN_IDS = {
    int(part)
    for part in _text("ADMIN_IDS", "5820040561").replace(";", ",").split(",")
    if part.strip().isdigit()
}

STORAGE_API_BASE = _text("STORAGE_API_BASE", "https://bot-1794382662-8848-cc073335.bothost.tech").rstrip("/")
STORAGE_TOKEN = _text("STORAGE_TOKEN")
TRAILER_TOKEN = _text("TRAILER_TOKEN")

PUBLIC_URL = _text("PUBLIC_URL").rstrip("/")
PORT = _int("PORT", 3000)
MEDIA_SECRET = _text("MEDIA_SECRET") or "trailers"

DEFAULT_QUALITY = _int("DEFAULT_QUALITY", 1080)
MAX_FILE_MB = _int("MAX_FILE_MB", 1900)
YOUTUBE_CHANNELS = [
    part.strip()
    for part in _text("YOUTUBE_CHANNELS", "@kinomantrailers").split(",")
    if part.strip()
]
POLL_MINUTES = _int("POLL_MINUTES", 15)
YT_COOKIES_FILE = _text("YT_COOKIES_FILE")
YT_PROXY = _text("YT_PROXY")
