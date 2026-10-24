"""Конфигурация приложения «Аналитика»."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AnalyticsConfig(AppConfig):
    """Индекс готовности, методы MCDA, матрица 9-box, метрики резерва."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.analytics"
    label = "analytics"
    verbose_name = _("Аналитика")
