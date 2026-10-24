"""Конфигурация приложения «Публичный портал»."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PortalConfig(AppConfig):
    """Общедоступные страницы, база знаний, глоссарий, обратная связь."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.portal"
    label = "portal"
    verbose_name = _("Публичный портал")
