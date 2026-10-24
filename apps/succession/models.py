"""Планирование преемственности."""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.constants import ReadinessLevel
from core.models import AuthoredModel, TimeStampedModel, UUIDModel
from core.settings_types import get_reservix_settings


class CriticalityLevel(models.IntegerChoices):
    """Степень критичности должности для непрерывности деятельности."""

    LOW = 1, _("Низкая")
    MODERATE = 2, _("Умеренная")
    SIGNIFICANT = 3, _("Значительная")
    HIGH = 4, _("Высокая")
    CRITICAL = 5, _("Критическая")


class VacancyRisk(models.IntegerChoices):
    """Вероятность освобождения должности в горизонте планирования."""

    LOW = 1, _("Низкая")
    MEDIUM = 2, _("Средняя")
    HIGH = 3, _("Высокая")


class KeyPositionQuerySet(models.QuerySet):
    """Выборки критичных должностей."""

    def active(self) -> Self:
        return self.filter(is_active=True)

    def critical(self) -> Self:
        return self.filter(criticality__gte=CriticalityLevel.HIGH, is_active=True)

    def uncovered(self) -> Self:
        """Критичные должности без действующего плана преемственности."""
        return self.filter(is_active=True, plans__isnull=True)

    def with_related(self) -> Self:
        return self.select_related("position", "position__grade", "incumbent")


class KeyPosition(TimeStampedModel):
    """Критичная должность — объект планирования преемственности."""

    position = models.OneToOneField(
        "org.Position",
        verbose_name=_("должность"),
        on_delete=models.CASCADE,
        related_name="key_position",
    )
    incumbent = models.ForeignKey(
        "people.Employee",
        verbose_name=_("действующий сотрудник"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="held_key_positions",
        help_text=_("Пусто, если должность вакантна"),
    )
    criticality = models.PositiveSmallIntegerField(
        _("критичность"),
        choices=CriticalityLevel.choices,
        default=CriticalityLevel.SIGNIFICANT,
        db_index=True,
    )
    vacancy_risk = models.PositiveSmallIntegerField(
        _("риск освобождения"),
        choices=VacancyRisk.choices,
        default=VacancyRisk.LOW,
        db_index=True,
        help_text=_("Учитывает возраст, риск ухода и планы действующего сотрудника"),
    )
    impact_of_loss = models.TextField(
        _("последствия ухода"),
        blank=True,
        help_text=_("Что перестанет работать в организации при освобождении должности"),
    )
    justification = models.TextField(
        _("обоснование критичности"),
        blank=True,
        help_text=_("Почему должность отнесена к критичным"),
    )
    required_successors = models.PositiveSmallIntegerField(
        _("требуется преемников"),
        default=2,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text=_("Целевая глубина резерва на должность"),
    )
    vacancy_horizon_months = models.PositiveSmallIntegerField(
        _("горизонт освобождения, месяцев"),
        null=True,
        blank=True,
        help_text=_("Ожидаемый срок до освобождения: выход на пенсию, ротация, проект"),
    )
    is_active = models.BooleanField(_("учитывается в планировании"), default=True, db_index=True)
    reviewed_at = models.DateField(
        _("дата последнего пересмотра"),
        null=True,
        blank=True,
        help_text=_("Перечень критичных должностей пересматривается ежегодно"),
    )

    objects = KeyPositionQuerySet.as_manager()

    class Meta:
        verbose_name = _("критичная должность")
        verbose_name_plural = _("критичные должности")
        ordering = ["-criticality", "-vacancy_risk", "position__name"]
        indexes = [
            models.Index(fields=["-criticality", "-vacancy_risk"], name="keyposition_priority_idx"),
        ]

    def __str__(self) -> str:
        return self.position.name

    def get_absolute_url(self) -> str:
        return reverse("succession:key_position_detail", kwargs={"pk": self.pk})

    @property
    def priority_score(self) -> int:
        """Приоритет планирования: критичность, умноженная на риск освобождения."""
        return int(self.criticality) * int(self.vacancy_risk)

    @property
    def successor_count(self) -> int:
        """Число преемников в действующих планах."""
        return SuccessionSlate.objects.filter(
            plan__key_position=self, plan__status=PlanStatus.ACTIVE
        ).count()

    @property
    def ready_successor_count(self) -> int:
        """Преемники, готовые к назначению немедленно."""
        return SuccessionSlate.objects.filter(
            plan__key_position=self,
            plan__status=PlanStatus.ACTIVE,
            readiness_level=ReadinessLevel.HIGH,
        ).count()

    @property
    def coverage(self) -> float:
        """Обеспеченность преемниками, доля от требуемого числа."""
        if not self.required_successors:
            return 0.0
        return round(self.successor_count / self.required_successors, 2)

    @property
    def is_covered(self) -> bool:
        return self.successor_count >= self.required_successors


class PlanStatus(models.TextChoices):
    """Состояние плана преемственности."""

    DRAFT = "draft", _("Черновик")
    ACTIVE = "active", _("Действует")
    ON_REVIEW = "on_review", _("На пересмотре")
    CLOSED = "closed", _("Закрыт")
    CANCELLED = "cancelled", _("Отменён")


class SuccessionPlanQuerySet(models.QuerySet):
    """Выборки планов преемственности."""

    def active(self) -> Self:
        return self.filter(status=PlanStatus.ACTIVE)

    def due_for_review(self) -> Self:
        return self.filter(status=PlanStatus.ACTIVE, review_date__lte=timezone.localdate())

    def with_related(self) -> Self:
        return self.select_related("key_position", "key_position__position", "owner")


class SuccessionPlan(UUIDModel, AuthoredModel, TimeStampedModel):
    """План преемственности по критичной должности."""

    key_position = models.ForeignKey(
        KeyPosition,
        verbose_name=_("критичная должность"),
        on_delete=models.CASCADE,
        related_name="plans",
    )
    title = models.CharField(
        _("наименование плана"),
        max_length=255,
        help_text=_("Например: «Преемственность руководителя департамента разработки»"),
    )
    status = models.CharField(
        _("состояние"),
        max_length=20,
        choices=PlanStatus.choices,
        default=PlanStatus.DRAFT,
        db_index=True,
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("ответственный"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_succession_plans",
    )
    horizon_months = models.PositiveSmallIntegerField(
        _("горизонт планирования, месяцев"),
        default=36,
        help_text=_("Срок, на который планируется замещение"),
    )
    start_date = models.DateField(_("дата начала"), default=timezone.localdate)
    review_date = models.DateField(
        _("дата пересмотра"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Когда план подлежит очередному пересмотру"),
    )
    closed_at = models.DateField(_("дата закрытия"), null=True, blank=True)
    risks = models.TextField(
        _("риски исполнения плана"),
        blank=True,
        help_text=_("Что помешает подготовить преемников в срок"),
    )
    notes = models.TextField(_("примечания"), blank=True)

    objects = SuccessionPlanQuerySet.as_manager()

    class Meta:
        verbose_name = _("план преемственности")
        verbose_name_plural = _("планы преемственности")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "review_date"], name="succplan_status_idx"),
        ]
        constraints = [
            # Действующий план по должности может быть только один: два плана
            # означали бы два несогласованных списка преемников.
            models.UniqueConstraint(
                fields=["key_position"],
                condition=Q(status="active"),
                name="succplan_single_active",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("succession:plan_detail", kwargs={"public_id": self.public_id})

    def save(self, *args, **kwargs) -> None:
        """Проставляет горизонт планирования из настроек при создании плана."""
        if not self.pk and not self.horizon_months:
            self.horizon_months = get_reservix_settings()["SUCCESSION_HORIZON_MONTHS"]
        super().save(*args, **kwargs)

    @property
    def successor_count(self) -> int:
        return self.slate.count()

    @property
    def ready_now_count(self) -> int:
        return self.slate.filter(readiness_level=ReadinessLevel.HIGH).count()

    @property
    def is_sufficient(self) -> bool:
        """Достаточно ли преемников относительно требования критичной должности."""
        return self.successor_count >= self.key_position.required_successors

    @property
    def needs_review(self) -> bool:
        return (
            self.status == PlanStatus.ACTIVE
            and self.review_date is not None
            and self.review_date <= timezone.localdate()
        )


class SuccessionSlate(TimeStampedModel):
    """Преемник в плане: кандидат с рангом и горизонтом готовности."""

    plan = models.ForeignKey(
        SuccessionPlan,
        verbose_name=_("план преемственности"),
        on_delete=models.CASCADE,
        related_name="slate",
    )
    employee = models.ForeignKey(
        "people.Employee",
        verbose_name=_("преемник"),
        on_delete=models.CASCADE,
        related_name="succession_slates",
    )
    entry = models.ForeignKey(
        "reserve.ReserveEntry",
        verbose_name=_("запись резерва"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="succession_slates",
        help_text=_("Связь с резервом; пусто у внешнего кандидата"),
    )
    rank = models.PositiveSmallIntegerField(
        _("ранг"),
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(20)],
        help_text=_("Порядок рассмотрения кандидатов при замещении"),
    )
    readiness_level = models.CharField(
        _("готовность"),
        max_length=20,
        choices=ReadinessLevel.choices,
        default=ReadinessLevel.LOW,
        db_index=True,
    )
    readiness_index = models.DecimalField(
        _("индекс готовности"),
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Значение на момент включения в список преемников"),
    )
    is_emergency_successor = models.BooleanField(
        _("экстренная замена"),
        default=False,
        help_text=_("Кандидат, способный принять должность немедленно и без подготовки"),
    )
    development_focus = models.TextField(
        _("фокус подготовки"),
        blank=True,
        help_text=_("Что предстоит развить кандидату до готовности"),
    )
    added_on = models.DateField(_("включён в список"), default=timezone.localdate)
    removed_on = models.DateField(_("исключён из списка"), null=True, blank=True)
    removal_reason = models.CharField(_("причина исключения"), max_length=255, blank=True)

    class Meta:
        verbose_name = _("преемник")
        verbose_name_plural = _("списки преемников")
        ordering = ["plan", "rank"]
        indexes = [
            models.Index(fields=["plan", "rank"], name="slate_plan_rank_idx"),
            models.Index(fields=["employee"], name="slate_employee_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "employee"],
                name="slate_unique_employee_per_plan",
            ),
            models.UniqueConstraint(
                fields=["plan", "rank"],
                condition=Q(removed_on__isnull=True),
                name="slate_unique_rank_per_plan",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.rank}. {self.employee.short_name}"

    @property
    def is_active(self) -> bool:
        return self.removed_on is None


class AppointmentKind(models.TextChoices):
    """Источник назначения."""

    FROM_RESERVE = "from_reserve", _("Из кадрового резерва")
    INTERNAL = "internal", _("Внутреннее перемещение вне резерва")
    EXTERNAL = "external", _("Внешний наём")
    ACTING = "acting", _("Исполняющий обязанности")


class AppointmentQuerySet(models.QuerySet):
    """Выборки назначений."""

    def from_reserve(self) -> Self:
        return self.filter(kind=AppointmentKind.FROM_RESERVE)

    def in_year(self, year: int) -> Self:
        return self.filter(appointment_date__year=year)

    def on_key_positions(self) -> Self:
        return self.filter(position__is_key_position=True)

    def with_related(self) -> Self:
        return self.select_related("employee", "position", "plan")


class Appointment(UUIDModel, AuthoredModel, TimeStampedModel):
    """Факт назначения сотрудника на должность."""

    employee = models.ForeignKey(
        "people.Employee",
        verbose_name=_("назначенный"),
        on_delete=models.PROTECT,
        related_name="appointments",
    )
    position = models.ForeignKey(
        "org.Position",
        verbose_name=_("должность"),
        on_delete=models.PROTECT,
        related_name="appointments",
    )
    plan = models.ForeignKey(
        SuccessionPlan,
        verbose_name=_("план преемственности"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
    )
    entry = models.ForeignKey(
        "reserve.ReserveEntry",
        verbose_name=_("запись резерва"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
    )
    predecessor = models.ForeignKey(
        "people.Employee",
        verbose_name=_("предшественник"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="succeeded_appointments",
    )
    kind = models.CharField(
        _("источник назначения"),
        max_length=20,
        choices=AppointmentKind.choices,
        default=AppointmentKind.FROM_RESERVE,
        db_index=True,
    )
    appointment_date = models.DateField(_("дата назначения"), db_index=True)
    order_number = models.CharField(
        _("номер приказа"),
        max_length=50,
        blank=True,
        help_text=_("Реквизит кадрового документа"),
    )
    probation_until = models.DateField(
        _("испытательный срок до"),
        null=True,
        blank=True,
        help_text=_("Для назначений с проверкой соответствия должности"),
    )
    readiness_index_at_appointment = models.DecimalField(
        _("индекс готовности при назначении"),
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Фиксируется для последующей проверки прогностической силы индекса"),
    )
    months_in_reserve = models.PositiveSmallIntegerField(
        _("месяцев в резерве"),
        null=True,
        blank=True,
        help_text=_("Срок подготовки от включения в резерв до назначения"),
    )
    comment = models.TextField(_("комментарий"), blank=True)

    objects = AppointmentQuerySet.as_manager()

    class Meta:
        verbose_name = _("назначение")
        verbose_name_plural = _("назначения")
        ordering = ["-appointment_date"]
        indexes = [
            models.Index(fields=["-appointment_date", "kind"], name="appointment_date_kind_idx"),
            models.Index(fields=["employee", "-appointment_date"], name="appointment_empl_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.employee.short_name} → {self.position.name} ({self.appointment_date})"

    def get_absolute_url(self) -> str:
        return reverse("succession:appointment_detail", kwargs={"public_id": self.public_id})

    @property
    def is_from_reserve(self) -> bool:
        return self.kind == AppointmentKind.FROM_RESERVE

    @property
    def preparation_years(self) -> Decimal | None:
        """Срок подготовки в годах — форма, удобная для отчётности."""
        if self.months_in_reserve is None:
            return None
        return Decimal(self.months_in_reserve) / Decimal("12")
