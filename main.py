from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiohttp import web

import web as webapp
from config import ADMIN_IDS, BOT_TOKEN, PORT, PUBLIC_URL
from db import DB
from telegram_client import Telegram
from worker import Worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("trailers")


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_IDS)


async def main() -> None:
    db = DB()
    telegram = Telegram()
    await telegram.start()
    bot = Bot(BOT_TOKEN)
    worker = Worker(db, telegram, bot)
    await worker.start()

    dispatcher = Dispatcher()

    @dispatcher.message(CommandStart())
    async def start(message: Message) -> None:
        if not is_admin(message):
            await message.answer("Приватный бот.")
            return
        if not PUBLIC_URL:
            await message.answer("Мини-апп недоступен: не задан PUBLIC_URL.")
            return
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть 720", web_app=WebAppInfo(url=PUBLIC_URL + "/"))
        ]])
        await message.answer(
            "Архив трейлеров.\n\n"
            "Внутри: ссылки, качество по умолчанию, очередь и статусы.",
            reply_markup=markup,
        )

    @dispatcher.message(Command("status"))
    async def status(message: Message) -> None:
        if not is_admin(message):
            return
        stats = db.stats()
        current = worker.current or {}
        text = (
            f"Готово: {stats.get('done', 0)}\n"
            f"В очереди: {stats.get('pending', 0)}\n"
            f"Ошибок: {stats.get('failed', 0)}\n"
            f"Качество: {db.quality()}p"
        )
        if current:
            text += f"\nСейчас: {current.get('title') or current.get('youtube_id')} · {current.get('status')}"
        await message.answer(text)

    @dispatcher.message(Command("quality"))
    async def quality(message: Message) -> None:
        if not is_admin(message):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip().isdigit():
            value = int(parts[1].strip())
            if value in {480, 720, 1080, 1440, 2160}:
                db.set_quality(value)
                await message.answer(f"Качество по умолчанию: {value}p")
                return
        await message.answer(
            f"Текущее: {db.quality()}p\nПоставить: /quality 720"
        )

    @dispatcher.message(Command("poll"))
    async def poll(message: Message) -> None:
        if not is_admin(message):
            return
        asyncio.create_task(worker.poll_channels())
        await message.answer("Проверяю каналы.")

    @dispatcher.message(F.text.regexp(r"https?://"))
    async def link(message: Message) -> None:
        if not is_admin(message):
            return
        result = await worker.add_link(message.text or "")
        if result.get("ok"):
            suffix = "уже было" if result.get("duplicate") else "в очереди"
            await message.answer(f"{result.get('title')} — {suffix}")
        else:
            await message.answer(f"Не взял: {result.get('error')}")

    app = webapp.create_app(db, telegram, worker)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("web on %s", PORT)

    try:
        await dispatcher.start_polling(bot)
    finally:
        await worker.stop()
        await runner.cleanup()
        await telegram.stop()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
