"""Автоматическая фиксация событий безопасности."""

from __future__ import annotations

from typing import Any

from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.dispatch import receiver

from apps.audit.middleware import get_audit_context
from apps.audit.models import AuditAction, AuditSeverity
from apps.audit.services import record
from core.logging import get_logger

logger = get_logger("audit")


@receiver(user_logged_in, dispatch_uid="audit_user_logged_in")
def on_login(sender: Any, request, user, **kwargs) -> None:
    """Фиксирует успешный вход и сохраняет запись в личной истории пользователя."""
    from apps.accounts.models import LoginRecord

    context = get_audit_context()

    record(
        AuditAction.LOGIN,
        obj=user,
        message=f"Вход в систему: {user.email}",
        severity=AuditSeverity.INFO,
    )

    LoginRecord.objects.create(
        user=user,
        outcome=LoginRecord.Outcome.SUCCESS,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
        session_key=request.session.session_key or "",
    )

    user.touch_activity()


@receiver(user_logged_out, dispatch_uid="audit_user_logged_out")
def on_logout(sender: Any, request, user, **kwargs) -> None:
    """Фиксирует выход из системы."""
    if user is None:
        return

    from apps.accounts.models import LoginRecord

    context = get_audit_context()

    record(
        AuditAction.LOGOUT,
        obj=user,
        message=f"Выход из системы: {user.email}",
        severity=AuditSeverity.INFO,
    )

    LoginRecord.objects.create(
        user=user,
        outcome=LoginRecord.Outcome.LOGOUT,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
        session_key=getattr(request.session, "session_key", "") or "",
    )


@receiver(user_login_failed, dispatch_uid="audit_user_login_failed")
def on_login_failed(sender: Any, credentials: dict, request=None, **kwargs) -> None:
    """Фиксирует неудачную попытку входа."""
    from apps.accounts.models import LoginRecord, User

    context = get_audit_context()
    attempted = (credentials or {}).get("username", "")[:255]

    record(
        AuditAction.LOGIN_FAILED,
        message=f"Неудачная попытка входа: {attempted or 'адрес не указан'}",
        severity=AuditSeverity.WARNING,
    )

    # Попытка связать запись с существующей учётной записью: это позволяет сотруднику увидеть на
    # странице безопасности, что кто-то подбирает пароль именно к его записи.
    user = User.objects.filter(email__iexact=attempted).first() if attempted else None

    LoginRecord.objects.create(
        user=user,
        email_attempted=attempted,
        outcome=LoginRecord.Outcome.FAILED,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
    )


def on_axes_lockout(*args, **kwargs) -> None:
    """Фиксирует блокировку учётной записи после исчерпания попыток входа."""
    from apps.accounts.models import LoginRecord

    context = get_audit_context()
    credentials = kwargs.get("credentials") or {}
    attempted = credentials.get("username", "")[:255]

    record(
        AuditAction.LOGIN_FAILED,
        message=f"Учётная запись заблокирована после серии неудачных попыток: {attempted}",
        severity=AuditSeverity.CRITICAL,
    )

    LoginRecord.objects.create(
        email_attempted=attempted,
        outcome=LoginRecord.Outcome.BLOCKED,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
    )

    logger.warning("Сработала блокировка по числу попыток входа", extra={"email": attempted})


try:  # pragma: no cover - зависит от настроек окружения
    from axes.signals import user_locked_out

    user_locked_out.connect(on_axes_lockout, dispatch_uid="audit_axes_lockout")
except ImportError:
    logger.debug("Приложение axes не установлено, сигнал блокировки не подключён")
