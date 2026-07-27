# crossposting-tg-max

Авто-перенос постов из канала **Telegram** в канал **MAX**. Пересобран с нуля
2026-07-27 (предыдущая версия жила на VDS `2.27.10.248`, который стёрли —
исходник и `.env` утеряны вместе с ним).

## Как работает
```
TG-канал ──channel_post──▶ бот (@vastu_syncbot) через ЛОКАЛЬНЫЙ Bot API
                                   │ download (видео >20 МБ — потому и локальный сервер)
                                   ▼
                          перезалив в MAX (/uploads → token → /messages)
                                   ▼
                              MAX-канал
```
Два контейнера: `crosspost-bot-api` (локальный Telegram Bot API, `--local`) и
`crosspost-tg-worker` (Python/aiogram + httpx). Альбомы собираются с дебаунсом,
дедуп постов — в sqlite (`/data/crosspost.db`), рестарт не задваивает.

## Запуск
1. `cp .env.example .env` и заполнить: `TG_BOT_TOKEN`, `TG_SOURCE_CHAT_ID`,
   `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`, `MAX_TOKEN`, `MAX_CHAT_ID`.
2. Бот TG — админ канала-источника; бот MAX — админ канала-приёмника.
3. `docker compose up -d --build`
4. Логи: `docker compose logs -f crosspost-tg-worker`

## Прод
Развёрнут в `/opt/crossposting-tg-max` на Hetzner `62.238.27.80` (там же
телеграм-бот школы). Порт наружу не публикуется, всё по внутренней сети compose.

## Что проверить при первом живом тесте
- MAX `/uploads` — форма ответа (token vs photos.*.token) разобрана в
  `max_client._extract_token`, но при новой форме смотреть логи `upload body:`.
- Если `/messages` или `/uploads` дают 404 — сменить `MAX_API_BASE` на
  `https://platform-api2.max.ru`.
- Если MAX ругается на chat_id — переключить `MAX_CHAT_ID_IN_QUERY=true`.
