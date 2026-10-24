"""Уведомления пользователей."""

from __future__ import annotations

from typing import Self

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.constants import Signal


class NotificationKind(models.TextChoices):
    """Повод для уведомления."""

    RESERVE_INCLUDED = "reserve_included", _("Включение в кадровый резерв")
    RESERVE_EXCLUDED = "reserve_excluded", _("Исключение из кадрового резерва")
    RESERVE_STATUS = "reserve_status", _("Изменение статуса в резерве")
    NOMINATION_SUBMITTED = "nomination_submitted", _("Поступила заявка на включение")
    NOMINATION_APPROVED = "nomination_approved", _("Заявка согласована")
    NOMINATION_REJECTED = "nomination_rejected", _("Заявка отклонена")
    PLAN_ASSIGNED = "plan_assigned", _("Назначен план развития")
    ACTIVITY_DUE_SOON = "activity_due_soon", _("Приближается срок мероприятия")
    ACTIVITY_OVERDUE = "activity_overdue", _("Мероприятие просрочено")
    ACTIVITY_COMPLETED = "activity_completed", _("Мероприятие выполнено")
    ASSESSMENT_REQUESTED = "assessment_requested", _("Требуется оценка")
    ASSESSMENT_COMPLETED = "assessment_completed", _("Оценка завершена")
    CAMPAIGN_STARTED = "campaign_started", _("Началась кампания оценки")
    MENTORSHIP_ASSIGNED = "mentorship_assigned", _("Назначено наставничество")
    APPOINTMENT_MADE = "appointment_made", _("Состоялось назначение")
    READINESS_CHANGED = "readiness_changed", _("Изменился уровень готовности")
    REPORT_READY = "report_ready", _("Отчёт сформирован")
    SYSTEM = "system", _("Системное сообщение")


#: Сигнальная окраска уведомления по поводу.
KIND_SIGNALS: dict[str, str] = {
    NotificationKind.RESERVE_INCLUDED: Signal.READY,
    NotificationKind.RESERVE_EXCLUDED: Signal.RISK,
    NotificationKind.NOMINATION_APPROVED: Signal.READY,
    NotificationKind.NOMINATION_REJECTED: Signal.RISK,
    NotificationKind.ACTIVITY_DUE_SOON: Signal.DEVELOP,
    NotificationKind.ACTIVITY_OVERDUE: Signal.RISK,
    NotificationKind.ACTIVITY_COMPLETED: Signal.READY,
    NotificationKind.APPOINTMENT_MADE: Signal.READY,
}


class NotificationQuerySet(models.QuerySet):
    """Выборки уведомлений."""

    def unread(self) -> Self:
        return self.filter(read_at__isnull=True)

    def for_user(self, user) -> Self:
        return self.filter(recipient=user)

    def actionable(self) -> Self:
        """Уведомления, требующие действия пользователя."""
        return self.filter(
            kind__in=[
                NotificationKind.ASSESSMENT_REQUESTED,
                NotificationKind.NOMINATION_SUBMITTED,
                NotificationKind.ACTIVITY_OVERDUE,
                NotificationKind.ACTIVITY_DUE_SOON,
            ]
        )

    def pending_email(self) -> Self:
        """Уведомления, ожидающие отправки по электронной почте."""
        return self.filter(should_email=True, emailed_at__isnull=True)

    def mark_read(self) -> int:
        """Массовая отметка о прочтении."""
        return self.filter(read_at__isnull=True).update(read_at=timezone.now())


class Notification(models.Model):
    """Уведомление пользователя."""

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("получатель"),
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("инициатор"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="triggered_notifications",
        help_text=_("Пустое значение означает системное событие"),
    )

    kind = models.CharField(
        _("повод"), max_length=32, choices=NotificationKind.choices, db_index=True
    )
    title = models.CharField(_("заголовок"), max_length=200)
    body = models.TextField(_("текст"), blank=True)
    url = models.CharField(
        _("ссылка"),
        max_length=500,
        blank=True,
        help_text=_("Адрес, на который ведёт уведомление"),
    )

    content_type = models.ForeignKey(
        ContentType,
        verbose_name=_("тип объекта"),
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    object_id = models.CharField(_("идентификатор объекта"), max_length=64, blank=True)
    content_object = GenericForeignKey("content_type", "object_id")

    read_at = models.DateTimeField(_("прочитано"), null=True, blank=True, db_index=True)
    should_email = models.BooleanField(
        _("отправить письмо"),
        default=False,
        help_text=_("Учитывает настройки уведомлений получателя"),
    )
    emailed_at = models.DateTimeField(_("письмо отправлено"), null=True, blank=True)
    created_at = models.DateTimeField(_("создано"), auto_now_add=True, db_index=True)

    objects = NotificationQuerySet.as_manager()

    class Meta:
        verbose_name = _("уведомление")
        verbose_name_plural = _("уведомления")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "read_at"], name="notif_recipient_read_idx"),
            models.Index(fields=["recipient", "-created_at"], name="notif_recipient_time_idx"),
            models.Index(
                fields=["should_email", "emailed_at"],
                name="notif_email_queue_idx",
                condition=models.Q(should_email=True, emailed_at__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.recipient.short_name}: {self.title}"

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    @property
    def signal(self) -> str:
        """Сигнальная окраска для интерфейса."""
        return KIND_SIGNALS.get(self.kind, Signal.NEUTRAL)

    def mark_read(self) -> None:
        """Отмечает уведомление прочитанным, если оно ещё не отмечено."""
        if self.read_at is None:
            self.read_at = timezone.now()
            self.save(update_fields=["read_at"])
