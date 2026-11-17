from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any

import aiohttp

import youtube
from config import (
    ADMIN_IDS,
    DATA_DIR,
    DEFAULT_QUALITY,
    MAX_FILE_MB,
    POLL_MINUTES,
    TRAILER_CHAT_ID,
    YOUTUBE_CHANNELS,
)
from db import DB
from matcher import Matcher
from telegram_client import Telegram, UploadError

logger = logging.getLogger(__name__)

TEMP = DATA_DIR / "tmp"
TEMP.mkdir(parents=True, exist_ok=True)
MAX_ATTEMPTS = 3


class Worker:
    def __init__(self, db: DB, telegram: Telegram, bot) -> None:
        self.db = db
        self.telegram = telegram
        self.bot = bot
        self.matcher = Matcher()
        self.current: dict[str, Any] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        for url in YOUTUBE_CHANNELS:
            if not any(row["url"] == url for row in self.db.channels()):
                self.db.add_channel(url, "", "")
        self._task = asyncio.create_task(self._loop(), name="trailer-worker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._session:
            await self._session.close()

    def wake(self) -> None:
        self._wake.set()

    async def notify(self, text: str) -> None:
        for admin in ADMIN_IDS:
            try:
                await self.bot.send_message(admin, text)
            except Exception:
                logger.warning("notify admin failed")

    async def _loop(self) -> None:
        last_poll = 0.0
        while True:
            try:
                if time.time() - last_poll >= POLL_MINUTES * 60:
                    await self.poll_channels()
                    last_poll = time.time()
                row = self.db.next_pending()
                if row is None:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=60)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                    continue
                await self.process(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("worker loop: %s", exc)
                await asyncio.sleep(5)

    async def poll_channels(self) -> dict[str, int]:
        added = skipped = 0
        session = self._session or aiohttp.ClientSession()
        try:
            for row in self.db.channels(enabled_only=True):
                url = str(row["url"])
                channel_id = str(row["channel_id"] or "")
                title = str(row["title"] or "")
                if not channel_id:
                    try:
                        info = await youtube.resolve_channel(url)
                        channel_id = info.get("channel_id") or ""
                        title = title or info.get("title") or ""
                    except Exception:
                        channel_id = await youtube.channel_from_html(url)
                if not channel_id:
                    continue
                if not title or channel_id != row["channel_id"]:
                    self.db.add_channel(url, channel_id, title)
                entries = await youtube.fetch_feed(session, channel_id)
                for entry in entries:
                    seen = self.db.seen(entry["youtube_id"])
                    if seen and not (
                        seen["status"] == "skipped"
                        and int(seen["updated_at"] or 0) < time.time() - 6 * 3600
                    ):
                        continue
                    match = await self.matcher.find(session, entry["title"])
                    if not match:
                        if seen:
                            self.db.set_status(entry["youtube_id"], "skipped", "нет в хранилище")
                        else:
                            self.db.add_trailer(
                                {
                                    **entry,
                                    "youtube_title": entry["title"],
                                    "quality": self.db.quality(),
                                },
                                status="skipped",
                            )
                            self.db.set_status(entry["youtube_id"], "skipped", "нет в хранилище")
                        skipped += 1
                        continue
                    data = {
                        **entry,
                        "youtube_title": entry["title"],
                        "title_id": int(match["id"]),
                        "title_name": match.get("title") or "",
                        "title_year": int(match.get("year") or 0),
                        "quality": self.db.quality(),
                    }
                    if seen:
                        self.db.retry(entry["youtube_id"])
                    elif self.db.add_trailer(data):
                        added += 1
                        await self.notify(
                            f"Новый трейлер: {data['title_name']} — в очереди"
                        )
                self.db.set_channel_polled(url)
        finally:
            if session is not self._session:
                await session.close()
        if added or skipped:
            logger.info("poll: добавлено %s, пропущено %s", added, skipped)
        return {"added": added, "skipped": skipped}

    async def add_link(self, url: str, quality: int | None = None) -> dict[str, Any]:
        session = self._session or aiohttp.ClientSession()
        try:
            info = await youtube.probe(url)
            youtube_id = info.get("youtube_id") or ""
            if not youtube_id:
                return {"ok": False, "error": "не удалось прочитать видео"}
            seen = self.db.seen(youtube_id)
            if seen and seen["status"] == "done":
                return {"ok": True, "duplicate": True, "title": seen["title_name"]}
            match = await self.matcher.find(session, info.get("title") or "")
            if not match:
                return {"ok": False, "error": "фильма нет в хранилище", "youtube_title": info.get("title")}
            data = {
                "youtube_id": youtube_id,
                "title_id": int(match["id"]),
                "title_name": match.get("title") or "",
                "title_year": int(match.get("year") or 0),
                "youtube_title": info.get("title") or "",
                "published_at": int(info.get("uploaded_at") or time.time()),
                "quality": int(quality or self.db.quality()),
            }
            if seen:
                self.db.retry(youtube_id)
                self.db.set_status(youtube_id, "pending", "", quality=data["quality"])
            else:
                self.db.add_trailer(data)
            self.wake()
            return {
                "ok": True,
                "queued": True,
                "title": data["title_name"],
                "year": data["title_year"],
                "youtube_title": data["youtube_title"],
            }
        finally:
            if session is not self._session:
                await session.close()

    async def process(self, row) -> None:
        youtube_id = str(row["youtube_id"])
        title_id = int(row["title_id"] or 0)
        title_name = str(row["title_name"] or "")
        quality = int(row["quality"] or DEFAULT_QUALITY)
        attempts = int(row["attempts"] or 0) + 1
        target = TEMP / f"{youtube_id}.mp4"
        self.db.set_status(youtube_id, "downloading", "")
        self.current = {
            "youtube_id": youtube_id,
            "title": title_name,
            "status": "downloading",
            "bytes": 0,
            "total": 0,
        }
        try:
            def progress(done: int, total: int) -> None:
                self.current["bytes"] = done
                self.current["total"] = total

            path, height, width, duration = await youtube.download(
                f"https://www.youtube.com/watch?v={youtube_id}",
                quality,
                target,
                progress,
            )
            size = path.stat().st_size
            limit = MAX_FILE_MB * 1024 * 1024
            if size > limit:
                raise UploadError(f"файл {size // (1024**2)} МБ больше предела {MAX_FILE_MB} МБ")
            self.db.set_status(youtube_id, "uploading", "", size=size, height=height, width=width, duration=duration)
            self.current.update(status="uploading", bytes=0, total=size)
            caption = f"🎬 {title_name}"
            if row["title_year"]:
                caption += f" ({int(row['title_year'])})"
            caption += " · трейлер"
            message_id = await self.telegram.upload(
                path, caption, duration, width or 1280, height or 720,
                progress=lambda done, total: self.current.update(bytes=done, total=total),
            )
            self.db.set_status(
                youtube_id, "done", "", message_id=message_id, size=size,
                height=height, width=width, duration=duration,
                chat_id=TRAILER_CHAT_ID,
            )
            await self.notify(f"Трейлер загружен: {title_name}")
        except asyncio.CancelledError:
            self.db.set_status(youtube_id, "pending", "остановлено")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("trailer %s: %s", youtube_id, exc)
            if attempts >= MAX_ATTEMPTS:
                self.db.set_status(youtube_id, "failed", str(exc), attempts=attempts)
                await self.notify(f"Трейлер не загрузился: {title_name} — {str(exc)[:160]}")
            else:
                self.db.set_status(youtube_id, "pending", str(exc), attempts=attempts)
        finally:
            self.current = {}
            try:
                for file in TEMP.glob(f"{youtube_id}.*"):
                    file.unlink(missing_ok=True)
            except OSError:
                pass
            shutil.rmtree(TEMP / youtube_id, ignore_errors=True)
