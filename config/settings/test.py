"""Конфигурация тестового окружения."""

from __future__ import annotations

import os

# Значения задаются до импорта базовой конфигурации: она требует SECRET_KEY.
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-not-used-anywhere-else")
os.environ.setdefault("DJANGO_DEBUG", "False")

from .base import *

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

# ---------------------------------------------------------------------------
# Ускорение
# ---------------------------------------------------------------------------
# MD5 в тестах допустим: хеширование пароля перестаёт быть узким местом при
# создании сотен объектов фабриками. В продакшене используется Argon2id.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Локальный кэш в памяти процесса: тестам не нужен работающий Redis.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "reservix-test",
    }
}
SESSION_ENGINE = "django.contrib.sessions.backends.db"

# ---------------------------------------------------------------------------
# Внешние эффекты
# ---------------------------------------------------------------------------
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

# Блокировки по числу попыток ломают параметризованные тесты форм входа,
# поэтому отключены; отдельный набор тестов включает их точечно.
AXES_ENABLED = False
OTP_REQUIRED_ROLES = []

# ---------------------------------------------------------------------------
# Файлы
# ---------------------------------------------------------------------------
# Медиа пишутся во временный каталог, который очищается фикстурой сессии.
MEDIA_ROOT = BASE_DIR / ".pytest-media"
STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

# WhiteNoise исключается из цепочки промежуточных слоёв: тесты не запрашивают статику, а слой
# предупреждает об отсутствии каталога staticfiles на каждый запрос, засоряя вывод.
MIDDLEWARE = [layer for layer in MIDDLEWARE if "whitenoise" not in layer]

# ---------------------------------------------------------------------------
# Прочее
# ---------------------------------------------------------------------------
# Миграции выполняются полностью: они сами являются объектом тестирования
# (проверяется согласованность моделей и схемы БД).
LANGUAGE_CODE = "ru"

LOGGING["root"]["level"] = "ERROR"
for _logger in LOGGING["loggers"].values():
    _logger["level"] = "ERROR"
    _logger["handlers"] = ["console"]
