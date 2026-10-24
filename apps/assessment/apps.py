"""Конфигурация приложения «Оценка персонала»."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AssessmentConfig(AppConfig):
    """Кампании оценки, круговая оценка 360°, тестирование, результаты."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.assessment"
    label = "assessment"
    verbose_name = _("Оценка персонала")
