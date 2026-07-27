"""Воркер кросспостинга: канал Telegram → канал MAX.

Слушает channel_post из TG-канала-источника через ЛОКАЛЬНЫЙ Bot API сервер
(нужен для видео >20 МБ), скачивает медиа с общего тома, перезаливает в MAX
и постит одним сообщением. Альбомы (media_group) собираются с дебаунсом.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import Message

from app.config import Config
from app.max_client import MaxClient
from app.store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("crosspost")

cfg = Config.load()
cfg.require()

store = Store(cfg.db_path)
maxc = MaxClient(cfg.max_token, cfg.max_api_base, cfg.max_chat_id, cfg.max_chat_id_in_query)

session = AiohttpSession(api=TelegramAPIServer.from_base(cfg.tg_api_base, is_local=True))
bot = Bot(cfg.tg_bot_token, session=session)
dp = Dispatcher()

# буфер альбомов: media_group_id -> {"msgs": [...], "task": asyncio.Task}
_albums: dict[str, dict[str, Any]] = defaultdict(dict)


def _extract_media(m: Message) -> tuple[str, str] | None:
    """Возвращает (kind, file_id) первого медиа в сообщении или None."""
    if m.photo:
        return "photo", m.photo[-1].file_id
    if m.video:
        return "video", m.video.file_id
    if m.animation:
        return "animation", m.animation.file_id
    if m.document:
        return "document", m.document.file_id
    if m.audio:
        return "audio", m.audio.file_id
    if m.voice:
        return "voice", m.voice.file_id
    if m.video_note:
        return "video_note", m.video_note.file_id
    return None


def _text_of(m: Message) -> str:
    return (m.caption or m.text or "").strip()


async def _read_local_file(file_id: str) -> tuple[bytes, str, str] | None:
    """Скачивает файл через локальный Bot API и читает его с диска.
    Возвращает (bytes, filename, content_type)."""
    try:
        f = await bot.get_file(file_id)
    except Exception as e:  # noqa: BLE001
        log.error("get_file(%s) упал: %s", file_id, e)
        return None
    path = f.file_path or ""
    # локальный сервер отдаёт абсолютный путь; при относительном — префиксуем корнем тома
    candidates = [path]
    if not os.path.isabs(path):
        candidates.append(os.path.join(cfg.tg_files_root, path))
    else:
        # если путь абсолютный, но том смонтирован в другой корень — попробуем срез
        candidates.append(os.path.join(cfg.tg_files_root, path.lstrip("/")))
    for p in candidates:
        if p and os.path.exists(p):
            with open(p, "rb") as fh:
                data = fh.read()
            return data, os.path.basename(p), "application/octet-stream"
    log.error("файл не найден на диске: file_path=%s (пробовал %s)", path, candidates)
    return None


async def _build_attachment(m: Message) -> dict[str, Any] | None:
    media = _extract_media(m)
    if not media:
        return None
    kind, file_id = media
    got = await _read_local_file(file_id)
    if not got:
        return None
    data, filename, ctype = got
    return await maxc.upload_media(kind, filename, data, ctype)


async def _process(messages: list[Message]) -> None:
    """Постит группу сообщений (1 шт. или альбом) в MAX одним сообщением."""
    messages = sorted(messages, key=lambda x: x.message_id)
    head = messages[0]
    if store.seen(head.chat.id, head.message_id):
        return

    text = ""
    for m in messages:
        t = _text_of(m)
        if t:
            text = t
            break

    attachments: list[dict[str, Any]] = []
    for m in messages:
        att = await _build_attachment(m)
        if att:
            attachments.append(att)

    if not text and not attachments:
        # нечего переносить (стикер/опрос/сервисное) — помечаем как обработанное
        store.mark(head.chat.id, head.message_id, int(time.time()))
        log.info("пропуск %s/%s: нет переносимого контента", head.chat.id, head.message_id)
        return

    ok = await maxc.send(text, attachments)
    if ok:
        for m in messages:
            store.mark(m.chat.id, m.message_id, int(time.time()))
        log.info("кросспост %s/%s → MAX ok (%d медиа)", head.chat.id, head.message_id, len(attachments))
    else:
        log.error("кросспост %s/%s → MAX НЕ отправлен", head.chat.id, head.message_id)


async def _flush_album(group_id: str) -> None:
    await asyncio.sleep(cfg.album_debounce_ms / 1000)
    bundle = _albums.pop(group_id, None)
    if not bundle:
        return
    await _process(bundle["msgs"])


@dp.channel_post(F.chat.id == cfg.tg_source_chat_id)
async def on_channel_post(m: Message) -> None:
    if m.media_group_id:
        b = _albums[m.media_group_id]
        b.setdefault("msgs", []).append(m)
        if not b.get("task"):
            b["task"] = asyncio.create_task(_flush_album(m.media_group_id))
        return
    await _process([m])


async def main() -> None:
    me = await bot.get_me()
    log.info("старт: бот @%s, источник TG %s → MAX %s (api=%s)",
             me.username, cfg.tg_source_chat_id, cfg.max_chat_id, cfg.tg_api_base)
    # ВАЖНО (прод): сбрасываем накопившуюся очередь апдейтов, чтобы при первом старте
    # не задваивать/не заливать в MAX старые посты канала. Ловим только новое — с этого момента.
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("pending updates сброшены — ловим только новые посты")
    except Exception as e:  # noqa: BLE001
        log.warning("не удалось сбросить pending updates: %s", e)
    try:
        # только channel_post/edited_channel_post нужны
        await dp.start_polling(bot, allowed_updates=["channel_post", "edited_channel_post"])
    finally:
        await maxc.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
