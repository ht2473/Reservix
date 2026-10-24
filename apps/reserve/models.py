"""Кадровый резерв."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Self

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.constants import ReadinessLevel
from core.models import (
    ActivatableModel,
    AuthoredModel,
    DescribedModel,
    OrderedModel,
    TimeStampedModel,
    UUIDModel,
)
from core.settings_types import get_reservix_settings
from core.validators import code_validator


class ReserveGroupKind(models.TextChoices):
    """Вид резерва."""

    OPERATIONAL = "operational", _("Оперативный резерв")
    STRATEGIC = "strategic", _("Стратегический резерв")
    MANAGERIAL = "managerial", _("Управленческий резерв")
    YOUTH = "youth", _("Молодёжный резерв")
    PROFESSIONAL = "professional", _("Профессиональный резерв")


class ReserveGroup(DescribedModel, OrderedModel, ActivatableModel, TimeStampedModel):
    """Группа резерва — программа подготовки со своими правилами."""

    code = models.SlugField(_("код"), max_length=48, unique=True, validators=[code_validator])
    kind = models.CharField(
        _("вид резерва"),
        max_length=20,
        choices=ReserveGroupKind.choices,
        default=ReserveGroupKind.OPERATIONAL,
        db_index=True,
    )
    horizon_months = models.PositiveSmallIntegerField(
        _("горизонт подготовки, месяцев"),
        default=12,
        help_text=_("Срок, за который резервист должен быть готов к назначению"),
    )
    max_age = models.PositiveSmallIntegerField(
        _("предельный возраст"),
        null=True,
        blank=True,
        help_text=_("Ограничение молодёжного резерва; пусто — без ограничения"),
    )
    min_tenure_months = models.PositiveSmallIntegerField(
        _("минимальный стаж в организации, месяцев"),
        default=6,
    )
    min_readiness_index = models.PositiveSmallIntegerField(
        _("минимальный индекс готовности"),
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text=_("Порог зачисления в группу по композитному индексу"),
    )
    capacity = models.PositiveSmallIntegerField(
        _("предельная численность"),
        null=True,
        blank=True,
        help_text=_("Пусто — численность не ограничена"),
    )
    curator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("куратор группы"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="curated_reserve_groups",
    )

    class Meta:
        verbose_name = _("группа резерва")
        verbose_name_plural = _("группы резерва")
        ordering = ["sort_order", "name"]
        indexes = [
            models.Index(fields=["kind", "is_active"], name="reservegroup_kind_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("reserve:group_detail", kwargs={"pk": self.pk})

    @property
    def member_count(self) -> int:
        return self.memberships.filter(left_at__isnull=True).count()

    @property
    def has_free_places(self) -> bool:
        return self.capacity is None or self.member_count < self.capacity


class ReserveStatus(models.TextChoices):
    """Состояние записи резерва."""

    CANDIDATE = "candidate", _("Кандидат")
    ACTIVE = "active", _("В резерве")
    INACTIVE = "inactive", _("Приостановлен")
    APPOINTED = "appointed", _("Назначен")
    EXCLUDED = "excluded", _("Исключён")


#: Статусы, при которых резервист считается состоящим в резерве.
ACTIVE_RESERVE_STATUSES = frozenset({ReserveStatus.CANDIDATE, ReserveStatus.ACTIVE})

#: Статусы, завершающие пребывание в резерве.
CLOSING_RESERVE_STATUSES = frozenset({ReserveStatus.APPOINTED, ReserveStatus.EXCLUDED})

#: Допустимые переходы статусов.
STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    ReserveStatus.CANDIDATE: frozenset({ReserveStatus.ACTIVE, ReserveStatus.EXCLUDED}),
    ReserveStatus.ACTIVE: frozenset(
        {ReserveStatus.INACTIVE, ReserveStatus.APPOINTED, ReserveStatus.EXCLUDED}
    ),
    ReserveStatus.INACTIVE: frozenset({ReserveStatus.ACTIVE, ReserveStatus.EXCLUDED}),
    ReserveStatus.APPOINTED: frozenset(),
    ReserveStatus.EXCLUDED: frozenset(),
}


class ReserveEntryQuerySet(models.QuerySet):
    """Выборки записей кадрового резерва."""

    def in_reserve(self) -> Self:
        """Действующие записи резерва."""
        return self.filter(status__in=ACTIVE_RESERVE_STATUSES)

    def ready_now(self) -> Self:
        """Резервисты, готовые к назначению немедленно."""
        return self.filter(status=ReserveStatus.ACTIVE, readiness_level=ReadinessLevel.HIGH)

    def for_position(self, position_id: int) -> Self:
        return self.filter(target_position_id=position_id)

    def for_employee(self, employee_id: int) -> Self:
        return self.filter(employee_id=employee_id)

    def appointed(self) -> Self:
        return self.filter(status=ReserveStatus.APPOINTED)

    def due_for_review(self, on_date: dt.date | None = None) -> Self:
        """Записи, срок пересмотра которых наступил."""
        moment = on_date or timezone.localdate()
        return self.filter(status__in=ACTIVE_RESERVE_STATUSES, review_date__lte=moment)

    def with_related(self) -> Self:
        return self.select_related(
            "employee",
            "employee__department",
            "employee__branch",
            "target_position",
            "target_position__grade",
            "reserve_group",
        )


class ReserveEntry(UUIDModel, AuthoredModel, TimeStampedModel):
    """Запись кадрового резерва: сотрудник — целевая должность."""

    #: Признак «карточку записи можно открыть текущему пользователю».
    can_open: bool = False

    employee = models.ForeignKey(
        "people.Employee",
        verbose_name=_("резервист"),
        on_delete=models.CASCADE,
        related_name="reserve_entries",
    )
    target_position = models.ForeignKey(
        "org.Position",
        verbose_name=_("целевая должность"),
        on_delete=models.PROTECT,
        related_name="reserve_entries",
    )
    reserve_group = models.ForeignKey(
        ReserveGroup,
        verbose_name=_("группа резерва"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="entries",
    )
    status = models.CharField(
        _("статус"),
        max_length=20,
        choices=ReserveStatus.choices,
        default=ReserveStatus.CANDIDATE,
        db_index=True,
    )
    readiness_level = models.CharField(
        _("уровень готовности"),
        max_length=20,
        choices=ReadinessLevel.choices,
        default=ReadinessLevel.LOW,
        db_index=True,
        help_text=_("Рассчитывается по индексу готовности; вручную не задаётся"),
    )
    readiness_index = models.DecimalField(
        _("индекс готовности"),
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text=_("Композитный индекс готовности резервиста, 0–100"),
    )
    readiness_calculated_at = models.DateTimeField(
        _("индекс рассчитан"), null=True, blank=True, editable=False
    )
    rank = models.PositiveSmallIntegerField(
        _("ранг в списке преемников"),
        null=True,
        blank=True,
        help_text=_("Место кандидата среди претендентов на должность"),
    )
    inclusion_date = models.DateField(
        _("дата включения"), default=timezone.localdate, db_index=True
    )
    expected_ready_date = models.DateField(
        _("плановая готовность"),
        null=True,
        blank=True,
        help_text=_("Дата, к которой резервист должен быть готов к назначению"),
    )
    review_date = models.DateField(
        _("дата пересмотра"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Когда состав резерва по этой записи подлежит пересмотру"),
    )
    exclusion_date = models.DateField(_("дата исключения"), null=True, blank=True)
    exclusion_reason = models.TextField(_("причина исключения"), blank=True)
    justification = models.TextField(
        _("обоснование включения"),
        blank=True,
        help_text=_("Переносится из заявки на выдвижение"),
    )
    notes = models.TextField(_("примечания"), blank=True)
    legacy_id = models.PositiveIntegerField(
        _("идентификатор в исходной базе"),
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )

    objects = ReserveEntryQuerySet.as_manager()

    class Meta:
        verbose_name = _("запись резерва")
        verbose_name_plural = _("кадровый резерв")
        ordering = ["-readiness_index", "employee__last_name"]
        indexes = [
            models.Index(fields=["status", "readiness_level"], name="reserve_status_ready_idx"),
            models.Index(fields=["target_position", "status"], name="reserve_position_idx"),
            models.Index(fields=["employee", "status"], name="reserve_employee_idx"),
            models.Index(fields=["-readiness_index"], name="reserve_index_idx"),
        ]
        constraints = [
            # Действующая запись «сотрудник — должность» может быть только одна.
            models.UniqueConstraint(
                fields=["employee", "target_position"],
                condition=Q(status__in=["candidate", "active", "inactive"]),
                name="reserve_unique_active_pair",
            ),
            models.CheckConstraint(
                condition=(
                    Q(exclusion_date__isnull=True)
                    | Q(exclusion_date__gte=models.F("inclusion_date"))
                ),
                name="reserve_exclusion_after_inclusion",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.employee.short_name} → {self.target_position.name}"

    def get_absolute_url(self) -> str:
        return reverse("reserve:entry_detail", kwargs={"public_id": self.public_id})

    # -- Состояние ----------------------------------------------------------
    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_RESERVE_STATUSES

    @property
    def is_ready(self) -> bool:
        return self.readiness_level == ReadinessLevel.HIGH

    @property
    def months_in_reserve(self) -> int:
        """Продолжительность пребывания в резерве в месяцах."""
        end = self.exclusion_date or timezone.localdate()
        months = (end.year - self.inclusion_date.year) * 12 + (
            end.month - self.inclusion_date.month
        )
        return max(months, 0)

    @property
    def is_overdue_for_review(self) -> bool:
        return (
            self.is_active
            and self.review_date is not None
            and self.review_date < timezone.localdate()
        )

    def can_transition_to(self, new_status: str) -> bool:
        """Проверяет допустимость перехода по таблице переходов."""
        return new_status in STATUS_TRANSITIONS.get(self.status, frozenset())

    @transaction.atomic
    def transition_to(
        self, new_status: str, *, user=None, reason: str = ""
    ) -> ReserveStatusHistory:
        """Переводит запись в новый статус с записью в историю."""
        if not self.can_transition_to(new_status):
            raise ValueError(
                f"Переход из состояния «{self.get_status_display()}» в «{new_status}» недопустим"
            )

        previous = self.status
        self.status = new_status
        updates = ["status", "updated_at"]

        if new_status in CLOSING_RESERVE_STATUSES:
            self.exclusion_date = timezone.localdate()
            updates.append("exclusion_date")
            if reason and new_status == ReserveStatus.EXCLUDED:
                self.exclusion_reason = reason
                updates.append("exclusion_reason")

        if user is not None:
            self.updated_by = user
            updates.append("updated_by")

        self.save(update_fields=updates)

        return ReserveStatusHistory.objects.create(
            entry=self,
            old_status=previous,
            new_status=new_status,
            changed_by=user,
            reason=reason,
            readiness_index=self.readiness_index,
        )

    def apply_readiness(self, index_value: float) -> None:
        """Записывает результат расчёта индекса готовности."""
        options = get_reservix_settings()
        thresholds = (
            options["READINESS_THRESHOLD_HIGH"],
            options["READINESS_THRESHOLD_MEDIUM"],
        )
        self.readiness_index = Decimal(str(round(index_value, 2)))
        self.readiness_level = ReadinessLevel.from_index(index_value, thresholds)
        self.readiness_calculated_at = timezone.now()
        self.save(
            update_fields=[
                "readiness_index",
                "readiness_level",
                "readiness_calculated_at",
                "updated_at",
            ]
        )


class ReserveStatusHistory(models.Model):
    """История изменения статуса записи резерва."""

    entry = models.ForeignKey(
        ReserveEntry,
        verbose_name=_("запись резерва"),
        on_delete=models.CASCADE,
        related_name="status_history",
    )
    old_status = models.CharField(
        _("прежний статус"),
        max_length=20,
        choices=ReserveStatus.choices,
        blank=True,
    )
    new_status = models.CharField(_("новый статус"), max_length=20, choices=ReserveStatus.choices)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("автор изменения"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reserve_status_changes",
    )
    reason = models.TextField(_("основание"), blank=True)
    readiness_index = models.DecimalField(
        _("индекс готовности на момент перехода"),
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )
    changed_at = models.DateTimeField(_("дата изменения"), auto_now_add=True, db_index=True)
    legacy_id = models.PositiveIntegerField(
        _("идентификатор в исходной базе"),
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )

    class Meta:
        verbose_name = _("изменение статуса резерва")
        verbose_name_plural = _("история статусов резерва")
        ordering = ["-changed_at", "-id"]
        indexes = [
            models.Index(fields=["entry", "-changed_at"], name="reservehist_entry_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.entry_id}: {self.old_status or '—'} → {self.new_status}"


class NominationKind(models.TextChoices):
    """Кто инициировал выдвижение."""

    SELF = "self", _("Самовыдвижение")
    MANAGER = "manager", _("Выдвижение руководителем")
    HR = "hr", _("Выдвижение кадровой службой")


class NominationStatus(models.TextChoices):
    """Состояние заявки на выдвижение."""

    DRAFT = "draft", _("Черновик")
    SUBMITTED = "submitted", _("Подана")
    MANAGER_REVIEW = "manager_review", _("На согласовании у руководителя")
    HR_REVIEW = "hr_review", _("На рассмотрении кадровой службы")
    APPROVED = "approved", _("Одобрена")
    REJECTED = "rejected", _("Отклонена")
    WITHDRAWN = "withdrawn", _("Отозвана")


#: Состояния, в которых заявка ожидает решения. Используются для счётчика
#: задач в рабочем месте руководителя и HR-специалиста.
PENDING_NOMINATION_STATUSES = frozenset(
    {
        NominationStatus.SUBMITTED,
        NominationStatus.MANAGER_REVIEW,
        NominationStatus.HR_REVIEW,
    }
)


class ReserveNominationQuerySet(models.QuerySet):
    """Выборки заявок на выдвижение."""

    def pending(self) -> Self:
        return self.filter(status__in=PENDING_NOMINATION_STATUSES)

    def awaiting_manager(self) -> Self:
        return self.filter(status__in=[NominationStatus.SUBMITTED, NominationStatus.MANAGER_REVIEW])

    def awaiting_hr(self) -> Self:
        return self.filter(status=NominationStatus.HR_REVIEW)

    def for_employee(self, employee_id: int) -> Self:
        return self.filter(employee_id=employee_id)

    def with_related(self) -> Self:
        return self.select_related("employee", "target_position", "nominated_by")


class ReserveNomination(UUIDModel, TimeStampedModel):
    """Заявка на включение сотрудника в кадровый резерв."""

    employee = models.ForeignKey(
        "people.Employee",
        verbose_name=_("кандидат"),
        on_delete=models.CASCADE,
        related_name="nominations",
    )
    target_position = models.ForeignKey(
        "org.Position",
        verbose_name=_("целевая должность"),
        on_delete=models.PROTECT,
        related_name="nominations",
    )
    reserve_group = models.ForeignKey(
        ReserveGroup,
        verbose_name=_("группа резерва"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nominations",
    )
    kind = models.CharField(
        _("вид выдвижения"),
        max_length=20,
        choices=NominationKind.choices,
        default=NominationKind.MANAGER,
        db_index=True,
    )
    status = models.CharField(
        _("состояние"),
        max_length=20,
        choices=NominationStatus.choices,
        default=NominationStatus.DRAFT,
        db_index=True,
    )
    nominated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("инициатор"),
        on_delete=models.SET_NULL,
        null=True,
        related_name="submitted_nominations",
    )
    justification = models.TextField(
        _("обоснование"),
        help_text=_("Достижения и основания, подтверждающие готовность кандидата"),
    )
    achievements = models.TextField(
        _("достижения кандидата"),
        blank=True,
        help_text=_("Результаты, подтверждающие потенциал"),
    )
    submitted_at = models.DateTimeField(_("подана"), null=True, blank=True)

    # -- Решение руководителя ----------------------------------------------
    manager_reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("согласующий руководитель"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nominations_as_manager",
    )
    manager_decision_at = models.DateTimeField(_("решение руководителя"), null=True, blank=True)
    manager_comment = models.TextField(_("комментарий руководителя"), blank=True)

    # -- Решение кадровой службы -------------------------------------------
    hr_reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("согласующий от кадровой службы"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nominations_as_hr",
    )
    hr_decision_at = models.DateTimeField(_("решение кадровой службы"), null=True, blank=True)
    hr_comment = models.TextField(_("комментарий кадровой службы"), blank=True)
    rejection_reason = models.CharField(_("причина отказа"), max_length=255, blank=True)

    entry = models.OneToOneField(
        ReserveEntry,
        verbose_name=_("созданная запись резерва"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nomination",
        help_text=_("Заполняется при одобрении заявки"),
    )

    objects = ReserveNominationQuerySet.as_manager()

    class Meta:
        verbose_name = _("заявка на выдвижение")
        verbose_name_plural = _("заявки на выдвижение")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="nomination_status_idx"),
            models.Index(fields=["employee", "status"], name="nomination_employee_idx"),
        ]
        constraints = [
            # Две одновременно рассматриваемые заявки на одну должность —
            # ошибка ввода: решение по ним принимал бы один человек дважды.
            models.UniqueConstraint(
                fields=["employee", "target_position"],
                condition=Q(status__in=["draft", "submitted", "manager_review", "hr_review"]),
                name="nomination_unique_pending",
            ),
        ]

    def __str__(self) -> str:
        return str(
            _("Заявка: %(employee)s → %(position)s")
            % {"employee": self.employee.short_name, "position": self.target_position.name}
        )

    def get_absolute_url(self) -> str:
        return reverse("reserve:nomination_detail", kwargs={"public_id": self.public_id})

    @property
    def is_pending(self) -> bool:
        return self.status in PENDING_NOMINATION_STATUSES

    @property
    def is_editable(self) -> bool:
        return self.status == NominationStatus.DRAFT

    @property
    def waiting_days(self) -> int:
        """Сколько дней заявка ожидает решения."""
        if not self.submitted_at:
            return 0
        return (timezone.now() - self.submitted_at).days


class ReserveMembership(TimeStampedModel):
    """Участие резервиста в группе подготовки."""

    group = models.ForeignKey(
        ReserveGroup,
        verbose_name=_("группа резерва"),
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    employee = models.ForeignKey(
        "people.Employee",
        verbose_name=_("резервист"),
        on_delete=models.CASCADE,
        related_name="reserve_memberships",
    )
    entry = models.ForeignKey(
        ReserveEntry,
        verbose_name=_("запись резерва"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memberships",
    )
    joined_at = models.DateField(_("зачислен"), default=timezone.localdate)
    left_at = models.DateField(_("выбыл"), null=True, blank=True)
    leave_reason = models.CharField(_("причина выбытия"), max_length=255, blank=True)
    is_curator_approved = models.BooleanField(
        _("подтверждено куратором"),
        default=False,
        help_text=_("Куратор группы подтверждает участие в программе подготовки"),
    )

    class Meta:
        verbose_name = _("участие в группе резерва")
        verbose_name_plural = _("участие в группах резерва")
        ordering = ["-joined_at"]
        indexes = [
            models.Index(fields=["group", "left_at"], name="membership_group_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["group", "employee"],
                condition=Q(left_at__isnull=True),
                name="membership_unique_active",
            ),
            models.CheckConstraint(
                condition=Q(left_at__isnull=True) | Q(left_at__gte=models.F("joined_at")),
                name="membership_period_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.employee.short_name} — {self.group.name}"

    @property
    def is_current(self) -> bool:
        return self.left_at is None
