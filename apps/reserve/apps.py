"""Конфигурация приложения «Кадровый резерв»."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ReserveConfig(AppConfig):
    """Записи резерва, статусный процесс, заявки и согласования."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reserve"
    label = "reserve"
    verbose_name = _("Кадровый резерв")
