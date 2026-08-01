#!/usr/bin/env bash
# ============================================================================
#  Установщик кросспостинга Telegram → MAX для Linux-серверов.
#
#  Одной командой:
#    bash <(curl -fsSL https://raw.githubusercontent.com/RakushkaTOP/telegram-to-max-crossposter/master/install.sh)
#
#  Что делает: проверяет Docker (предложит поставить), клонирует репозиторий
#  в /opt/telegram-to-max-crossposter, спрашивает токены, собирает .env,
#  запускает контейнеры и подсказывает, как узнать ID каналов.
#
#  Повторный запуск = обновление: код подтянется, .env останется как был.
#
#  Всё можно задать заранее переменными окружения (тогда вопросов не будет):
#    TG_BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, MAX_TOKEN,
#    TG_SOURCE_CHAT_ID, MAX_CHAT_ID, INSTALL_DIR
# ============================================================================
set -euo pipefail

REPO_URL="https://github.com/RakushkaTOP/telegram-to-max-crossposter.git"
INSTALL_DIR="${INSTALL_DIR:-/opt/telegram-to-max-crossposter}"
DRY_RUN="${DRY_RUN:-}"   # DRY_RUN=1 — прогон без docker (для отладки самого скрипта)

# ── оформление ──────────────────────────────────────────────────────────────
say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ✔\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m ✘ %s\033[0m\n' "$*" >&2; exit 1; }

# ── вопросы: читаем с терминала, чтобы работало и через `curl | bash` ───────
# Наличие /dev/tty проверяем реальной попыткой открыть: по ssh без -t файл
# «существует и читаем», но открытие падает с ENXIO — [ -r ] тут врёт.
tty_ok() { { exec 9< /dev/tty; } 2>/dev/null && { exec 9<&-; return 0; }; return 1; }

ask() { # ask "текст" VAR [обязательное]
    local prompt="$1" var="$2" required="${3:-}" val=""
    # значение уже задано переменной окружения — не спрашиваем
    if [ -n "${!var:-}" ]; then return 0; fi
    if ! tty_ok; then
        [ -z "$required" ] && return 0  # необязательное — молча пропускаем
        die "Нет терминала для вопросов. Задай $var переменной окружения и перезапусти."
    fi
    while :; do
        printf '\033[1m%s\033[0m ' "$prompt" > /dev/tty
        IFS= read -r val < /dev/tty || die "Ввод прерван"
        val="$(printf '%s' "$val" | tr -d '[:space:]')"
        if [ -n "$val" ] || [ -z "$required" ]; then break; fi
        warn "Это поле обязательное."
    done
    printf -v "$var" '%s' "$val"
}

compose() {
    if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi
}

# ── проверки окружения ──────────────────────────────────────────────────────
say "Проверяю окружение"

[ "$(uname -s)" = "Linux" ] || die "Скрипт рассчитан на Linux-сервер."
[ "$(id -u)" = "0" ] || die "Нужны права root: перезапусти через  sudo bash install.sh"

command -v git >/dev/null 2>&1 || {
    say "Ставлю git"
    if command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -y -qq git
    elif command -v dnf >/dev/null 2>&1; then dnf install -y -q git
    elif command -v yum >/dev/null 2>&1; then yum install -y -q git
    else die "Не нашёл пакетный менеджер — поставь git вручную и перезапусти."
    fi
}
ok "git есть"

if [ -z "$DRY_RUN" ] && ! command -v docker >/dev/null 2>&1; then
    warn "Docker не найден."
    ANSWER=""
    if tty_ok; then
        printf '\033[1mПоставить Docker автоматически (get.docker.com)? [y/N]\033[0m ' > /dev/tty
        IFS= read -r ANSWER < /dev/tty || true
    fi
    case "$ANSWER" in
        y|Y|yes|да) curl -fsSL https://get.docker.com | sh ;;
        *) die "Поставь Docker сам (https://docs.docker.com/engine/install/) и перезапусти скрипт." ;;
    esac
fi
if [ -z "$DRY_RUN" ]; then
    docker info >/dev/null 2>&1 || die "Docker установлен, но демон не отвечает: systemctl start docker"
    compose version >/dev/null 2>&1 || die "Не найден Docker Compose (плагин compose или docker-compose)."
    ok "Docker и Compose работают"
fi

# ── код ─────────────────────────────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    say "Код уже есть — обновляю ($INSTALL_DIR)"
    git -C "$INSTALL_DIR" pull --ff-only
else
    say "Клонирую репозиторий в $INSTALL_DIR"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"
ok "Код на месте"

# ── конфигурация ────────────────────────────────────────────────────────────
if [ -f .env ]; then
    ok "Найден существующий .env — оставляю без изменений"
else
    say "Настройка. Понадобятся:"
    echo "   1) токен Telegram-бота          — @BotFather → /newbot"
    echo "   2) api_id и api_hash            — https://my.telegram.org → API development tools"
    echo "   3) токен бота MAX               — https://dev.max.ru"
    echo "   ID каналов можно пропустить (Enter) — после запуска их подскажет лог."
    echo
    ask "Токен Telegram-бота:"            TG_BOT_TOKEN     required
    ask "api_id (my.telegram.org):"       TELEGRAM_API_ID  required
    ask "api_hash (my.telegram.org):"     TELEGRAM_API_HASH required
    ask "Токен бота MAX:"                 MAX_TOKEN        required
    ask "ID канала Telegram (Enter — позже):" TG_SOURCE_CHAT_ID
    ask "ID канала MAX (Enter — позже):"      MAX_CHAT_ID

    cat > .env <<ENV
TG_BOT_TOKEN=${TG_BOT_TOKEN}
TG_SOURCE_CHAT_ID=${TG_SOURCE_CHAT_ID:-0}
TELEGRAM_API_ID=${TELEGRAM_API_ID}
TELEGRAM_API_HASH=${TELEGRAM_API_HASH}

MAX_TOKEN=${MAX_TOKEN}
MAX_CHAT_ID=${MAX_CHAT_ID:-0}
MAX_API_BASE=https://platform-api2.max.ru
MAX_CHAT_ID_IN_QUERY=true

ROUTES=
ALBUM_DEBOUNCE_MS=1500
ENV
    chmod 600 .env
    ok "Записал .env (права 600)"
fi

# ── запуск ──────────────────────────────────────────────────────────────────
if [ -n "$DRY_RUN" ]; then
    ok "DRY_RUN: пропускаю сборку и запуск"
    exit 0
fi

say "Собираю и запускаю контейнеры (первая сборка — пара минут)"
compose up -d --build
ok "Контейнеры запущены"

echo
say "Готово! Что дальше:"
cat <<'NEXT'
  1. Добавь Telegram-бота АДМИНОМ в канал-источник, бота MAX — АДМИНОМ в канал-приёмник.
  2. Если ID каналов ещё не заполнены — опубликуй в Telegram-канале любой пост и смотри лог:
        docker compose logs -f worker
     Строка «источник вне маршрутов: chat_id=-100…» — это ID твоего канала.
     ID канала MAX виден в адресе web.max.ru/-77…
  3. Впиши ID в .env (TG_SOURCE_CHAT_ID и MAX_CHAT_ID) и перезапусти:
        docker compose up -d
  4. Опубликуй пост в Telegram — через пару секунд он появится в MAX.

  Полезное:
        docker compose logs -f worker     # живой лог
        docker compose ps                 # статус
        bash install.sh                   # обновление до свежей версии
NEXT
