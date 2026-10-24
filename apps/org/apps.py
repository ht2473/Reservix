"""Конфигурация приложения «Организационная структура»."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class OrgConfig(AppConfig):
    """Филиалы, подразделения, должности и профили должностей."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.org"
    label = "org"
    verbose_name = _("Организационная структура")
