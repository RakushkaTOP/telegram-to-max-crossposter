"""Минимальный клиент MAX Bot API (https://dev.max.ru/docs-api).

Поток медиа двухшаговый:
  1) POST {base}/uploads?type=<image|video|audio|file>  ->  {"url": "<upload_url>"}
  2) POST <upload_url> (multipart с бинарником)          ->  токен вложения
Затем сообщение: POST {base}/messages  с телом {text, chat_id, attachments}.
Авторизация — заголовком  Authorization: <token>  (query-параметр токена задепрекейчен).

Формы ответа upload различаются по типу, поэтому _extract_token разбирает несколько
вариантов и подробно логирует сырой ответ — это ускорит первый живой тест.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("max")

# TG-тип медиа -> тип загрузки MAX
UPLOAD_TYPE = {
    "photo": "image",
    "image": "image",
    "video": "video",
    "animation": "video",
    "video_note": "video",
    "audio": "audio",
    "voice": "audio",
    "document": "file",
}


class MaxClient:
    def __init__(self, token: str, base: str, chat_id: int, chat_id_in_query: bool = False) -> None:
        self._token = token
        self._base = base.rstrip("/")
        self._chat_id = chat_id
        self._chat_id_in_query = chat_id_in_query
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(120.0), headers={"Authorization": token})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def upload_media(self, kind: str, filename: str, data: bytes, content_type: str) -> dict[str, Any] | None:
        """Загружает один файл, возвращает готовый объект attachment или None при неудаче."""
        up_type = UPLOAD_TYPE.get(kind, "file")
        # шаг 1 — получить upload url
        r = await self._http.post(f"{self._base}/uploads", params={"type": up_type})
        if r.status_code >= 300:
            log.error("uploads init failed %s: %s", r.status_code, r.text[:400])
            return None
        url = (r.json() or {}).get("url")
        if not url:
            log.error("uploads init: нет url в ответе: %s", r.text[:400])
            return None
        # шаг 2 — залить бинарник
        files = {"data": (filename, data, content_type)}
        r2 = await self._http.post(url, files=files)
        if r2.status_code >= 300:
            log.error("upload body failed %s: %s", r2.status_code, r2.text[:400])
            return None
        try:
            payload = r2.json()
        except Exception:
            log.error("upload body: не JSON: %s", r2.text[:400])
            return None
        token = self._extract_token(payload)
        if not token:
            log.error("upload body: не нашёл токен в ответе: %s", str(payload)[:600])
            return None
        # image допускает payload {"token": ...}; video/file/audio — {"token": ...}
        att_type = "image" if up_type == "image" else up_type
        return {"type": att_type, "payload": {"token": token}}

    @staticmethod
    def _extract_token(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        if isinstance(payload.get("token"), str):
            return payload["token"]
        # image возвращает {"photos": {"<id>": {"token": "..."}}}
        photos = payload.get("photos")
        if isinstance(photos, dict):
            for v in photos.values():
                if isinstance(v, dict) and isinstance(v.get("token"), str):
                    return v["token"]
        # иногда вложено в {"result": {...}}
        res = payload.get("result")
        if isinstance(res, dict):
            return MaxClient._extract_token(res)
        return None

    async def send(self, text: str, attachments: list[dict[str, Any]] | None) -> bool:
        body: dict[str, Any] = {}
        params: dict[str, Any] = {}
        if self._chat_id_in_query:
            params["chat_id"] = self._chat_id
        else:
            body["chat_id"] = self._chat_id
        if text:
            body["text"] = text
        if attachments:
            body["attachments"] = attachments
        if not text and not attachments:
            return True  # нечего слать
        r = await self._http.post(f"{self._base}/messages", params=params, json=body)
        if r.status_code >= 300:
            log.error("send failed %s: %s", r.status_code, r.text[:500])
            return False
        log.info("MAX ← отправлено (attachments=%d, text=%d)", len(attachments or []), len(text or ""))
        return True
