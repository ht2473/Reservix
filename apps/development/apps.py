"""Конфигурация приложения «Развитие персонала»."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class DevelopmentConfig(AppConfig):
    """Индивидуальные планы развития, мероприятия, наставничество."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.development"
    label = "development"
    verbose_name = _("Развитие персонала")
