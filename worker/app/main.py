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

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import Message

from app.config import Config
from app.markup import append_buttons, build_parts
from app.max_client import MaxClient
from app.store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("crosspost")
# httpx на INFO печатает полный URL каждого запроса — лишний шум в логах контейнера
# (и риск утащить в них query-параметры). Ошибки транспорта видны и на WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)

cfg = Config.load()
cfg.require()

store = Store(cfg.db_path)
maxc = MaxClient(cfg.max_token, cfg.max_api_base, cfg.max_chat_id_in_query)

session = AiohttpSession(api=TelegramAPIServer.from_base(cfg.tg_api_base, is_local=True))
bot = Bot(cfg.tg_bot_token, session=session)
dp = Dispatcher()

# буфер альбомов: (chat_id, media_group_id) -> {"msgs": [...], "task": asyncio.Task}
_albums: dict[tuple[int, str], dict[str, Any]] = defaultdict(dict)


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


def _text_of(m: Message) -> tuple[str, Any]:
    """Текст поста и его entities (разметка). У медиа они в caption_*."""
    if m.caption:
        return m.caption, m.caption_entities
    if m.text:
        return m.text, m.entities
    if m.poll:
        # опрос вложением MAX не принимает — переносим содержимым, иначе пост пропадёт
        opts = "\n".join(f"• {o.text}" for o in m.poll.options)
        return f"{m.poll.question}\n\n{opts}", None
    return "", None


def _buttons_of(m: Message) -> list[tuple[str, str]]:
    """Пары (подпись, ссылка) из inline-клавиатуры поста."""
    kb = getattr(m.reply_markup, "inline_keyboard", None) if m.reply_markup else None
    return [(b.text, b.url) for row in (kb or []) for b in row if getattr(b, "url", None)]


async def _read_local_file(file_id: str) -> tuple[bytes, str, str, str] | None:
    """Скачивает файл через локальный Bot API и читает его с диска.
    Возвращает (bytes, filename, content_type, path_on_disk)."""
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
            return data, os.path.basename(p), "application/octet-stream", p
    log.error("файл не найден на диске: file_path=%s (пробовал %s)", path, candidates)
    return None


def _cleanup(paths: list[str]) -> None:
    """Удаляет скачанные Bot API файлы.

    Локальный Bot API складывает всё в свой том и НИКОГДА не чистит его сам —
    на канале с видео это гарантированно съело бы диск (а на боксе рядом живут
    другие сервисы). Файл нужен только до перезалива в MAX, дальше он мусор.
    """
    for p in paths:
        # страховка: сносим только внутри тома Bot API, ничего снаружи
        if not os.path.abspath(p).startswith(os.path.abspath(cfg.tg_files_root)):
            log.warning("отказ удалять файл вне тома Bot API: %s", p)
            continue
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("не удалось удалить %s: %s", p, e)


async def _build_attachment(m: Message) -> tuple[dict[str, Any] | None, str | None]:
    """Возвращает (attachment для MAX, путь скачанного файла — его потом надо убрать)."""
    media = _extract_media(m)
    if not media:
        return None, None
    kind, file_id = media
    got = await _read_local_file(file_id)
    if not got:
        return None, None
    data, filename, ctype, path = got
    return await maxc.upload_media(kind, filename, data, ctype), path


async def _process(messages: list[Message], dst_chat_id: int) -> None:
    """Постит группу сообщений (1 шт. или альбом) в MAX-канал dst_chat_id одним сообщением."""
    messages = sorted(messages, key=lambda x: x.message_id)
    head = messages[0]
    if store.seen(head.chat.id, head.message_id):
        return

    text, entities = "", None
    for m in messages:
        t, ents = _text_of(m)
        if t:
            text, entities = t, ents
            break
    # разметка Telegram → HTML MAX; длинные посты режутся под лимит в 4000 символов
    parts = build_parts(text, entities)
    parts = append_buttons(parts, _buttons_of(head))

    attachments: list[dict[str, Any]] = []
    downloaded: list[str] = []
    for m in messages:
        att, path = await _build_attachment(m)
        if path:
            downloaded.append(path)
        if att:
            attachments.append(att)

    if not parts and not attachments:
        # нечего переносить (стикер/сервисное сообщение) — помечаем как обработанное
        store.mark(head.chat.id, head.message_id, int(time.time()))
        _cleanup(downloaded)
        log.info("пропуск %s/%s: нет переносимого контента", head.chat.id, head.message_id)
        return

    mids: list[str] = []
    try:
        # вложения уходят с первой частью, хвост длинного текста — следом
        mids = await maxc.send(dst_chat_id, parts[0] if parts else "", attachments, fmt="html")
        ok = bool(mids)
        for tail in parts[1:]:
            more = await maxc.send(dst_chat_id, tail, None, fmt="html")
            if more:
                mids += more
            else:
                log.error("хвост длинного поста %s/%s не ушёл", head.chat.id, head.message_id)
    finally:
        # чистим и после провала: MAX всё равно уже получил копию либо пост потерян,
        # а держать оригинал в томе Bot API смысла нет — только диск занимать
        _cleanup(downloaded)
    if ok:
        now = int(time.time())
        for m in messages:
            store.mark(m.chat.id, m.message_id, now)
            # правка любого кадра альбома должна найти цель в MAX — помним для каждого
            store.remember_sent(m.chat.id, m.message_id, mids, now)
        log.info("кросспост %s/%s → MAX %s ok (%d медиа)",
                 head.chat.id, head.message_id, dst_chat_id, len(attachments))
    else:
        log.error("кросспост %s/%s → MAX %s НЕ отправлен", head.chat.id, head.message_id, dst_chat_id)


async def _flush_album(key: tuple[int, str], dst_chat_id: int) -> None:
    try:
        await asyncio.sleep(cfg.album_debounce_ms / 1000)
    except asyncio.CancelledError:
        return  # приехал ещё один кадр альбома — отправку перенёс новый таймер
    bundle = _albums.pop(key, None)
    if not bundle:
        return
    msgs = bundle.get("msgs") or []
    log.info("альбом %s собран: %d сообщений", key[1], len(msgs))
    await _process(msgs, dst_chat_id)


@dp.channel_post()
async def on_channel_post(m: Message) -> None:
    dst = cfg.routes.get(m.chat.id)
    if dst is None:
        # чужой чат: бот мог попасть в канал, которого нет в маршрутах. Молчать нельзя —
        # именно по этой строке узнаётся chat_id нового (в т.ч. тестового) канала.
        log.info("источник вне маршрутов: chat_id=%s title=%r — пост пропущен",
                 m.chat.id, m.chat.title)
        return
    if m.media_group_id:
        # ключ с chat_id: media_group_id уникален у Telegram, но так альбомы разных
        # каналов гарантированно не смешаются в одну пачку
        key = (m.chat.id, m.media_group_id)
        b = _albums[key]
        b.setdefault("msgs", []).append(m)
        # дебаунс скользящий: каждый новый кадр альбома отодвигает отправку.
        # С фиксированным таймером от первого кадра большой альбом (или медленная
        # доставка) разрывался бы на два поста в MAX.
        task = b.get("task")
        if task:
            task.cancel()
        b["task"] = asyncio.create_task(_flush_album(key, dst))
        return
    await _process([m], dst)


@dp.edited_channel_post()
async def on_edited_channel_post(m: Message) -> None:
    """Правка поста в Telegram → правка того же сообщения в MAX.

    Медиа не переотправляем: правят почти всегда текст, а перезалив видео ради
    исправленной опечатки стоил бы минуты и трафика.
    """
    if cfg.routes.get(m.chat.id) is None:
        return
    mids = store.max_mids(m.chat.id, m.message_id)
    if not mids:
        log.info("правка %s/%s: соответствия в MAX нет (пост до внедрения правок) — пропуск",
                 m.chat.id, m.message_id)
        return
    text, entities = _text_of(m)
    parts = append_buttons(build_parts(text, entities), _buttons_of(m))
    if not parts:
        return
    if await maxc.edit_message(mids[0], parts[0], fmt="html"):
        log.info("правка %s/%s применена в MAX (mid=%s)", m.chat.id, m.message_id, mids[0])
    else:
        log.error("правка %s/%s не применилась", m.chat.id, m.message_id)
    if len(parts) > 1:
        log.warning("правка %s/%s: текст снова длиннее лимита, хвост в MAX не обновлён",
                    m.chat.id, m.message_id)


async def main() -> None:
    me = await bot.get_me()
    log.info("старт: бот @%s, api=%s, маршрутов %d", me.username, cfg.tg_api_base, len(cfg.routes))
    for src, dst in cfg.routes.items():
        log.info("маршрут: TG %s → MAX %s", src, dst)
    # самопроверка MAX-токена: чтобы при первом тесте сразу видеть, чей токен не тот
    who = await maxc.whoami()
    if who:
        log.info("MAX-бот ok: %s", who.get("username") or who.get("name") or who)
    else:
        log.error("MAX-токен не прошёл проверку /me — кросспост будет падать на отправке")
    # Очередь апдейтов сбрасываем ТОЛЬКО на первом старте (база дедупа пуста), чтобы
    # не залить в MAX всю историю канала разом. На последующих рестартах сброс вреден:
    # посты, пришедшие пока воркер лежал, потерялись бы. От задваивания страхует дедуп.
    first_start = store.is_empty()
    try:
        await bot.delete_webhook(drop_pending_updates=first_start)
        log.info(
            "первый старт — pending updates сброшены, ловим только новые посты"
            if first_start else
            "рестарт — очередь апдейтов сохранена, догоним пропущенное (дедуп по sqlite)"
        )
    except Exception as e:  # noqa: BLE001
        log.warning("не удалось снять webhook: %s", e)
    try:
        # только channel_post/edited_channel_post нужны
        await dp.start_polling(bot, allowed_updates=["channel_post", "edited_channel_post"])
    finally:
        await maxc.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
