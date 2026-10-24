# =============================================================================
# Reservix — образ приложения
# Многоэтапная сборка: этап `builder` создаёт виртуальное окружение, финальный
# образ получает только его и исходный код.
# =============================================================================

# --- Этап сборки -------------------------------------------------------------
FROM python:3.12.14-slim-bookworm AS builder

# uv копируется из официального образа: версия закреплена тегом, а установка
# через pip добавила бы лишний слой и замедлила сборку.
COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Зависимости устанавливаются отдельным слоем до копирования исходников:
# правка кода не приводит к переустановке пакетов при пересборке.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-install-project --no-dev

COPY . /app

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# Каталоги перевода .mo в репозиторий не входят и собираются здесь.
RUN /app/.venv/bin/python scripts/i18n.py compile


# --- Финальный образ ---------------------------------------------------------
FROM python:3.12.14-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.prod

# libpq5 нужен psycopg для связи с PostgreSQL; curl — для проверки состояния
# контейнера; fonts-dejavu-core — для выгрузки отчётов в PDF.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 curl tzdata fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Приложение работает от непривилегированного пользователя: процесс,
# скомпрометированный через уязвимость в зависимости, не получит прав root.
RUN groupadd --system --gid 1001 reservix \
    && useradd --system --uid 1001 --gid reservix --create-home reservix

WORKDIR /app

COPY --from=builder --chown=reservix:reservix /app /app

# Каталоги, в которые приложение пишет во время работы.
RUN mkdir -p /app/media /app/logs /app/staticfiles \
    && chown -R reservix:reservix /app/media /app/logs /app/staticfiles

USER reservix

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/health/ || exit 1

# Число воркеров задаётся переменной окружения: значение зависит от числа
# ядер конкретного сервера и не должно быть зашито в образ.
CMD ["sh", "-c", "gunicorn config.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers ${GUNICORN_WORKERS:-3} \
    --threads ${GUNICORN_THREADS:-2} \
    --timeout ${GUNICORN_TIMEOUT:-60} \
    --graceful-timeout 30 \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --access-logfile - \
    --error-logfile - \
    --capture-output"]
