"""Конфигурация приложения «Программный интерфейс»."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ApiConfig(AppConfig):
    """REST API на django-ninja со спецификацией OpenAPI."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.api"
    label = "api"
    verbose_name = _("Программный интерфейс")
