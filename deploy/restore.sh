#!/usr/bin/env bash
# =============================================================================
# Reservix — восстановление из резервной копии
# Скрипт существует по той же причине, по какой существует backup.sh: копия,
# процедура восстановления которой не описана и не проверена, копией не
# является.
# =============================================================================

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/reservix}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/reservix}"
COMPOSE="docker compose --env-file ${APP_DIR}/.env -f ${APP_DIR}/deploy/docker-compose.yml"

log()  { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
die()  { printf '\033[1;31mОШИБКА:\033[0m %s\n' "$*" >&2; exit 1; }

trap 'die "Восстановление прервано на строке $LINENO"' ERR

# --- Перечень копий ----------------------------------------------------------
if [[ "${1:-}" == "--list" || -z "${1:-}" ]]; then
    printf 'Доступные копии в %s:\n\n' "$BACKUP_DIR"
    find "$BACKUP_DIR" -maxdepth 1 -name 'db-*.dump' -printf '%TY-%Tm-%Td %TH:%TM  %10s  %p\n' \
        | sort -r | head -30
    printf '\nВосстановление: %s <путь к дампу>\n' "$0"
    exit 0
fi

DUMP_FILE="$1"
[[ -f "$DUMP_FILE" ]] || die "Файл не найден: $DUMP_FILE"

STAMP="$(basename "$DUMP_FILE" | sed 's/^db-//; s/\.dump$//')"
MEDIA_FILE="$BACKUP_DIR/media-$STAMP.tar.gz"
CHECKSUM_FILE="$BACKUP_DIR/checksums-$STAMP.sha256"

cd "$APP_DIR"
# shellcheck disable=SC1091  # файл создаётся установщиком на целевой машине
source "$APP_DIR/.env"

# --- 1. Проверка копии до всего остального -----------------------------------
log "Проверка контрольных сумм"
if [[ -f "$CHECKSUM_FILE" ]]; then
    ( cd "$BACKUP_DIR" && sha256sum --check --quiet "$(basename "$CHECKSUM_FILE")" ) \
        || die "Контрольные суммы не совпали: копия повреждена"
    log "Суммы совпадают"
else
    log "ВНИМАНИЕ: файл контрольных сумм отсутствует, проверка пропущена"
fi

log "Проверка читаемости дампа"
$COMPOSE exec -T postgres pg_restore --list /dev/stdin < "$DUMP_FILE" > /dev/null \
    || die "Дамп не читается"

# --- 2. Подтверждение --------------------------------------------------------
printf '\n'
printf '  Будет восстановлено:\n'
printf '    база данных:  %s\n' "$DUMP_FILE"
printf '    медиафайлы:   %s\n' "$([[ -f "$MEDIA_FILE" ]] && echo "$MEDIA_FILE" || echo 'нет в копии')"
printf '    целевая база: %s\n' "$POSTGRES_DB"
printf '\n'
printf '  \033[1;31mТекущие данные будут заменены.\033[0m\n'
printf '\n'
read -r -p '  Введите ВОССТАНОВИТЬ для продолжения: ' confirmation
[[ "$confirmation" == "ВОССТАНОВИТЬ" ]] || die "Отменено пользователем"

# --- 3. Копия текущего состояния ---------------------------------------------
# Снимается всегда, даже когда «восстанавливаем поверх мусора»: ошибка в
# выборе файла обнаруживается уже после того, как прежние данные стёрты.
SAFETY="$BACKUP_DIR/before-restore-$(date +%Y%m%d-%H%M%S).dump"
log "Снятие копии текущего состояния: $SAFETY"
$COMPOSE exec -T postgres pg_dump \
    --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
    --format=custom --compress=9 --no-owner --no-privileges > "$SAFETY"

# --- 4. Остановка приложения -------------------------------------------------
# Останавливаются только потребители базы. Сам postgres продолжает работать:
# восстановление выполняется через него.
log "Остановка приложения и фоновых задач"
$COMPOSE stop web worker beat

# --- 5. Восстановление базы --------------------------------------------------
log "Восстановление базы данных"
$COMPOSE exec -T postgres pg_restore \
    --username="$POSTGRES_USER" \
    --dbname="$POSTGRES_DB" \
    --clean --if-exists \
    --no-owner --no-privileges \
    --exit-on-error \
    /dev/stdin < "$DUMP_FILE" \
    || die "Восстановление не удалось. Прежнее состояние: $SAFETY"
log "База восстановлена"

# --- 6. Восстановление медиафайлов -------------------------------------------
if [[ -f "$MEDIA_FILE" ]]; then
    log "Восстановление медиафайлов"
    docker run --rm \
        --volume reservix_media_files:/media \
        --volume "$BACKUP_DIR":/backup:ro \
        alpine:3 sh -c "rm -rf /media/* && tar xzf /backup/$(basename "$MEDIA_FILE") -C /media"
    log "Медиафайлы восстановлены"
else
    log "ВНИМАНИЕ: медиафайлы в копии отсутствуют — ссылки на документы будут нерабочими"
fi

# --- 7. Запуск и проверка ----------------------------------------------------
log "Запуск приложения"
$COMPOSE up -d web worker beat

log "Ожидание готовности"
for attempt in {1..30}; do
    if curl --fail --silent --output /dev/null http://localhost/health/; then
        log "Приложение отвечает"
        break
    fi
    [[ $attempt -eq 30 ]] && die "Приложение не поднялось. Прежнее состояние: $SAFETY"
    sleep 5
done

# --- 8. Сверка целостности документов ----------------------------------------
# Хранилище считает контрольную сумму при приёме документа и сверяет её при
# открытии карточки.
log "Сверка контрольных сумм документов"
$COMPOSE exec -T web python manage.py verify_documents || \
    log "ВНИМАНИЕ: часть документов не прошла сверку — база и медиафайлы из разных копий?"

log "Восстановление завершено. Копия прежнего состояния: $SAFETY"
