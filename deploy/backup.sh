#!/usr/bin/env bash
# =============================================================================
# Reservix — резервное копирование
# Копируются три вещи, и все три обязательны.
# =============================================================================

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/reservix}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/reservix}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
COMPOSE="docker compose --env-file ${APP_DIR}/.env -f ${APP_DIR}/deploy/docker-compose.yml"
STAMP="$(date +%Y%m%d-%H%M%S)"
VERIFY_RESTORE=false

[[ "${1:-}" == "--verify" ]] && VERIFY_RESTORE=true

log()  { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die()  { printf '[%s] ОШИБКА: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; exit 1; }

trap 'die "Копирование прервано на строке $LINENO"' ERR

# --- Подготовка --------------------------------------------------------------
mkdir -p "$BACKUP_DIR"
cd "$APP_DIR"

# shellcheck disable=SC1091  # файл создаётся установщиком на целевой машине
source "$APP_DIR/.env"
: "${POSTGRES_USER:?не задан в .env}"
: "${POSTGRES_DB:?не задан в .env}"

log "Начало резервного копирования"

# --- 1. База данных ----------------------------------------------------------
DB_FILE="$BACKUP_DIR/db-$STAMP.dump"

log "Дамп базы данных"
# Формат custom, а не обычный SQL: он сжат, допускает параллельное
# восстановление и выборочную выгрузку отдельной таблицы.
$COMPOSE exec -T postgres pg_dump \
    --username="$POSTGRES_USER" \
    --dbname="$POSTGRES_DB" \
    --format=custom \
    --compress=9 \
    --no-owner \
    --no-privileges \
    > "$DB_FILE"

DB_SIZE="$(du -h "$DB_FILE" | cut -f1)"
log "База выгружена: $DB_FILE ($DB_SIZE)"

# --- 2. Проверка дампа -------------------------------------------------------
# Читается оглавление архива: повреждённый файл на этом шаге не открывается.
# Проверка стоит доли секунды и отделяет «файл создан» от «файл пригоден».
log "Проверка целостности дампа"
$COMPOSE exec -T postgres pg_restore --list /dev/stdin < "$DB_FILE" > /dev/null \
    || die "Дамп повреждён: $DB_FILE"
log "Дамп читается"

# --- 3. Медиафайлы -----------------------------------------------------------
MEDIA_FILE="$BACKUP_DIR/media-$STAMP.tar.gz"

log "Архивирование медиафайлов"
# Архив снимается из тома через временный контейнер: том принадлежит Docker, и
# обращаться к его каталогу на хосте напрямую нельзя — путь зависит от драйвера
# хранилища и меняется между версиями.
docker run --rm \
    --volume reservix_media_files:/media:ro \
    --volume "$BACKUP_DIR":/backup \
    alpine:3 tar czf "/backup/media-$STAMP.tar.gz" -C /media . \
    || die "Не удалось заархивировать медиафайлы"

MEDIA_SIZE="$(du -h "$MEDIA_FILE" | cut -f1)"
log "Медиафайлы заархивированы: $MEDIA_FILE ($MEDIA_SIZE)"

# --- 4. Конфигурация ---------------------------------------------------------
ENV_FILE="$BACKUP_DIR/env-$STAMP.enc"

log "Копирование конфигурации"
# Файл .env содержит секреты и попадает в архив зашифрованным.
if [[ -n "${BACKUP_PASSPHRASE:-}" ]]; then
    openssl enc -aes-256-cbc -pbkdf2 -salt \
        -in "$APP_DIR/.env" -out "$ENV_FILE" \
        -pass env:BACKUP_PASSPHRASE
    log "Конфигурация зашифрована: $ENV_FILE"
else
    cp "$APP_DIR/.env" "$BACKUP_DIR/env-$STAMP.plain"
    chmod 600 "$BACKUP_DIR/env-$STAMP.plain"
    log "ВНИМАНИЕ: BACKUP_PASSPHRASE не задана, конфигурация сохранена открытым текстом"
fi

# --- 5. Контрольные суммы ----------------------------------------------------
CHECKSUM_FILE="$BACKUP_DIR/checksums-$STAMP.sha256"
( cd "$BACKUP_DIR" && sha256sum "db-$STAMP.dump" "media-$STAMP.tar.gz" > "checksums-$STAMP.sha256" )
log "Контрольные суммы записаны: $CHECKSUM_FILE"

# --- 6. Полная проверка восстановлением --------------------------------------
if [[ "$VERIFY_RESTORE" == true ]]; then
    log "Проверка восстановлением во временную базу"
    VERIFY_DB="reservix_verify_$STAMP"

    $COMPOSE exec -T postgres createdb --username="$POSTGRES_USER" "$VERIFY_DB"
    if $COMPOSE exec -T postgres pg_restore \
            --username="$POSTGRES_USER" --dbname="$VERIFY_DB" \
            --no-owner --no-privileges /dev/stdin < "$DB_FILE" >/dev/null 2>&1; then
        rows="$($COMPOSE exec -T postgres psql --username="$POSTGRES_USER" --dbname="$VERIFY_DB" \
                --tuples-only --command 'SELECT count(*) FROM people_employee;' | tr -d '[:space:]')"
        log "Восстановление успешно: сотрудников в копии — $rows"
    else
        $COMPOSE exec -T postgres dropdb --username="$POSTGRES_USER" "$VERIFY_DB" || true
        die "Копия не восстанавливается"
    fi
    $COMPOSE exec -T postgres dropdb --username="$POSTGRES_USER" "$VERIFY_DB"
fi

# --- 7. Удаление устаревших копий -------------------------------------------
log "Удаление копий старше $RETENTION_DAYS дней"
removed=0
while IFS= read -r -d '' old; do
    rm -f "$old"
    removed=$((removed + 1))
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'db-*.dump' -o -name 'media-*.tar.gz' -o -name 'env-*' -o -name 'checksums-*' \) \
    -mtime "+$RETENTION_DAYS" -print0)
log "Удалено файлов: $removed"

# --- Итог --------------------------------------------------------------------
TOTAL="$(du -sh "$BACKUP_DIR" | cut -f1)"
log "Копирование завершено. Всего в каталоге копий: $TOTAL"

# Отдельная строка для мониторинга: её ищет проверка «когда последний раз
# проходило копирование».
log "BACKUP_OK stamp=$STAMP db=$DB_SIZE media=$MEDIA_SIZE"
