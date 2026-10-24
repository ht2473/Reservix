"""Конфигурация окружения разработки."""

from __future__ import annotations

from .base import *

DEBUG = True

# Адрес 0.0.0.0 нужен для доступа к серверу разработки из контейнера и с другого устройства
# локальной сети при проверке адаптивной вёрстки.
ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "0.0.0.0",  # noqa: S104  # nosec B104
    "[::1]",
    ".localhost",
]

# --- Инструменты разработки ---------------------------------------------------
INSTALLED_APPS += ["django_extensions"]

# --- Почта ----------------------------------------------------------------- Письма печатаются в
# консоль: не требует SMTP-сервера на машине разработчика.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# --- Статика ---------------------------------------------------------------
# Манифест-хранилище требует collectstatic, что мешает горячей перезагрузке.
STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

# --- Безопасность ----------------------------------------------------------
# Блокировки axes мешают отладке форм входа, поэтому по умолчанию выключены.
AXES_ENABLED = env.bool("AXES_ENABLED", default=False)
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Второй фактор в разработке не запрашивается — упрощает вход под любой ролью.
OTP_REQUIRED_ROLES = []

# --- Фоновые задачи -------------------------------------------------------- Задачи выполняются
# синхронно в процессе веб-сервера: не нужен отдельный воркер.
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=True)
CELERY_TASK_EAGER_PROPAGATES = True

# --- Логирование -----------------------------------------------------------
LOGGING["loggers"]["reservix"]["level"] = "DEBUG"

# Раскомментируйте, чтобы видеть все SQL-запросы в консоли:
# LOGGING["loggers"]["django.db.backends"]["level"] = "DEBUG"

# --- Шаблоны ---------------------------------------------------------------
# Отключаем кэширование шаблонов, чтобы правки применялись без перезапуска.
TEMPLATES[0]["OPTIONS"]["debug"] = True
