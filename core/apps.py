"""Конфигурация приложения `core`."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


def _keep_default_ordering(self) -> None:
    """
    Инициализация запроса modeltranslation без явной сортировки по умолчанию.

    Пакет переносит Meta.ordering в явный ORDER BY, и Django начинает включать
    поля сортировки в GROUP BY запросов values().annotate(): агрегаты дробятся.
    Неявная сортировка по умолчанию в группировку не попадает.
    """
    self._rewrite = True
    self._populate = None


class CoreConfig(AppConfig):
    """Общий слой: абстрактные модели, права, навигация, шаблонные теги."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = _("Общие компоненты")

    def ready(self) -> None:
        from modeltranslation.manager import MultilingualQuerySet
        from modeltranslation.translator import translator

        from django.db.models.signals import pre_save

        from core.data_translation import fill_missing_english

        MultilingualQuerySet._post_init = _keep_default_ordering  # type: ignore[method-assign]

        for model in translator.get_registered_models():
            pre_save.connect(
                fill_missing_english, sender=model, dispatch_uid=f"en-{model._meta.label}"
            )
