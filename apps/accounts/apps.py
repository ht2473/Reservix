"""Конфигурация приложения учётных записей."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AccountsConfig(AppConfig):
    """Пользователи, роли, области видимости, история входов."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    label = "accounts"
    verbose_name = _("Учётные записи")

    def ready(self) -> None:
        """Подключает обработчики сигналов при инициализации приложения."""
        from apps.accounts import signals  # noqa: F401
