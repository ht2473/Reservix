"""Журнал аудита."""

from __future__ import annotations

import datetime as dt
from typing import Any, Self

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.settings_types import get_reservix_settings


class AuditAction(models.TextChoices):
    """Типы фиксируемых действий."""

    CREATE = "create", _("Создание")
    UPDATE = "update", _("Изменение")
    DELETE = "delete", _("Удаление")
    VIEW = "view", _("Просмотр персональных данных")
    EXPORT = "export", _("Выгрузка данных")
    LOGIN = "login", _("Вход в систему")
    LOGOUT = "logout", _("Выход из системы")
    LOGIN_FAILED = "login_failed", _("Неудачная попытка входа")
    PERMISSION_DENIED = "denied", _("Отказ в доступе")
    STATUS_CHANGE = "status", _("Изменение статуса")
    APPROVE = "approve", _("Согласование")
    REJECT = "reject", _("Отклонение")
    CALCULATE = "calculate", _("Пересчёт показателей")
    SETTINGS = "settings", _("Изменение настроек")


class AuditSeverity(models.TextChoices):
    """Значимость события."""

    INFO = "info", _("Обычное")
    NOTICE = "notice", _("Значимое")
    WARNING = "warning", _("Требует внимания")
    CRITICAL = "critical", _("Критическое")


class AuditLogQuerySet(models.QuerySet):
    """Выборки журнала для страницы аудита."""

    def by_user(self, user_id: int) -> Self:
        return self.filter(actor_id=user_id)

    def by_action(self, *actions: str) -> Self:
        return self.filter(action__in=actions)

    def security_events(self) -> Self:
        """События, относящиеся к безопасности доступа."""
        return self.filter(
            action__in=[
                AuditAction.LOGIN_FAILED,
                AuditAction.PERMISSION_DENIED,
                AuditAction.EXPORT,
                AuditAction.SETTINGS,
            ]
        )

    def for_object(self, obj) -> Self:
        """Все записи, относящиеся к конкретному объекту."""
        return self.filter(
            content_type=ContentType.objects.get_for_model(obj),
            object_id=str(obj.pk),
        )

    def recent(self, days: int = 30) -> Self:
        return self.filter(created_at__gte=timezone.now() - dt.timedelta(days=days))

    def expired(self) -> Self:
        """Записи за пределами срока хранения — кандидаты на удаление."""
        retention = get_reservix_settings()["AUDIT_RETENTION_DAYS"]
        return self.filter(created_at__lt=timezone.now() - dt.timedelta(days=retention))


class AuditLog(models.Model):
    """Запись журнала аудита."""

    # -- Кто ----------------------------------------------------------------
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("пользователь"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_records",
        help_text=_("Пустое значение означает системное действие"),
    )
    actor_repr = models.CharField(
        _("пользователь (текстом)"),
        max_length=255,
        blank=True,
        help_text=_("Сохраняется на случай удаления учётной записи"),
    )
    actor_role = models.CharField(_("роль на момент действия"), max_length=20, blank=True)

    # -- Что ----------------------------------------------------------------
    action = models.CharField(
        _("действие"), max_length=20, choices=AuditAction.choices, db_index=True
    )
    severity = models.CharField(
        _("значимость"),
        max_length=10,
        choices=AuditSeverity.choices,
        default=AuditSeverity.INFO,
        db_index=True,
    )
    content_type = models.ForeignKey(
        ContentType,
        verbose_name=_("тип объекта"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    object_id = models.CharField(
        _("идентификатор объекта"), max_length=64, blank=True, db_index=True
    )
    content_object = GenericForeignKey("content_type", "object_id")
    object_repr = models.CharField(
        _("объект (текстом)"),
        max_length=400,
        blank=True,
        help_text=_("Читаемое описание объекта на момент действия"),
    )

    # -- Подробности --------------------------------------------------------
    message = models.CharField(_("описание"), max_length=500, blank=True)
    changes = models.JSONField(
        _("изменения"),
        default=dict,
        blank=True,
        help_text=_("Значения полей до и после изменения"),
    )
    reason = models.TextField(
        _("основание"),
        blank=True,
        help_text=_("Комментарий пользователя, обосновывающий решение"),
    )

    # -- Контекст запроса ---------------------------------------------------
    ip_address = models.GenericIPAddressField(_("IP-адрес"), null=True, blank=True)
    user_agent = models.CharField(_("клиент"), max_length=400, blank=True)
    request_path = models.CharField(_("адрес запроса"), max_length=255, blank=True)
    request_method = models.CharField(_("метод"), max_length=10, blank=True)
    request_id = models.CharField(
        _("идентификатор запроса"),
        max_length=36,
        blank=True,
        db_index=True,
        help_text=_("Объединяет записи, порождённые одним запросом"),
    )

    created_at = models.DateTimeField(_("время"), auto_now_add=True, db_index=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        verbose_name = _("запись журнала аудита")
        verbose_name_plural = _("журнал аудита")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at", "action"], name="audit_time_action_idx"),
            models.Index(fields=["actor", "-created_at"], name="audit_actor_time_idx"),
            models.Index(
                fields=["content_type", "object_id", "-created_at"],
                name="audit_object_time_idx",
            ),
            models.Index(fields=["severity", "-created_at"], name="audit_severity_time_idx"),
        ]

    def __str__(self) -> str:
        moment = f"{self.created_at:%d.%m.%Y %H:%M}"
        return f"{moment} · {self.get_action_display()} · {self.object_repr}"

    def save(self, *args, **kwargs) -> None:
        """Запрещает изменение существующей записи."""
        if self.pk is not None:
            raise ValueError("Записи журнала аудита неизменяемы")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # pragma: no cover - защита от случайного вызова
        raise ValueError("Записи журнала удаляются только периодической очисткой по сроку хранения")

    # -- Представление изменений -------------------------------------------
    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    @property
    def changed_fields(self) -> list[dict[str, Any]]:
        """Разворачивает словарь изменений в список для вывода таблицей."""
        result: list[dict[str, Any]] = []
        for field, values in (self.changes or {}).items():
            if not isinstance(values, dict):
                continue
            result.append(
                {
                    "field": field,
                    "label": values.get("label", field),
                    "old": values.get("old"),
                    "new": values.get("new"),
                }
            )
        return sorted(result, key=lambda item: item["label"])
