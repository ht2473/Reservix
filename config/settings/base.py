"""Базовая конфигурация Reservix."""

from __future__ import annotations

from pathlib import Path

import environ

from django.utils.translation import gettext_lazy as _

from core.settings_types import ReservixSettings

# ---------------------------------------------------------------------------
# Пути проекта
# ---------------------------------------------------------------------------
# BASE_DIR указывает на корень репозитория: config/settings/base.py -> ../../..
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Идентификация приложения
# ---------------------------------------------------------------------------
# Значения используются в шаблонах (шапка, подвал), метаданных страниц,
# генерируемых документах и заголовках писем.
SITE_NAME = "Reservix"
SITE_TAGLINE = _("Система формирования и развития кадрового резерва")
SITE_DOMAIN = env.str("SITE_DOMAIN", default="reservix.ru")

# Организация, для бизнес-процессов которой внедряется система.
ORGANIZATION_NAME = _("ООО «Меридиан Софт»")
ORGANIZATION_SHORT = _("Меридиан Софт")

# ---------------------------------------------------------------------------
# Безопасность
# ---------------------------------------------------------------------------
SECRET_KEY = env.str("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# ---------------------------------------------------------------------------
# Приложения
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django.contrib.postgres",
]

THIRD_PARTY_APPS = [
    "modeltranslation",
    "django_celery_beat",
    "django_htmx",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
    "axes",
]

# Порядок отражает зависимости: справочники объявляются раньше потребителей.
LOCAL_APPS = [
    "core",
    "apps.accounts",  # пользователи, роли, области видимости
    "apps.audit",  # журнал аудита действий
    "apps.org",  # филиалы, подразделения, должности, профили должностей
    "apps.people",  # сотрудники, карьерная история
    "apps.competencies",  # компетенции, шкалы, модели компетенций
    "apps.assessment",  # кампании оценки, 360°, тестирование
    "apps.reserve",  # кадровый резерв, статусы, заявки
    "apps.succession",  # критичные должности, планы преемственности
    "apps.development",  # ИПР, мероприятия, наставничество
    "apps.analytics",  # индекс готовности, MCDA, 9-box, метрики
    "apps.documents",  # файловое хранилище
    "apps.reporting",  # генерация .docx / .xlsx / .pdf
    "apps.notifications",  # уведомления
    "apps.portal",  # публичная часть сайта
    "apps.dashboard",  # дашборды по ролям
    "apps.administration",  # собственная панель администратора
    "apps.api",  # REST API на django-ninja
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ---------------------------------------------------------------------------
# Промежуточные слои
# ---------------------------------------------------------------------------
# Порядок важен: безопасность и статика — первыми, аудит — последним.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "core.middleware.TimezoneMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
    "axes.middleware.AxesMiddleware",
    "apps.audit.middleware.AuditContextMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Шаблоны
# ---------------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Собственные процессоры контекста
                "core.context_processors.site_identity",  # название, автор, организация
                "core.context_processors.navigation",  # главное меню и активный пункт
                "core.context_processors.user_context",  # роль, кабинет, счётчики
                "core.context_processors.interface",  # тема, плотность, подписи сценариев
            ],
            "builtins": [
                "core.templatetags.reservix",  # фильтры и теги доступны без {% load %}
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------
DATABASES = {
    "default": {
        **env.db_url(
            "DATABASE_URL", default="postgres://reservix:reservix@localhost:5432/reservix"
        ),
        # Переиспользование соединений: снижает накладные расходы на установку
        # TCP-сессии при работе через gunicorn с несколькими воркерами.
        "CONN_MAX_AGE": env.int("DB_CONN_MAX_AGE", default=60),
        "CONN_HEALTH_CHECKS": True,
        "ATOMIC_REQUESTS": False,
        "OPTIONS": {
            "connect_timeout": 10,
            "application_name": "reservix",
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Кэш и сессии
# ---------------------------------------------------------------------------
REDIS_URL = env.str("REDIS_URL", default="redis://localhost:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "reservix",
        "TIMEOUT": 300,
    }
}

SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
SESSION_COOKIE_NAME = "reservix_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", default=60 * 60 * 12)
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_SAVE_EVERY_REQUEST = True  # продлевает сессию при активности пользователя

CSRF_COOKIE_NAME = "reservix_csrf"
CSRF_COOKIE_HTTPONLY = False  # значение читает HTMX для заголовка X-CSRFToken
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=["http://localhost:8000"])
CSRF_FAILURE_VIEW = "core.views.csrf_failure"

# ---------------------------------------------------------------------------
# Аутентификация и пароли
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    # AxesStandaloneBackend должен идти первым: он блокирует попытку входа
    # до проверки учётных данных, если исчерпан лимит неудачных попыток.
    "axes.backends.AxesStandaloneBackend",
    "apps.accounts.backends.EmailBackend",
]

# Argon2id — рекомендация OWASP Password Storage Cheat Sheet.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    {"NAME": "apps.accounts.validators.ComplexityValidator"},
]

LOGIN_URL = "accounts:login"
# Адрес после входа.
LOGIN_REDIRECT_URL = "dashboard:employee"
LOGOUT_REDIRECT_URL = "portal:home"
PASSWORD_RESET_TIMEOUT = 60 * 60 * 24  # ссылка восстановления живёт сутки

# django-axes: защита от подбора учётных данных
AXES_ENABLED = env.bool("AXES_ENABLED", default=True)
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1  # час блокировки после исчерпания попыток
AXES_LOCKOUT_PARAMETERS = ["ip_address", "username"]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = "account/lockout.html"
AXES_VERBOSE = True

# django-otp: двухфакторная аутентификация для привилегированных ролей
OTP_TOTP_ISSUER = SITE_NAME
OTP_TOTP_THROTTLE_FACTOR = 1
# Роли, для которых второй фактор обязателен.
OTP_REQUIRED_ROLES = ["admin", "hr"]

# ---------------------------------------------------------------------------
# Интернационализация
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "ru"
LANGUAGES = [
    ("ru", _("Русский")),
    ("en", _("English")),
]
LOCALE_PATHS = [BASE_DIR / "locale"]

# Двуязычные поля справочников и содержимого: русский — основной язык.
# При отсутствии значения на текущем языке показывается значение на другом.
MODELTRANSLATION_DEFAULT_LANGUAGE = "ru"
MODELTRANSLATION_LANGUAGES = ("ru", "en")
MODELTRANSLATION_FALLBACK_LANGUAGES = ("ru", "en")

# Имя собственное, а не django_language: все cookie системы носят общую приставку, по которой их
# видно в инструментах разработчика и в настройках обратного прокси-сервера.
LANGUAGE_COOKIE_NAME = "reservix_language"
LANGUAGE_COOKIE_SAMESITE = "Lax"
# Год: язык интерфейса — долговременный выбор, и переспрашивать о нём при
# каждом возвращении в систему незачем.
LANGUAGE_COOKIE_AGE = 60 * 60 * 24 * 365

# ---------------------------------------------------------------------------
# Оформление интерфейса
# ---------------------------------------------------------------------------
# Тема и плотность таблиц передаются в cookie, а не только в локальном хранилище браузера.
APPEARANCE_COOKIE_NAME = "reservix_appearance"
APPEARANCE_COOKIE_AGE = 60 * 60 * 24 * 365

TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_L10N = True
USE_TZ = True

# Форматы, привычные российскому пользователю; переопределяют локаль Django.
DATE_INPUT_FORMATS = ["%d.%m.%Y", "%Y-%m-%d"]
DATETIME_INPUT_FORMATS = ["%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S"]

# ---------------------------------------------------------------------------
# Статические файлы и медиа
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Ограничения загрузки файлов (модуль документов).
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024  # 5 МБ держим в памяти
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000  # формы калибровки содержат много полей
MAX_UPLOAD_SIZE = env.int("MAX_UPLOAD_SIZE", default=25 * 1024 * 1024)
ALLOWED_UPLOAD_EXTENSIONS = [
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".odt",
    ".ods",
    ".txt",
    ".rtf",
    ".csv",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".zip",
]

# ---------------------------------------------------------------------------
# Шрифт для выгрузки в PDF
# ---------------------------------------------------------------------------
# Библиотека reportlab располагает только латинскими встроенными начертаниями: кириллица печаталась
# бы пустыми прямоугольниками.
PDF_FONT_PATHS = [
    env.str("PDF_FONT_PATH", default=""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
]

# ---------------------------------------------------------------------------
# Почта
# ---------------------------------------------------------------------------
EMAIL_BACKEND = env.str("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env.str("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=25)
EMAIL_HOST_USER = env.str("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env.str("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=False)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
EMAIL_TIMEOUT = 15
DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", default=f"Reservix <noreply@{SITE_DOMAIN}>")
SERVER_EMAIL = env.str("SERVER_EMAIL", default=f"reservix-system@{SITE_DOMAIN}")
EMAIL_SUBJECT_PREFIX = "[Reservix] "

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = env.str("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = env.str("CELERY_RESULT_BACKEND", default=REDIS_URL)
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 15 * 60
CELERY_TASK_SOFT_TIME_LIMIT = 10 * 60
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # честное распределение длинных задач
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200  # страховка от утечек памяти в pandas
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_ROUTES = {
    "apps.analytics.tasks.*": {"queue": "analytics"},
    "apps.reporting.tasks.*": {"queue": "reports"},
    "apps.notifications.tasks.*": {"queue": "notifications"},
    # Напоминания о сроках развития направляются в очередь уведомлений, а не в собственную: они и
    # есть уведомления, отличаясь от прочих только поводом, и отдельная очередь означала бы
    # отдельный воркер ради того же действия.
    "apps.development.tasks.*": {"queue": "notifications"},
}

# ---------------------------------------------------------------------------
# Сообщения интерфейса
# ---------------------------------------------------------------------------
MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

# ---------------------------------------------------------------------------
# Настройки предметной области
# ---------------------------------------------------------------------------
RESERVIX: ReservixSettings = {
    # Шкала оценки компетенций: от 1 до 5 баллов включительно.
    "COMPETENCY_SCALE_MIN": 1,
    "COMPETENCY_SCALE_MAX": 5,
    # Пороговые значения индекса готовности резервиста (ИГР), в баллах 0–100.
    "READINESS_THRESHOLD_HIGH": 75,
    "READINESS_THRESHOLD_MEDIUM": 55,
    # Пороги матрицы 9-box по осям результативности и потенциала (0–100).
    "NINE_BOX_LOW_MAX": 40,
    "NINE_BOX_MID_MAX": 70,
    # Предельное отношение согласованности матрицы парных сравнений (метод анализа иерархий Саати).
    "AHP_MAX_CONSISTENCY_RATIO": 0.10,
    # Горизонт планирования преемственности, месяцев.
    "SUCCESSION_HORIZON_MONTHS": 36,
    # Срок, за который система предупреждает о приближении дедлайна мероприятия ИПР.
    "ACTIVITY_DUE_SOON_DAYS": 14,
    # Количество записей на странице по умолчанию.
    "PAGE_SIZE": 25,
    "PAGE_SIZE_CHOICES": [10, 25, 50, 100],
    # Срок хранения записей журнала аудита, дней (требование политики ПДн).
    "AUDIT_RETENTION_DAYS": 1095,
}

# ---------------------------------------------------------------------------
# REST API (django-ninja)
# ---------------------------------------------------------------------------
NINJA_PAGINATION_PER_PAGE = RESERVIX["PAGE_SIZE"]
NINJA_PAGINATION_MAX_LIMIT = 200
NINJA_DEFAULT_THROTTLE_RATES = {
    "anon": "60/m",
    "user": "600/m",
}

API_TITLE = "Reservix API"
API_VERSION = "1.0.1"

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
LOG_LEVEL = env.str("LOG_LEVEL", default="INFO")
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname:<8} {name:<28} {message}",
            "style": "{",
        },
        "json": {
            "()": "core.logging.JsonFormatter",
        },
    },
    "filters": {
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "reservix.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "json",
            "encoding": "utf-8",
        },
        "security_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "security.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 10,
            "formatter": "json",
            "encoding": "utf-8",
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console", "file"], "level": LOG_LEVEL, "propagate": False},
        "django.security": {
            "handlers": ["security_file", "console"],
            "level": "WARNING",
            "propagate": False,
        },
        "django.db.backends": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "axes": {"handlers": ["security_file", "console"], "level": "INFO", "propagate": False},
        "reservix": {"handlers": ["console", "file"], "level": LOG_LEVEL, "propagate": False},
        "reservix.audit": {"handlers": ["security_file"], "level": "INFO", "propagate": False},
    },
}
