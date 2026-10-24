"""Сервисный слой уведомлений."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from django.db import models
from django.db.models import Count, Q
from django.urls import NoReverseMatch
from django.utils import timezone

from apps.notifications.models import Notification, NotificationKind
from core.constants import Role
from core.logging import get_logger

if TYPE_CHECKING:
    from apps.accounts.models import User

logger = get_logger("notifications")

#: Поводы, о которых уведомляют письмом всегда — они требуют реакции вне системы.
ALWAYS_EMAIL_KINDS = frozenset(
    {
        NotificationKind.RESERVE_INCLUDED,
        NotificationKind.RESERVE_EXCLUDED,
        NotificationKind.ASSESSMENT_REQUESTED,
        NotificationKind.APPOINTMENT_MADE,
        NotificationKind.ACTIVITY_OVERDUE,
    }
)

#: Окно подавления дубликатов.
DEDUPLICATION_WINDOW = dt.timedelta(hours=6)


def notify(
    recipient: User,
    kind: str,
    title: str,
    *,
    body: str = "",
    obj: models.Model | None = None,
    url: str = "",
    actor: User | None = None,
    force_email: bool | None = None,
    deduplicate: bool = True,
) -> Notification | None:
    """Создаёт уведомление для пользователя."""
    if not recipient.is_active:
        return None
    if actor is not None and actor.pk == recipient.pk:
        return None

    if deduplicate and obj is not None and _is_duplicate(recipient, kind, obj):
        logger.debug(
            "Уведомление подавлено как дубликат",
            extra={"recipient_id": recipient.pk, "kind": kind},
        )
        return None

    should_email = _resolve_email_flag(recipient, kind, force_email)

    notification = Notification(
        recipient=recipient,
        actor=actor,
        kind=kind,
        title=title[:200],
        body=body,
        url=url or _default_url(obj),
        should_email=should_email,
    )

    if obj is not None:
        from django.contrib.contenttypes.models import ContentType

        notification.content_type = ContentType.objects.get_for_model(obj)
        notification.object_id = str(obj.pk)

    notification.save()

    if should_email:
        _enqueue_email(notification)

    return notification


def notify_many(recipients: list[User], kind: str, title: str, **kwargs: Any) -> list[Notification]:
    """Создаёт уведомления для группы пользователей."""
    seen: set[int] = set()
    created: list[Notification] = []

    for recipient in recipients:
        if recipient.pk in seen:
            continue
        seen.add(recipient.pk)
        notification = notify(recipient, kind, title, **kwargs)
        if notification is not None:
            created.append(notification)

    return created


def get_navigation_counters(user: User) -> dict[str, int]:
    """Возвращает значения значков в меню рабочего места."""
    counters: dict[str, int] = {}

    counters["unread_notifications"] = Notification.objects.for_user(user).unread().count()

    if user.role == Role.MANAGER:
        counters.update(_manager_counters(user))
    elif user.role == Role.ADMIN:
        counters.update(_admin_counters())

    return {key: value for key, value in counters.items() if value}


def _manager_counters(user: User) -> dict[str, int]:
    """Счётчики задач руководителя."""
    rows = (
        Notification.objects.for_user(user)
        .unread()
        .filter(
            kind__in=[
                NotificationKind.ASSESSMENT_REQUESTED,
                NotificationKind.NOMINATION_SUBMITTED,
            ]
        )
        .values("kind")
        .annotate(total=Count("id"))
    )
    mapping = {row["kind"]: row["total"] for row in rows}
    return {
        "pending_assessments": mapping.get(NotificationKind.ASSESSMENT_REQUESTED, 0),
        "pending_approvals": mapping.get(NotificationKind.NOMINATION_SUBMITTED, 0),
    }


def _admin_counters() -> dict[str, int]:
    """Счётчики администратора: необработанные обращения обратной связи."""
    try:
        from apps.portal.models import Feedback

        return {"new_feedback": Feedback.objects.filter(status=Feedback.Status.NEW).count()}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Внутренние функции
# ---------------------------------------------------------------------------
def _is_duplicate(recipient: User, kind: str, obj: models.Model) -> bool:
    """Проверяет наличие такого же уведомления в пределах окна подавления."""
    from django.contrib.contenttypes.models import ContentType

    return Notification.objects.filter(
        recipient=recipient,
        kind=kind,
        content_type=ContentType.objects.get_for_model(obj),
        object_id=str(obj.pk),
        created_at__gte=timezone.now() - DEDUPLICATION_WINDOW,
    ).exists()


def _resolve_email_flag(recipient: User, kind: str, force: bool | None) -> bool:
    """Определяет, отправлять ли письмо."""
    if force is not None:
        return force

    profile = getattr(recipient, "profile", None)
    if profile is not None and not profile.email_notifications:
        return kind in {NotificationKind.RESERVE_INCLUDED, NotificationKind.RESERVE_EXCLUDED}

    return kind in ALWAYS_EMAIL_KINDS


def _default_url(obj: models.Model | None) -> str:
    """Пытается получить адрес объекта, к которому относится уведомление."""
    if obj is None:
        return ""
    getter = getattr(obj, "get_absolute_url", None)
    if getter is None:
        return ""
    try:
        return getter()
    except NoReverseMatch:  # pragma: no cover - маршрут может быть не подключён
        return ""


def _enqueue_email(notification: Notification) -> None:
    """Ставит письмо в очередь отправки."""
    try:
        from apps.notifications.tasks import send_notification_email

        send_notification_email.delay(notification.pk)
    except Exception as exc:
        logger.warning(
            "Не удалось поставить письмо в очередь",
            extra={"notification_id": notification.pk, "error": str(exc)},
        )


def mark_all_read(user: User) -> int:
    """Отмечает все уведомления пользователя прочитанными."""
    return Notification.objects.for_user(user).mark_read()


def unread_count(user: User) -> int:
    """Число непрочитанных уведомлений."""
    return Notification.objects.for_user(user).unread().count()


def cleanup_old(days: int = 180) -> int:
    """Удаляет прочитанные уведомления старше указанного срока."""
    threshold = timezone.now() - dt.timedelta(days=days)
    deleted, _ = Notification.objects.filter(
        Q(read_at__isnull=False) & Q(created_at__lt=threshold)
    ).delete()
    return deleted
