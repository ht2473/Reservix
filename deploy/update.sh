#!/usr/bin/env bash
# =============================================================================
# Reservix — обновление работающей установки
# Порядок шагов продиктован одним требованием: **в любой момент обновления
# должен существовать способ вернуться назад**.
# =============================================================================

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/reservix}"
COMPOSE="docker compose --env-file ${APP_DIR}/.env -f ${APP_DIR}/deploy/docker-compose.yml"
TARGET="${1:-}"

log()  { printf '\033[1;34m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

trap 'die "Обновление прервано на строке $LINENO"' ERR

cd "$APP_DIR"

# --- Откат -------------------------------------------------------------------
if [[ "$TARGET" == "--rollback" ]]; then
    log "Возврат на предыдущий образ"
    docker image inspect reservix:previous >/dev/null 2>&1 \
        || die "Предыдущий образ не сохранён — откат невозможен"

    docker tag reservix:previous reservix:latest
    $COMPOSE up -d --no-build web worker beat
    ok "Возврат выполнен. Проверьте состояние: $COMPOSE ps"
    warn "Миграции базы не откатываются автоматически: если новая версия"
    warn "изменяла схему, восстановите базу из копии перед обновлением."
    exit 0
fi

# --- 1. Резервная копия ------------------------------------------------------
# Первым шагом и без вопросов: обновление, после которого не к чему вернуться,
# обновлением не является.
log "Резервное копирование перед обновлением"
"$APP_DIR/deploy/backup.sh"
ok "Копия снята"

# --- 2. Получение кода -------------------------------------------------------
CURRENT="$(git -C "$APP_DIR" rev-parse --short HEAD)"
log "Текущая версия: $CURRENT"

log "Получение изменений"
git -C "$APP_DIR" fetch --tags --prune
if [[ -n "$TARGET" ]]; then
    git -C "$APP_DIR" checkout "$TARGET"
else
    git -C "$APP_DIR" pull --ff-only
fi

NEW="$(git -C "$APP_DIR" rev-parse --short HEAD)"
if [[ "$CURRENT" == "$NEW" ]]; then
    ok "Версия не изменилась — обновление не требуется"
    exit 0
fi
log "Новая версия: $NEW"

# --- 3. Сохранение текущего образа -------------------------------------------
# Метка ставится до сборки: собранный образ перезапишет `latest`, и без
# сохранённой метки возврат станет невозможен.
if docker image inspect reservix:latest >/dev/null 2>&1; then
    docker tag reservix:latest reservix:previous
    ok "Текущий образ сохранён как reservix:previous"
fi

# --- 4. Сборка ---------------------------------------------------------------
# Образ собирается до остановки работающих контейнеров: ошибка сборки не
# должна оставлять систему выключенной.
log "Сборка образа"
$COMPOSE build web
ok "Образ собран"

# --- 5. Проверка миграций ----------------------------------------------------
log "Проверка миграций"
PENDING="$($COMPOSE run --rm --no-deps web python manage.py showmigrations --plan \
    | grep -c '^\[ \]' || true)"
if [[ "$PENDING" -gt 0 ]]; then
    log "Будет применено миграций: $PENDING"
else
    ok "Схема базы не меняется"
fi

# --- 6. Обновление служб -----------------------------------------------------
# Миграции и сбор статики выполняются командой запуска контейнера, поэтому
# отдельного шага здесь нет: последовательность описана в docker-compose.yml и
# одинакова при первой установке и при обновлении.
log "Перезапуск служб"
$COMPOSE up -d --no-build web worker beat
ok "Службы перезапущены"

# --- 7. Проверка -------------------------------------------------------------
log "Проверка состояния"
for attempt in {1..24}; do
    if curl --fail --silent --output /dev/null http://localhost/health/; then
        ok "Приложение отвечает"
        break
    fi
    if [[ $attempt -eq 24 ]]; then
        warn "Приложение не поднялось за две минуты — выполняется откат"
        docker tag reservix:previous reservix:latest
        $COMPOSE up -d --no-build web worker beat
        die "Обновление отменено, возвращена версия $CURRENT. Журнал: $COMPOSE logs web"
    fi
    sleep 5
done

# --- 8. Очистка --------------------------------------------------------------
log "Удаление неиспользуемых образов"
docker image prune --force --filter "until=168h" >/dev/null
ok "Обновление завершено: $CURRENT → $NEW"

printf '\n'
printf '  Откат при необходимости: %s/deploy/update.sh --rollback\n' "$APP_DIR"
