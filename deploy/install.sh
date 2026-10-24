#!/usr/bin/env bash
# =============================================================================
# Reservix — первичная установка на чистый сервер
# Скрипт доводит свежий Ubuntu 24.04 (или Debian 12) до работающей системы.
# =============================================================================

set -Eeuo pipefail

# --- Параметры ---------------------------------------------------------------
DOMAIN="${1:-}"
LETSENCRYPT_EMAIL="${2:-}"
APP_DIR="${APP_DIR:-/opt/reservix}"
APP_USER="${APP_USER:-reservix}"
REPO_URL="${REPO_URL:-}"

readonly DOMAIN LETSENCRYPT_EMAIL APP_DIR APP_USER REPO_URL

# --- Вывод -------------------------------------------------------------------
log()  { printf '\033[1;34m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

trap 'die "Установка прервана на строке $LINENO"' ERR

# --- Проверки ----------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Запустите с правами root: sudo $0 <домен> <почта>"
[[ -n "$DOMAIN" ]] || die "Укажите домен: $0 reservix.ru admin@reservix.ru"
[[ -n "$LETSENCRYPT_EMAIL" ]] || die "Укажите почту для Let's Encrypt"

log "Установка Reservix на домен $DOMAIN"

# --- 1. Системные пакеты -----------------------------------------------------
log "Обновление списка пакетов"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

log "Установка базовых пакетов"
apt-get install -y -qq \
    ca-certificates curl git ufw jq \
    fonts-dejavu-core \
    postgresql-client
# fonts-dejavu-core — не косметика: reportlab не поставляет начертаний с
# кириллицей, и без системного шрифта выгрузка отчётов в PDF отключается.
ok "Пакеты установлены"

# --- 2. Docker ---------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
    ok "Docker уже установлен: $(docker --version)"
else
    log "Установка Docker Engine"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    ok "Docker установлен"
fi

# --- 3. Пользователь и каталоги ---------------------------------------------
if id "$APP_USER" >/dev/null 2>&1; then
    ok "Пользователь $APP_USER уже существует"
else
    log "Создание системного пользователя $APP_USER"
    useradd --system --create-home --home-dir "/home/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"
fi
usermod -aG docker "$APP_USER"

log "Подготовка каталогов"
mkdir -p "$APP_DIR" /var/backups/reservix /var/log/reservix
chown -R "$APP_USER:$APP_USER" "$APP_DIR" /var/backups/reservix /var/log/reservix
chmod 750 /var/backups/reservix
ok "Каталоги готовы"

# --- 4. Исходный код ---------------------------------------------------------
if [[ -d "$APP_DIR/.git" ]]; then
    log "Обновление исходного кода"
    git -C "$APP_DIR" pull --ff-only
elif [[ -n "$REPO_URL" ]]; then
    log "Клонирование $REPO_URL"
    git clone --depth 1 "$REPO_URL" "$APP_DIR"
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
else
    [[ -f "$APP_DIR/deploy/docker-compose.yml" ]] \
        || die "Каталог $APP_DIR пуст. Скопируйте код или задайте REPO_URL"
    ok "Исходный код на месте"
fi

# --- 5. Конфигурация ---------------------------------------------------------
if [[ -f "$APP_DIR/.env" ]]; then
    ok "Файл .env существует — оставлен без изменений"
else
    log "Создание .env из образца"
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"

    # Секреты генерируются на сервере и никогда не покидают его.
    secret_key="$(openssl rand -base64 48 | tr -d '\n=+/' | cut -c1-50)"
    db_password="$(openssl rand -base64 32 | tr -d '\n=+/' | cut -c1-32)"

    sed -i \
        -e "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=${secret_key}|" \
        -e "s|^DJANGO_DEBUG=.*|DJANGO_DEBUG=False|" \
        -e "s|^DJANGO_SETTINGS_MODULE=.*|DJANGO_SETTINGS_MODULE=config.settings.prod|" \
        -e "s|^DJANGO_ALLOWED_HOSTS=.*|DJANGO_ALLOWED_HOSTS=${DOMAIN},www.${DOMAIN}|" \
        -e "s|^CSRF_TRUSTED_ORIGINS=.*|CSRF_TRUSTED_ORIGINS=https://${DOMAIN},https://www.${DOMAIN}|" \
        -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${db_password}|" \
        "$APP_DIR/.env"

    chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    ok "Файл .env создан, секреты сгенерированы"
    warn "Проверьте настройки почты в $APP_DIR/.env — без них не уходят уведомления"
fi

# --- 6. Межсетевой экран -----------------------------------------------------
log "Настройка ufw"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
# Порты базы и Redis наружу не открываются: они доступны только внутри сети
# compose. Это второй рубеж — первый обеспечен самой сетью контейнеров.
ufw --force enable >/dev/null
ok "Открыты порты 22, 80, 443"

# --- 7. Сертификат -----------------------------------------------------------
CERT_PATH="/var/lib/docker/volumes/reservix_certbot_certs/_data/live/$DOMAIN"
if [[ -d "$CERT_PATH" ]]; then
    ok "Сертификат для $DOMAIN уже выпущен"
else
    log "Выпуск сертификата Let's Encrypt"
    # Порядок важен: nginx должен отвечать на проверку по HTTP до выпуска
    # сертификата, иначе certbot не подтвердит владение доменом.
    cd "$APP_DIR/deploy"
    docker compose --env-file ../.env up -d nginx || true
    sleep 5

    docker compose --env-file ../.env run --rm --entrypoint "\
        certbot certonly --webroot -w /var/www/certbot \
        --email $LETSENCRYPT_EMAIL --agree-tos --no-eff-email \
        -d $DOMAIN -d www.$DOMAIN" certbot \
        || warn "Сертификат не выпущен: проверьте, что домен указывает на этот сервер"
fi

# --- 8. Запуск ---------------------------------------------------------------
log "Сборка образа и запуск служб"
cd "$APP_DIR/deploy"
docker compose --env-file ../.env up -d --build
ok "Службы запущены"

log "Ожидание готовности приложения"
for attempt in {1..30}; do
    if curl --fail --silent --output /dev/null http://localhost/health/; then
        ok "Приложение отвечает на проверку состояния"
        break
    fi
    [[ $attempt -eq 30 ]] && die "Приложение не поднялось за 150 секунд. Смотрите: docker compose logs web"
    sleep 5
done

# --- 9. Администратор --------------------------------------------------------
log "Создание администратора системы"
if docker compose --env-file ../.env exec -T web \
    python manage.py shell -c "
from django.contrib.auth import get_user_model
print('EXISTS' if get_user_model().objects.filter(is_superuser=True).exists() else 'NONE')
" 2>/dev/null | grep -q EXISTS; then
    ok "Администратор уже создан"
else
    warn "Создайте администратора вручную:"
    warn "  cd $APP_DIR/deploy && docker compose exec web python manage.py createsuperuser"
fi

# --- 10. Резервное копирование ----------------------------------------------
log "Настройка резервного копирования"
install -m 0755 "$APP_DIR/deploy/backup.sh" /usr/local/bin/reservix-backup

# Ежедневно в 03:30 — время выбрано вне рабочих часов и вне окна обновления
# пакетов Ubuntu (06:00), чтобы копирование не совпало с перезагрузкой.
cat > /etc/cron.d/reservix-backup <<CRON
# Резервное копирование Reservix: база и медиафайлы
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
30 3 * * * root /usr/local/bin/reservix-backup >> /var/log/reservix/backup.log 2>&1
CRON
ok "Копирование настроено: ежедневно в 03:30"

# --- 11. Ротация журналов ----------------------------------------------------
install -m 0644 "$APP_DIR/deploy/logrotate/reservix" /etc/logrotate.d/reservix
ok "Ротация журналов настроена"

# --- Итог --------------------------------------------------------------------
printf '\n'
ok "Установка завершена"
printf '\n'
printf '  Адрес системы:      https://%s\n' "$DOMAIN"
printf '  Каталог установки:  %s\n' "$APP_DIR"
printf '  Резервные копии:    /var/backups/reservix\n'
printf '  Журналы:            /var/log/reservix\n'
printf '\n'
printf '  Состояние служб:    cd %s/deploy && docker compose ps\n' "$APP_DIR"
printf '  Журнал приложения:  cd %s/deploy && docker compose logs -f web\n' "$APP_DIR"
printf '  Обновление:         %s/deploy/update.sh\n' "$APP_DIR"
printf '\n'
warn "Заполните демонстрационными данными, если это стенд:"
warn "  docker compose exec web python manage.py seed_demo"
