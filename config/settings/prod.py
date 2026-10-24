"""Конфигурация продакшена."""

from __future__ import annotations

from .base import *

DEBUG = False

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

# ---------------------------------------------------------------------------
# Транспортная безопасность
# ---------------------------------------------------------------------------
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
# Django доверяет заголовку только потому, что его выставляет наш же nginx.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# ---------------------------------------------------------------------------
# Производительность шаблонов
# ---------------------------------------------------------------------------
# APP_DIRS несовместим с явным указанием loaders, поэтому отключаем его и
# собираем цепочку загрузчиков вручную, оборачивая её в кэширующий загрузчик.
TEMPLATES[0]["APP_DIRS"] = False
TEMPLATES[0]["OPTIONS"]["loaders"] = [
    (
        "django.template.loaders.cached.Loader",
        [
            "django.template.loaders.filesystem.Loader",
            "django.template.loaders.app_directories.Loader",
        ],
    )
]

# ---------------------------------------------------------------------------
# Отдача защищённых файлов
# ---------------------------------------------------------------------------
# Django проверяет права доступа и возвращает пустой ответ с заголовком X-Accel-Redirect; сам файл
# клиенту отдаёт nginx.
USE_X_ACCEL_REDIRECT = True
X_ACCEL_REDIRECT_PREFIX = "/protected-media/"

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
# В контейнере пишем структурированный JSON в stdout — его собирает docker logs.
LOGGING["handlers"]["console"]["formatter"] = "json"

# Критические ошибки дублируются администраторам письмом.
ADMINS = [
    (name.strip(), email.strip())
    for name, email in (pair.split("|") for pair in env.list("DJANGO_ADMINS", default=[]))
]
if ADMINS:
    LOGGING["handlers"]["mail_admins"] = {
        "class": "django.utils.log.AdminEmailHandler",
        "level": "ERROR",
        "filters": ["require_debug_false"],
        "include_html": False,
    }
    LOGGING["loggers"]["django"]["handlers"].append("mail_admins")

# ---------------------------------------------------------------------------
# Ограничения окружения
# ---------------------------------------------------------------------------
# Пул соединений в продакшене держим дольше: воркеры gunicorn долгоживущие.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=300)

# Фоновые задачи всегда уходят в очередь — синхронное выполнение недопустимо,
# оно блокирует воркер веб-сервера на время формирования отчёта.
CELERY_TASK_ALWAYS_EAGER = False
