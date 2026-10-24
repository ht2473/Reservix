#!/usr/bin/env bash
# =============================================================================
# Reservix — продление сертификата TLS
# В обычной работе продлением занимается контейнер `certbot`: он проверяет срок
# каждые двенадцать часов и продлевает сертификат за месяц до истечения.
# =============================================================================

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/reservix}"
COMPOSE="docker compose --env-file ${APP_DIR}/.env -f ${APP_DIR}/deploy/docker-compose.yml"
MODE="${1:-}"

# : За сколько дней до истечения продление считается своевременным.
RENEWAL_WINDOW_DAYS=30

log()  { printf '\033[1;34m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

trap 'die "Продление прервано на строке $LINENO"' ERR

cd "$APP_DIR"

# --- Сроки действующих сертификатов ------------------------------------------
if [[ "$MODE" == "--status" ]]; then
    log "Сроки действия сертификатов"
    $COMPOSE run --rm --entrypoint "certbot certificates" certbot
    exit 0
fi

# --- Проверка без обращения к удостоверяющему центру -------------------------
if [[ "$MODE" == "--dry-run" ]]; then
    log "Проверка процедуры продления без выпуска"
    # Обращения к боевому серверу Let's Encrypt ограничены по частоте: пять
    # неудачных попыток в час блокируют домен на неделю.
    $COMPOSE run --rm --entrypoint "\
        certbot renew --webroot -w /var/www/certbot --dry-run" certbot
    ok "Процедура продления работает"
    exit 0
fi

# --- Продление ---------------------------------------------------------------
FORCE_FLAG=""
if [[ "$MODE" == "--force" ]]; then
    warn "Принудительное продление: расходуется лимит обращений к Let's Encrypt"
    FORCE_FLAG="--force-renewal"
fi

log "Продление сертификатов"
if $COMPOSE run --rm --entrypoint "\
        certbot renew --webroot -w /var/www/certbot $FORCE_FLAG" certbot; then
    ok "Команда продления выполнена"
else
    die "Продление не удалось. Проверьте, что домен указывает на этот сервер и порт 80 открыт"
fi

# --- Перечитывание конфигурации ----------------------------------------------
# Продлённый сертификат подхватывается только после перечитывания конфигурации:
# nginx держит открытым тот файл, который прочитал при старте.
log "Перечитывание конфигурации nginx"
$COMPOSE exec nginx nginx -t || die "Конфигурация nginx содержит ошибку — перезагрузка отменена"
$COMPOSE exec nginx nginx -s reload
ok "Конфигурация перечитана"

# --- Проверка результата -----------------------------------------------------
DOMAIN="$(grep -oP '^DJANGO_ALLOWED_HOSTS=\K[^,]+' "$APP_DIR/.env" || true)"
if [[ -n "$DOMAIN" ]]; then
    log "Проверка сертификата на $DOMAIN"
    expiry="$(echo | openssl s_client -servername "$DOMAIN" -connect "$DOMAIN:443" 2>/dev/null \
        | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2 || true)"

    if [[ -n "$expiry" ]]; then
        remaining=$(( ( $(date -d "$expiry" +%s) - $(date +%s) ) / 86400 ))
        ok "Сертификат действует до $expiry (осталось дней: $remaining)"
        if (( remaining < RENEWAL_WINDOW_DAYS )); then
            warn "Срок меньше окна продления — проверьте журнал certbot"
        fi
    else
        warn "Не удалось прочитать сертификат по сети: проверьте вручную"
    fi
fi
