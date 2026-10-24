"""Конфигурация приложения «Администрирование»."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AdministrationConfig(AppConfig):
    """Панель управления системой, справочниками и доступом."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.administration"
    label = "administration"
    verbose_name = _("Администрирование")
