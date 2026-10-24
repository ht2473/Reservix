"""Абстрактные модели и менеджеры, общие для всех приложений."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Self, cast

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from apps.accounts.models import User


class TimeStampedQuerySet(models.QuerySet):
    """Набор запросов с типовыми временными срезами."""

    def created_after(self, moment) -> Self:
        return self.filter(created_at__gte=moment)

    def created_between(self, start, end) -> Self:
        return self.filter(created_at__gte=start, created_at__lte=end)

    def recent(self, days: int = 30) -> Self:
        """Записи, созданные за последние `days` дней."""
        return self.filter(created_at__gte=timezone.now() - dt.timedelta(days=days))


class TimeStampedModel(models.Model):
    """Базовая модель с отметками создания и последнего изменения."""

    created_at = models.DateTimeField(
        _("создано"),
        auto_now_add=True,
        db_index=True,
        help_text=_("Момент создания записи"),
    )
    updated_at = models.DateTimeField(
        _("изменено"),
        auto_now=True,
        help_text=_("Момент последнего изменения записи"),
    )

    objects = TimeStampedQuerySet.as_manager()

    class Meta:
        abstract = True
        get_latest_by = "created_at"


class AuthoredModel(models.Model):
    """Фиксация авторства изменений."""

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("автор записи"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(app_label)s_%(class)s_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("последний редактор"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(app_label)s_%(class)s_updated",
    )

    class Meta:
        abstract = True


class SoftDeleteQuerySet(models.QuerySet):
    """Набор запросов, различающий действующие и удалённые записи."""

    def alive(self) -> Self:
        return self.filter(deleted_at__isnull=True)

    def dead(self) -> Self:
        return self.filter(deleted_at__isnull=False)

    def delete(self):
        """Массовое мягкое удаление: помечает записи вместо физического удаления."""
        return self.update(deleted_at=timezone.now())

    def hard_delete(self):
        """Физическое удаление. Применяется только в задачах очистки данных."""
        return super().delete()


class SoftDeleteManager(models.Manager):
    """Менеджер по умолчанию, скрывающий мягко удалённые записи."""

    def get_queryset(self) -> SoftDeleteQuerySet:
        # Приведение выражает договорённость: менеджер подключается только к моделям, наследующим
        # `SoftDeleteModel`, и их наборы запросов происходят от `SoftDeleteQuerySet`.
        queryset = cast(SoftDeleteQuerySet, super().get_queryset())
        return queryset.alive()


class SoftDeleteModel(models.Model):
    """Мягкое удаление для сущностей, на которые ссылается история и отчётность."""

    deleted_at = models.DateTimeField(
        _("удалено"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Заполняется при мягком удалении; пусто у действующих записей"),
    )
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("удалил"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(app_label)s_%(class)s_deleted",
    )

    objects = SoftDeleteManager()
    all_objects = SoftDeleteQuerySet.as_manager()

    class Meta:
        abstract = True

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def delete(self, using=None, keep_parents=False, *, user: User | None = None):
        """Помечает запись удалённой, не затрагивая связанные объекты."""
        self.deleted_at = timezone.now()
        self.deleted_by = user
        self.save(update_fields=["deleted_at", "deleted_by"])

    def restore(self) -> None:
        """Возвращает мягко удалённую запись в работу."""
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=["deleted_at", "deleted_by"])

    def hard_delete(self, using=None, keep_parents=False):
        """Физическое удаление в обход мягкого. Используется в командах очистки."""
        super().delete(using=using, keep_parents=keep_parents)


class ActiveQuerySet(models.QuerySet):
    """Набор запросов для справочников с признаком активности."""

    def active(self) -> Self:
        return self.filter(is_active=True)

    def inactive(self) -> Self:
        return self.filter(is_active=False)


class ActivatableModel(models.Model):
    """Справочник, элементы которого выводятся из обращения без удаления."""

    is_active = models.BooleanField(
        _("действует"),
        default=True,
        db_index=True,
        help_text=_("Снимите отметку, чтобы скрыть элемент из форм выбора"),
    )

    objects = ActiveQuerySet.as_manager()

    class Meta:
        abstract = True


class OrderedModel(models.Model):
    """Элемент справочника с управляемым порядком вывода."""

    sort_order = models.PositiveIntegerField(
        _("порядок"),
        default=100,
        db_index=True,
        help_text=_("Меньшее значение выводится выше в списках"),
    )

    class Meta:
        abstract = True
        ordering = ["sort_order"]


class CodedModel(models.Model):
    """Сущность с человекочитаемым кодом."""

    code = models.SlugField(
        _("код"),
        max_length=64,
        unique=True,
        help_text=_("Латиница, цифры, дефис. Используется в выгрузках и API"),
    )

    class Meta:
        abstract = True


class UUIDModel(models.Model):
    """Публичный идентификатор, безопасный для передачи наружу."""

    public_id = models.UUIDField(
        _("публичный идентификатор"),
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
    )

    class Meta:
        abstract = True


class DescribedModel(models.Model):
    """Именованная сущность с необязательным описанием."""

    name = models.CharField(_("наименование"), max_length=255)
    description = models.TextField(_("описание"), blank=True)

    class Meta:
        abstract = True

    def __str__(self) -> str:
        return self.name


class ReferenceModel(CodedModel, DescribedModel, OrderedModel, ActivatableModel, TimeStampedModel):
    """Готовая основа для простого справочника."""

    class Meta:
        abstract = True
        ordering = ["sort_order", "name"]
