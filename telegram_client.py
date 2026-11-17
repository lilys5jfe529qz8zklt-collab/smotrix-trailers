from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Callable

from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError, ServerError
from telethon.helpers import generate_random_long

from config import API_HASH, API_ID, BOT_TOKEN, DATA_DIR, TRAILER_CHAT_ID

logger = logging.getLogger(__name__)

PART = 512 * 1024
BIG_THRESHOLD = 10 * 1024 * 1024
WORKERS = max(1, min(16, int(os.getenv("UPLOAD_WORKERS", "8") or 8)))
PART_TIMEOUT = max(60, int(os.getenv("UPLOAD_PART_TIMEOUT", "180") or 180))
PART_RETRIES = max(1, min(8, int(os.getenv("UPLOAD_PART_RETRIES", "5") or 5)))


class UploadError(RuntimeError):
    pass


async def _upload_parts(client, path: Path, progress: Callable[[int, int], None] | None = None):
    size = path.stat().st_size
    if size <= 0:
        raise UploadError("пустой файл")
    part_count = (size + PART - 1) // PART
    workers = min(WORKERS, part_count)
    file_id = generate_random_long()
    is_big = size > BIG_THRESHOLD
    checksum = hashlib.md5()
    sent = 0
    queue: asyncio.Queue = asyncio.Queue(maxsize=workers)

    async def send_part(index: int, chunk: bytes) -> None:
        for attempt in range(PART_RETRIES):
            request = (
                functions.upload.SaveBigFilePartRequest(file_id, index, part_count, chunk)
                if is_big
                else functions.upload.SaveFilePartRequest(file_id, index, chunk)
            )
            try:
                if await asyncio.wait_for(client(request), PART_TIMEOUT):
                    return
                if attempt + 1 == PART_RETRIES:
                    raise UploadError(f"Telegram не принял часть {index + 1}/{part_count}")
                delay = 0.25 * 2 ** attempt
            except FloodWaitError as exc:
                if attempt + 1 == PART_RETRIES or exc.seconds > 60:
                    raise UploadError(f"Telegram FloodWait {exc.seconds} с") from exc
                delay = max(0.1, exc.seconds)
            except (TimeoutError, OSError, ServerError) as exc:
                if attempt + 1 == PART_RETRIES:
                    raise UploadError(f"часть {index + 1}/{part_count} не загрузилась") from exc
                delay = 0.25 * 2 ** attempt
            await asyncio.sleep(delay)

    stream = path.open("rb")

    def read_exact(count: int) -> bytes:
        data = stream.read(count)
        if len(data) != count:
            raise UploadError("поток файла закончился раньше размера")
        return data

    async def produce() -> None:
        for index in range(part_count):
            count = min(PART, size - index * PART)
            chunk = await asyncio.to_thread(read_exact, count)
            if not is_big:
                checksum.update(chunk)
            await queue.put((index, chunk))
        for _ in range(workers):
            await queue.put(None)

    async def consume() -> None:
        nonlocal sent
        while True:
            part = await queue.get()
            if part is None:
                return
            index, chunk = part
            await send_part(index, chunk)
            sent += len(chunk)
            if progress:
                progress(sent, size)

    tasks = [asyncio.create_task(produce())]
    tasks += [asyncio.create_task(consume()) for _ in range(workers)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stream.close()

    filename = path.name
    if is_big:
        return types.InputFileBig(file_id, part_count, filename)
    return types.InputFile(file_id, part_count, filename, checksum.hexdigest())


class Telegram:
    def __init__(self) -> None:
        self.client = TelegramClient(
            str(DATA_DIR / "trailer-bot.session"),
            API_ID,
            API_HASH,
            connection_retries=None,
            request_retries=5,
            flood_sleep_threshold=60,
            auto_reconnect=True,
        )
        self._sizes: dict[int, int] = {}

    async def start(self) -> None:
        await self.client.start(bot_token=BOT_TOKEN)
        await self.client.get_entity(TRAILER_CHAT_ID)
        logger.info("Telegram подключён, канал %s", TRAILER_CHAT_ID)

    async def stop(self) -> None:
        await self.client.disconnect()

    async def upload(
        self,
        path: Path,
        caption: str,
        duration: int,
        width: int,
        height: int,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        uploaded = await _upload_parts(self.client, path, progress)
        message = await self.client.send_file(
            TRAILER_CHAT_ID,
            uploaded,
            caption=caption[:1024],
            parse_mode="html",
            attributes=[
                types.DocumentAttributeVideo(
                    duration=int(duration or 0),
                    w=int(width or 1280),
                    h=int(height or 720),
                    supports_streaming=True,
                )
            ],
            supports_streaming=True,
            force_document=False,
        )
        return int(message.id)

    async def size(self, message_id: int) -> int:
        cached = self._sizes.get(message_id)
        if cached:
            return cached
        message = await self.client.get_messages(TRAILER_CHAT_ID, ids=int(message_id))
        document = getattr(getattr(message, "media", None), "document", None)
        size = int(getattr(document, "size", 0) or 0)
        if size <= 0:
            raise UploadError("файл не найден в канале")
        self._sizes[message_id] = size
        return size

    async def stream(self, message_id: int, start: int, length: int | None, response) -> None:
        message = await self.client.get_messages(TRAILER_CHAT_ID, ids=int(message_id))
        if not message or not getattr(message, "media", None):
            raise UploadError("файл не найден в канале")
        remaining = length
        chunks = None if length is None else max(1, (length + PART - 1) // PART)
        async for chunk in self.client.iter_download(
            message.media,
            offset=start,
            limit=chunks,
            chunk_size=PART,
        ):
            if remaining is not None and len(chunk) > remaining:
                chunk = chunk[:remaining]
            await response.write(chunk)
            if remaining is not None:
                remaining -= len(chunk)
                if remaining <= 0:
                    break

    async def delete(self, message_id: int) -> None:
        try:
            await self.client.delete_messages(TRAILER_CHAT_ID, [int(message_id)])
        except Exception:
            pass
