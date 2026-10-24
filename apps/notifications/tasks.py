"""Фоновые задачи модуля уведомлений."""

from __future__ import annotations

from celery import shared_task

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.utils import timezone

from apps.notifications.models import Notification
from core.logging import get_logger

logger = get_logger("notifications")

#: Предел числа писем, отправляемых одной задачей рассылки. Ограничение
#: удерживает время выполнения в пределах мягкого таймаута воркера.
BATCH_LIMIT = 200


@shared_task(
    bind=True,
    autoretry_for=(OSError,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=5,
    retry_jitter=True,
)
def send_notification_email(self, notification_id: int) -> str:
    """Отправляет письмо по одному уведомлению."""
    notification = (
        Notification.objects.filter(pk=notification_id)
        .select_related("recipient", "recipient__profile", "actor")
        .first()
    )

    if notification is None:
        logger.warning("Уведомление не найдено", extra={"notification_id": notification_id})
        return "not_found"

    if notification.emailed_at is not None:
        return "already_sent"

    recipient = notification.recipient
    if not recipient.is_active or not recipient.email:
        return "skipped"

    context = {
        "notification": notification,
        "recipient": recipient,
        "site_name": settings.SITE_NAME,
        "site_domain": settings.SITE_DOMAIN,
        "action_url": _absolute_url(notification.url),
    }

    subject = f"{settings.EMAIL_SUBJECT_PREFIX}{notification.title}"
    body = render_to_string("notifications/email/notification.txt", context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient.email],
    )
    message.send(fail_silently=False)

    notification.emailed_at = timezone.now()
    notification.save(update_fields=["emailed_at"])

    logger.info(
        "Уведомление отправлено письмом",
        extra={"notification_id": notification.pk, "recipient_id": recipient.pk},
    )
    return "sent"


@shared_task
def send_pending_emails() -> dict[str, int]:
    """Досылает письма, не отправленные по разовым задачам."""
    pending = list(
        Notification.objects.pending_email()
        .select_related("recipient")
        .order_by("created_at")[:BATCH_LIMIT]
    )

    if not pending:
        return {"sent": 0, "failed": 0}

    sent = failed = 0
    connection = get_connection()

    try:
        connection.open()
        for notification in pending:
            try:
                send_notification_email(notification.pk)
                sent += 1
            except Exception as exc:
                failed += 1
                logger.error(
                    "Не удалось отправить письмо",
                    extra={"notification_id": notification.pk, "error": str(exc)},
                )
    finally:
        connection.close()

    logger.info("Рассылка завершена", extra={"sent": sent, "failed": failed})
    return {"sent": sent, "failed": failed}


@shared_task
def cleanup_notifications(days: int = 180) -> int:
    """Удаляет прочитанные уведомления старше указанного срока."""
    from apps.notifications.services import cleanup_old

    deleted = cleanup_old(days)
    logger.info("Очистка уведомлений выполнена", extra={"deleted": deleted, "days": days})
    return deleted


def _absolute_url(path: str) -> str:
    """Дополняет относительный адрес схемой и доменом."""
    if not path:
        return f"https://{settings.SITE_DOMAIN}/"
    if path.startswith("http"):
        return path
    return f"https://{settings.SITE_DOMAIN}{path}"
