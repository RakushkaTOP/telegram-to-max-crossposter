"""Дедуп обработанных постов — чтобы рестарт воркера не задваивал кросспост."""
from __future__ import annotations

import os
import sqlite3


class Store:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS done (chat_id INTEGER, message_id INTEGER, ts INTEGER, PRIMARY KEY(chat_id, message_id))"
        )
        self._db.commit()

    def is_empty(self) -> bool:
        """True, если ни один пост ещё не обработан — значит это первый старт."""
        cur = self._db.execute("SELECT 1 FROM done LIMIT 1")
        return cur.fetchone() is None

    def seen(self, chat_id: int, message_id: int) -> bool:
        cur = self._db.execute(
            "SELECT 1 FROM done WHERE chat_id=? AND message_id=?", (chat_id, message_id)
        )
        return cur.fetchone() is not None

    def mark(self, chat_id: int, message_id: int, ts: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO done(chat_id, message_id, ts) VALUES(?,?,?)",
            (chat_id, message_id, ts),
        )
        self._db.commit()
