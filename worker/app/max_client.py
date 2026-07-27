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

import asyncio
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
    # сколько раз ждать, пока MAX дожуёт загруженное видео (attachment.not.ready)
    NOT_READY_ATTEMPTS = 12

    def __init__(self, token: str, base: str, chat_id: int, chat_id_in_query: bool = False) -> None:
        self._token = token
        self._base = base.rstrip("/")
        self._chat_id = chat_id
        self._chat_id_in_query = chat_id_in_query
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(120.0), headers={"Authorization": token})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def whoami(self) -> dict[str, Any] | None:
        """GET /me — проверка, что MAX-токен валиден. Возвращает тело ответа или None."""
        r = await self._http.get(f"{self._base}/me")
        if r.status_code >= 300:
            log.error("MAX /me failed %s: %s", r.status_code, r.text[:300])
            return None
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return None

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

    def _message_request(self, text: str, attachments: list[dict[str, Any]] | None,
                         chat_id_in_query: bool) -> tuple[dict[str, Any], dict[str, Any]]:
        """Собирает (params, body) для POST /messages при выбранном способе адресации."""
        body: dict[str, Any] = {}
        params: dict[str, Any] = {}
        if chat_id_in_query:
            params["chat_id"] = self._chat_id
        else:
            body["chat_id"] = self._chat_id
        if text:
            body["text"] = text
        if attachments:
            body["attachments"] = attachments
        return params, body

    async def _post_once(self, text: str, attachments: list[dict[str, Any]] | None) -> httpx.Response:
        """POST /messages с ожиданием готовности вложений.

        MAX обрабатывает загруженное медиа асинхронно (особенно видео): сразу после
        /uploads токен ещё «сырой», и сервер отвечает 400 attachment.not.ready.
        Это не ошибка, а «подожди» — повторяем, пока не дозреет.
        """
        params, body = self._message_request(text, attachments, self._chat_id_in_query)
        delay = 2.0
        for attempt in range(1, self.NOT_READY_ATTEMPTS + 1):
            r = await self._http.post(f"{self._base}/messages", params=params, json=body)
            if not (r.status_code == 400 and "not.ready" in r.text):
                return r
            if attempt == self.NOT_READY_ATTEMPTS:
                log.error("вложение так и не дозрело за %d попыток", attempt)
                return r
            log.info("вложение ещё обрабатывается на стороне MAX — попытка %d/%d через %.0fс",
                     attempt, self.NOT_READY_ATTEMPTS, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 15.0)
        return r  # недостижимо, но пусть тип будет честным

    async def send(self, text: str, attachments: list[dict[str, Any]] | None) -> bool:
        if not text and not attachments:
            return True  # нечего слать
        r = await self._post_once(text, attachments)
        # Проверено на живом API 2026-07-27: chat_id В ТЕЛЕ даёт 400 "Unknown recipient",
        # рабочий способ — query. Если сервер не узнал адресата, пробуем второй способ,
        # чтобы смена формата на их стороне не роняла кросспост молча.
        if r.status_code == 400 and "recipient" in r.text.lower():
            alt = not self._chat_id_in_query
            log.warning("MAX не узнал адресата (chat_id_in_query=%s) — повтор с chat_id_in_query=%s",
                        self._chat_id_in_query, alt)
            params, body = self._message_request(text, attachments, alt)
            r = await self._http.post(f"{self._base}/messages", params=params, json=body)
            if r.status_code < 300:
                self._chat_id_in_query = alt  # запоминаем рабочий способ до конца жизни процесса
                log.warning("рабочий способ адресации: chat_id_in_query=%s — поправь MAX_CHAT_ID_IN_QUERY в .env", alt)
        # Альбом одним сообщением — предпочтительно, но MAX может не принять пачку
        # (особенно разнотипную: фото+видео). Тогда лучше разложить на отдельные
        # сообщения, чем потерять пост целиком.
        if r.status_code >= 300 and attachments and len(attachments) > 1:
            kinds = {a.get("type") for a in attachments}
            log.warning("альбом (%d вложений, типы %s) не принят: %s %s — раскладываю по одному",
                        len(attachments), sorted(k for k in kinds if k), r.status_code, r.text[:200])
            return await self._send_split(text, attachments)
        if r.status_code >= 300:
            log.error("send failed %s: %s", r.status_code, r.text[:500])
            return False
        log.info("MAX ← отправлено (attachments=%d, text=%d) mid=%s",
                 len(attachments or []), len(text or ""), self._mid_of(r))
        return True

    async def _send_split(self, text: str, attachments: list[dict[str, Any]]) -> bool:
        """Фолбэк для альбома: каждое вложение — отдельным сообщением (текст идёт с первым)."""
        sent = 0
        for i, att in enumerate(attachments):
            r = await self._post_once(text if i == 0 else "", [att])
            if r.status_code >= 300:
                log.error("split-часть %d/%d не отправлена: %s %s",
                          i + 1, len(attachments), r.status_code, r.text[:300])
                continue
            sent += 1
            log.info("MAX ← отправлена часть %d/%d mid=%s", i + 1, len(attachments), self._mid_of(r))
        if sent:
            log.info("альбом доставлен по частям: %d из %d", sent, len(attachments))
        return sent > 0

    @staticmethod
    def _mid_of(r: httpx.Response) -> str | None:
        """Достаёт mid отправленного сообщения — по нему можно удалить тестовый пост."""
        try:
            msg = (r.json() or {}).get("message") or {}
            return (msg.get("body") or {}).get("mid") or msg.get("mid")
        except Exception:  # noqa: BLE001
            return None

    async def delete_message(self, mid: str) -> bool:
        """Удаляет сообщение в MAX по его mid (для очистки тестового поста)."""
        r = await self._http.delete(f"{self._base}/messages", params={"message_id": mid})
        ok = r.status_code < 300
        log.info("MAX delete mid=%s -> %s %s", mid, r.status_code, "" if ok else r.text[:300])
        return ok
