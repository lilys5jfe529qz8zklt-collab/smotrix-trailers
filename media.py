from __future__ import annotations

import hashlib
import hmac
import math
import time
from typing import TYPE_CHECKING

from aiohttp import web

from config import MEDIA_SECRET, PUBLIC_URL

if TYPE_CHECKING:
    from telegram_client import Telegram

CHUNK = 1024 * 1024


def sign(message_id: int, ttl: int = 12 * 3600) -> str:
    expires = int(time.time()) + ttl
    payload = f"{int(message_id)}:{expires}".encode()
    signature = hmac.new(MEDIA_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    base = PUBLIC_URL or ""
    return f"{base}/media/{int(message_id)}?exp={expires}&sig={signature}"


def valid(message_id: int, expires: str, signature: str) -> bool:
    try:
        exp = int(expires)
    except (TypeError, ValueError):
        return False
    if exp < int(time.time()):
        return False
    payload = f"{int(message_id)}:{exp}".encode()
    expected = hmac.new(MEDIA_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


async def media_handler(request: web.Request, telegram: "Telegram") -> web.StreamResponse:
    try:
        message_id = int(request.match_info["message_id"])
    except (KeyError, ValueError):
        return web.Response(status=400, text="bad message id")
    if not valid(message_id, request.query.get("exp", ""), request.query.get("sig", "")):
        return web.Response(status=403, text="bad signature")
    try:
        size = await telegram.size(message_id)
    except Exception as exc:
        return web.Response(status=404, text=str(exc))

    range_header = request.headers.get("Range", "")
    start, end, status = 0, size - 1, 200
    if range_header.startswith("bytes="):
        spec = range_header[6:].split(",")[0].strip()
        left, _, right = spec.partition("-")
        try:
            if left:
                start = int(left)
                end = int(right) if right else size - 1
            else:
                start = max(0, size - int(right))
                end = size - 1
        except ValueError:
            return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        if start >= size or end < start:
            return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        end = min(end, size - 1)
        status = 206

    length = end - start + 1
    headers = {
        "Content-Type": "video/mp4",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "private, max-age=0",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Range",
        "Access-Control-Expose-Headers": "Content-Range, Content-Length, Accept-Ranges",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    response = web.StreamResponse(status=status, headers=headers)
    await response.prepare(request)
    if request.method == "HEAD":
        await response.write_eof()
        return response
    try:
        await telegram.stream(message_id, start, length, response)
    except (ConnectionResetError, BrokenPipeError):
        pass
    except Exception:
        pass
    try:
        await response.write_eof()
    except Exception:
        pass
    return response
