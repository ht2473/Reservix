"""Конфигурация приложения «Отчётность»."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ReportingConfig(AppConfig):
    """Формирование документов в форматах .docx, .xlsx и .pdf."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reporting"
    label = "reporting"
    verbose_name = _("Отчётность")
