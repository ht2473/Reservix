"""Конфигурация приложения журнала аудита."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AuditConfig(AppConfig):
    """Регистрация изменений данных и действий пользователей."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.audit"
    label = "audit"
    verbose_name = _("Журнал аудита")

    def ready(self) -> None:
        from apps.audit import signals  # noqa: F401
