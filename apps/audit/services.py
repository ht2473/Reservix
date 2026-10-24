"""Сервисный слой журнала аудита."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.db import models

from apps.audit.middleware import get_audit_context
from apps.audit.models import AuditAction, AuditLog, AuditSeverity
from core.logging import get_logger

logger = get_logger("audit")

#: Поля, значения которых не попадают в журнал ни при каких условиях.
SENSITIVE_FIELDS = frozenset(
    {"password", "password_hash", "secret_key", "token", "otp_secret", "session_key"}
)

#: Действия, автоматически повышающие значимость записи.
_ELEVATED_ACTIONS: dict[str, str] = {
    AuditAction.DELETE: AuditSeverity.NOTICE,
    AuditAction.EXPORT: AuditSeverity.NOTICE,
    AuditAction.SETTINGS: AuditSeverity.WARNING,
    AuditAction.PERMISSION_DENIED: AuditSeverity.WARNING,
    AuditAction.LOGIN_FAILED: AuditSeverity.WARNING,
}


def record(
    action: str,
    *,
    obj: models.Model | None = None,
    message: str = "",
    changes: dict[str, Any] | None = None,
    severity: str = "",
    reason: str = "",
) -> AuditLog | None:
    """Создаёт запись журнала."""
    context = get_audit_context()

    try:
        entry = AuditLog(
            actor=context.actor,
            actor_repr=context.actor_repr,
            actor_role=context.actor_role,
            action=action,
            severity=severity or _ELEVATED_ACTIONS.get(action, AuditSeverity.INFO),
            message=message[:500],
            changes=changes or {},
            reason=(reason or context.reason)[:2000],
            ip_address=context.ip_address,
            user_agent=context.user_agent,
            request_path=context.request_path,
            request_method=context.request_method,
            request_id=context.request_id,
        )

        if obj is not None:
            entry.content_type = ContentType.objects.get_for_model(obj)
            entry.object_id = str(obj.pk)
            entry.object_repr = str(obj)[:400]

        entry.save()
        return entry

    except Exception as exc:
        logger.error(
            "Не удалось записать событие в журнал аудита",
            extra={"action": action, "error": str(exc)},
        )
        return None


def record_create(obj: models.Model, *, message: str = "") -> AuditLog | None:
    """Фиксирует создание объекта."""
    return record(
        AuditAction.CREATE,
        obj=obj,
        message=message or f"Создан объект: {obj._meta.verbose_name}",
        changes=snapshot(obj),
    )


def record_update(
    obj: models.Model, before: dict[str, Any], *, message: str = ""
) -> AuditLog | None:
    """Фиксирует изменение объекта."""
    diff = compare(before, snapshot(obj))
    if not diff:
        return None
    return record(
        AuditAction.UPDATE,
        obj=obj,
        message=message or f"Изменён объект: {obj._meta.verbose_name}",
        changes=diff,
    )


def record_delete(obj: models.Model, *, reason: str = "") -> AuditLog | None:
    """Фиксирует удаление объекта, сохраняя его состояние на момент удаления."""
    return record(
        AuditAction.DELETE,
        obj=obj,
        message=f"Удалён объект: {obj._meta.verbose_name}",
        changes=snapshot(obj),
        reason=reason,
    )


def record_view(obj: models.Model, *, message: str = "") -> AuditLog | None:
    """Фиксирует просмотр персональных данных."""
    return record(
        AuditAction.VIEW,
        obj=obj,
        message=message or f"Просмотр карточки: {obj}",
        severity=AuditSeverity.INFO,
    )


def record_export(*, message: str, count: int = 0, fmt: str = "") -> AuditLog | None:
    """Фиксирует выгрузку данных."""
    return record(
        AuditAction.EXPORT,
        message=message,
        changes={
            "records": {"label": "Количество записей", "old": None, "new": count},
            "format": {"label": "Формат", "old": None, "new": fmt},
        },
        severity=AuditSeverity.NOTICE,
    )


def record_status_change(
    obj: models.Model, old_status: str, new_status: str, *, reason: str = ""
) -> AuditLog | None:
    """Фиксирует изменение статуса записи резерва, плана или заявки."""
    return record(
        AuditAction.STATUS_CHANGE,
        obj=obj,
        message=f"Статус изменён: {old_status} → {new_status}",
        changes={"status": {"label": "Статус", "old": old_status, "new": new_status}},
        reason=reason,
        severity=AuditSeverity.NOTICE,
    )


# ---------------------------------------------------------------------------
# Снимки и сравнение
# ---------------------------------------------------------------------------
def snapshot(obj: models.Model, fields: list[str] | None = None) -> dict[str, Any]:
    """Снимает значения полей объекта для последующего сравнения."""
    result: dict[str, Any] = {}

    for field in obj._meta.fields:
        name = field.name
        if name in SENSITIVE_FIELDS or (fields and name not in fields):
            continue

        value = getattr(obj, field.attname, None)
        result[name] = {
            "label": str(field.verbose_name),
            "value": _serialize(value),
            "display": _display(obj, field),
        }

    return result


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Сравнивает два снимка и возвращает только изменившиеся поля."""
    diff: dict[str, Any] = {}

    for name, after_data in after.items():
        before_data = before.get(name)
        if before_data is None:
            continue
        if before_data.get("value") == after_data.get("value"):
            continue
        diff[name] = {
            "label": after_data.get("label", name),
            "old": before_data.get("display") or before_data.get("value"),
            "new": after_data.get("display") or after_data.get("value"),
        }

    return diff


def _serialize(value: Any) -> Any:
    """Приводит значение поля к типу, пригодному для хранения в JSON."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.strftime("%H:%M:%S")
    return str(value)


def _display(obj: models.Model, field: models.Field) -> str:
    """Возвращает человекочитаемое представление значения."""
    if field.choices:
        method = f"get_{field.name}_display"
        if hasattr(obj, method):
            return str(getattr(obj, method)())

    if field.is_relation and field.many_to_one:
        related = getattr(obj, field.name, None)
        return str(related) if related is not None else ""

    return ""
