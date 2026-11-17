from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

import media
from config import ADMIN_IDS, BOT_TOKEN, PUBLIC_URL, TRAILER_TOKEN
from db import DB
from telegram_client import Telegram
from worker import Worker

logger = logging.getLogger(__name__)
BASE = Path(__file__).resolve().parent
MINIAPP = BASE / "miniapp"


def validate_init_data(raw: str, max_age: int = 86400) -> dict | None:
    if not raw or not BOT_TOKEN:
        return None
    pairs = dict(parse_qsl(raw, keep_blank_values=True))
    received = pairs.pop("hash", "")
    if not received:
        return None
    check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(pairs.get("auth_date") or 0)
    except ValueError:
        return None
    if max_age and time.time() - auth_date > max_age:
        return None
    try:
        return json.loads(pairs.get("user") or "{}")
    except ValueError:
        return None


def _admin(request: web.Request) -> dict | None:
    token = request.query.get("token") or request.headers.get("X-Admin-Token") or ""
    if TRAILER_TOKEN and hmac.compare_digest(token, TRAILER_TOKEN):
        return {"id": 0, "first_name": "token"}
    user = validate_init_data(request.headers.get("X-Init-Data") or request.query.get("init_data") or "")
    if not user or int(user.get("id") or 0) not in ADMIN_IDS:
        return None
    return user


def _trailer(row) -> dict:
    return {
        "youtube_id": row["youtube_id"],
        "title_id": int(row["title_id"] or 0),
        "title": row["title_name"],
        "year": int(row["title_year"] or 0),
        "youtube_title": row["youtube_title"],
        "quality": int(row["quality"] or 0),
        "height": int(row["height"] or 0),
        "duration": int(row["duration"] or 0),
        "size": int(row["size"] or 0),
        "published_at": int(row["published_at"] or 0),
        "mp4": media.sign(int(row["message_id"])) if row["message_id"] else "",
    }


def create_app(db: DB, telegram: Telegram, worker: Worker) -> web.Application:
    app = web.Application(client_max_size=4 * 1024 * 1024)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "trailers": db.stats(),
            "chat": bool(worker.current) or True,
            "channels": len(db.channels(enabled_only=True)),
        })

    async def trailer_for_title(request: web.Request) -> web.Response:
        token = request.query.get("token") or request.headers.get("X-Trailer-Token") or ""
        if TRAILER_TOKEN and not hmac.compare_digest(token, TRAILER_TOKEN):
            return web.json_response({"ok": False, "error": "bad token"}, status=403)
        try:
            title_id = int(request.match_info["title_id"])
        except (KeyError, ValueError):
            return web.json_response({"ok": False, "error": "bad id"}, status=400)
        row = db.latest(title_id)
        if row is None:
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        return web.json_response({"ok": True, "trailer": _trailer(row)})

    async def recent(request: web.Request) -> web.Response:
        token = request.query.get("token") or request.headers.get("X-Trailer-Token") or ""
        if TRAILER_TOKEN and not hmac.compare_digest(token, TRAILER_TOKEN):
            return web.json_response({"ok": False, "error": "bad token"}, status=403)
        try:
            limit = int(request.query.get("limit") or 20)
        except ValueError:
            limit = 20
        return web.json_response({
            "ok": True,
            "trailers": [_trailer(row) for row in db.recent(limit)],
        })

    async def admin_state(request: web.Request) -> web.Response:
        if not _admin(request):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        channels = [
            {"url": row["url"], "title": row["title"], "channel_id": row["channel_id"]}
            for row in db.channels()
        ]
        return web.json_response({
            "ok": True,
            "quality": db.quality(),
            "stats": db.stats(),
            "current": worker.current or None,
            "channels": channels,
            "trailers": [
                {
                    "youtube_id": row["youtube_id"],
                    "title": row["title_name"],
                    "year": int(row["title_year"] or 0),
                    "youtube_title": row["youtube_title"],
                    "quality": int(row["quality"] or 0),
                    "status": row["status"],
                    "error": row["error"],
                    "published_at": int(row["published_at"] or 0),
                    "message_id": int(row["message_id"] or 0),
                }
                for row in db.list(60)
            ],
        })

    async def admin_quality(request: web.Request) -> web.Response:
        if not _admin(request):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            value = int(body.get("value") or 0)
        except (TypeError, ValueError):
            value = 0
        if value not in {480, 720, 1080, 1440, 2160}:
            return web.json_response({"ok": False, "error": "плохое качество"}, status=400)
        db.set_quality(value)
        return web.json_response({"ok": True, "quality": value})

    async def admin_poll(request: web.Request) -> web.Response:
        if not _admin(request):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        asyncio.create_task(worker.poll_channels())
        return web.json_response({"ok": True, "started": True})

    async def admin_links(request: web.Request) -> web.Response:
        if not _admin(request):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw = body.get("urls") or body.get("url") or ""
        items = [part.strip() for part in str(raw).replace("\n", ",").split(",") if part.strip()]
        if not items:
            return web.json_response({"ok": False, "error": "нет ссылок"}, status=400)
        try:
            quality = int(body.get("quality") or db.quality())
        except (TypeError, ValueError):
            quality = db.quality()
        results = []
        for url in items[:20]:
            try:
                results.append(await worker.add_link(url, quality))
            except Exception as exc:  # noqa: BLE001
                results.append({"ok": False, "error": str(exc)[:200], "url": url})
        return web.json_response({"ok": True, "results": results})

    async def admin_retry(request: web.Request) -> web.Response:
        if not _admin(request):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:
            body = {}
        youtube_id = str(body.get("youtube_id") or "").strip()
        if not youtube_id:
            return web.json_response({"ok": False, "error": "нет id"}, status=400)
        db.retry(youtube_id)
        worker.wake()
        return web.json_response({"ok": True})

    async def admin_channel(request: web.Request) -> web.Response:
        if not _admin(request):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:
            body = {}
        url = str(body.get("url") or "").strip()
        if not url:
            return web.json_response({"ok": False, "error": "нет ссылки"}, status=400)
        import youtube

        info = await youtube.resolve_channel(url)
        if not info.get("channel_id"):
            return web.json_response({"ok": False, "error": "канал не найден"}, status=400)
        db.add_channel(url, info["channel_id"], info.get("title") or "")
        asyncio.create_task(worker.poll_channels())
        return web.json_response({"ok": True, **info})

    async def media_route(request: web.Request) -> web.StreamResponse:
        return await media.media_handler(request, telegram)

    async def index(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(MINIAPP / "index.html")

    async def static(request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        file = MINIAPP / name
        if not file.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(file)

    app.router.add_get("/health", health)
    app.router.add_get("/api/trailers/title/{title_id}", trailer_for_title)
    app.router.add_get("/api/trailers/recent", recent)
    app.router.add_get("/api/admin/state", admin_state)
    app.router.add_post("/api/admin/quality", admin_quality)
    app.router.add_post("/api/admin/poll", admin_poll)
    app.router.add_post("/api/admin/link", admin_links)
    app.router.add_post("/api/admin/retry", admin_retry)
    app.router.add_post("/api/admin/channel", admin_channel)
    app.router.add_get("/media/{message_id}", media_route)
    app.router.add_get("/", index)
    app.router.add_get("/{name}", static)
    return app
