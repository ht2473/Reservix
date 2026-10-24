"""Конфигурация приложения «Сотрудники»."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PeopleConfig(AppConfig):
    """Карточки сотрудников, карьерная история, кадровые данные."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.people"
    label = "people"
    verbose_name = _("Сотрудники")
