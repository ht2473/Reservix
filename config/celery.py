"""Точка входа Celery."""

from __future__ import annotations

import os

from celery import Celery
from celery.signals import setup_logging

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("reservix")

# Все настройки Celery живут в конфигурации Django с префиксом CELERY_.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@setup_logging.connect
def configure_logging(**kwargs) -> None:
    """Переиспользует конфигурацию логирования Django вместо собственной Celery."""
    from logging.config import dictConfig

    from django.conf import settings

    dictConfig(settings.LOGGING)


@app.task(bind=True, ignore_result=True)
def debug_task(self) -> str:
    """Диагностическая задача: подтверждает связность воркера и брокера."""
    return f"Задача выполнена воркером: {self.request.hostname}"
