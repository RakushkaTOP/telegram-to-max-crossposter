"""Конфигурация кросспостера Telegram → MAX. Всё из окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _clean(v: str | None) -> str:
    return (v or "").strip()


@dataclass(frozen=True)
class Config:
    # --- Telegram ---
    tg_bot_token: str            # токен бота @vastu_syncbot (BotFather)
    tg_source_chat_id: int       # ID канала-источника (напр. -1001669592486)
    tg_api_base: str             # адрес локального Bot API сервера
    tg_files_root: str           # где на диске лежат файлы локального Bot API (общий volume)

    # --- MAX ---
    max_token: str               # access-токен бота MAX
    max_chat_id: int             # ID канала-приёмника в MAX (напр. -72627929529786)
    max_api_base: str            # база REST API MAX
    max_chat_id_in_query: bool   # слать chat_id в query (совместимость) или в теле

    # --- поведение ---
    album_debounce_ms: int       # сколько ждать сборки альбома
    db_path: str                 # sqlite для дедупа

    @staticmethod
    def load() -> "Config":
        return Config(
            tg_bot_token=_clean(os.getenv("TG_BOT_TOKEN")),
            tg_source_chat_id=int(_clean(os.getenv("TG_SOURCE_CHAT_ID")) or "0"),
            tg_api_base=_clean(os.getenv("TG_API_BASE")) or "http://bot-api:8081",
            tg_files_root=_clean(os.getenv("TG_FILES_ROOT")) or "/var/lib/telegram-bot-api",
            max_token=_clean(os.getenv("MAX_TOKEN")),
            max_chat_id=int(_clean(os.getenv("MAX_CHAT_ID")) or "0"),
            max_api_base=_clean(os.getenv("MAX_API_BASE")) or "https://botapi.max.ru",
            max_chat_id_in_query=_clean(os.getenv("MAX_CHAT_ID_IN_QUERY")).lower() in ("1", "true", "yes"),
            album_debounce_ms=int(_clean(os.getenv("ALBUM_DEBOUNCE_MS")) or "1500"),
            db_path=_clean(os.getenv("DB_PATH")) or "/data/crosspost.db",
        )

    def require(self) -> None:
        missing = [k for k, v in {
            "TG_BOT_TOKEN": self.tg_bot_token,
            "TG_SOURCE_CHAT_ID": self.tg_source_chat_id,
            "MAX_TOKEN": self.max_token,
            "MAX_CHAT_ID": self.max_chat_id,
        }.items() if not v]
        if missing:
            raise SystemExit("Не заданы обязательные переменные: " + ", ".join(missing))
