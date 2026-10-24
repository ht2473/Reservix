"""Конфигурация приложения «Преемственность»."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class SuccessionConfig(AppConfig):
    """Критичные должности, планы преемственности, назначения резервистов."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.succession"
    label = "succession"
    verbose_name = _("Преемственность")
