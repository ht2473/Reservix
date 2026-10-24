"""Конфигурация приложения «Компетенции»."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CompetenciesConfig(AppConfig):
    """Справочник компетенций, шкалы оценки, модели компетенций должностей."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.competencies"
    label = "competencies"
    verbose_name = _("Компетенции")
