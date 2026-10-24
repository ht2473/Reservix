"""Развитие резервистов."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Self

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import (
    ActivatableModel,
    AuthoredModel,
    DescribedModel,
    TimeStampedModel,
    UUIDModel,
)
from core.settings_types import get_reservix_settings
from core.utils import section_url
from core.validators import FileExtensionValidator, FileSizeValidator, code_validator


class PlanStatus(models.TextChoices):
    """Состояние индивидуального плана развития."""

    DRAFT = "draft", _("Черновик")
    ON_APPROVAL = "on_approval", _("На утверждении")
    ACTIVE = "active", _("Выполняется")
    COMPLETED = "completed", _("Выполнен")
    CANCELLED = "cancelled", _("Отменён")


#: Состояния, в которых план считается работающим.
OPEN_PLAN_STATUSES = frozenset({PlanStatus.ON_APPROVAL, PlanStatus.ACTIVE})


class DevelopmentPlanQuerySet(models.QuerySet):
    """Выборки индивидуальных планов развития."""

    def active(self) -> Self:
        return self.filter(status=PlanStatus.ACTIVE)

    def open(self) -> Self:
        return self.filter(status__in=OPEN_PLAN_STATUSES)

    def for_employee(self, employee_id: int) -> Self:
        return self.filter(employee_id=employee_id)

    def expiring(self, days: int = 30) -> Self:
        """Планы, срок которых истекает в ближайшие дни."""
        horizon = timezone.localdate() + dt.timedelta(days=days)
        return self.filter(status=PlanStatus.ACTIVE, end_date__lte=horizon)

    def with_related(self) -> Self:
        return self.select_related("employee", "entry", "entry__target_position", "supervisor")


class DevelopmentPlan(UUIDModel, AuthoredModel, TimeStampedModel):
    """Индивидуальный план развития резервиста."""

    employee = models.ForeignKey(
        "people.Employee",
        verbose_name=_("сотрудник"),
        on_delete=models.CASCADE,
        related_name="development_plans",
    )
    entry = models.ForeignKey(
        "reserve.ReserveEntry",
        verbose_name=_("запись резерва"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="development_plans",
        help_text=_("Целевая должность, к которой готовится резервист"),
    )
    title = models.CharField(_("наименование плана"), max_length=255)
    goal = models.TextField(
        _("цель плана"),
        blank=True,
        help_text=_("Какого состояния готовности требуется достичь"),
    )
    status = models.CharField(
        _("состояние"),
        max_length=20,
        choices=PlanStatus.choices,
        default=PlanStatus.DRAFT,
        db_index=True,
    )
    start_date = models.DateField(_("дата начала"))
    end_date = models.DateField(_("дата окончания"))
    supervisor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("куратор плана"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supervised_plans",
        help_text=_("Руководитель, отвечающий за выполнение плана"),
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("утвердил"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_plans",
    )
    approved_at = models.DateTimeField(_("дата утверждения"), null=True, blank=True)
    completion_rate = models.PositiveSmallIntegerField(
        _("выполнение, процентов"),
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text=_("Пересчитывается при изменении состояния мероприятий"),
    )
    summary = models.TextField(
        _("итоги выполнения"),
        blank=True,
        help_text=_("Заполняется при завершении плана"),
    )
    legacy_id = models.PositiveIntegerField(
        _("идентификатор в исходной базе"),
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )

    objects = DevelopmentPlanQuerySet.as_manager()

    class Meta:
        verbose_name = _("план развития")
        verbose_name_plural = _("индивидуальные планы развития")
        ordering = ["-start_date"]
        indexes = [
            models.Index(fields=["employee", "status"], name="devplan_employee_idx"),
            models.Index(fields=["status", "end_date"], name="devplan_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__gte=models.F("start_date")),
                name="devplan_period_valid",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return section_url("development:plan_detail", public_id=self.public_id)

    # -- Прогресс -----------------------------------------------------------
    @property
    def activity_count(self) -> int:
        return self.activities.count()

    @property
    def completed_count(self) -> int:
        return self.activities.filter(status=ActivityStatus.COMPLETED).count()

    @property
    def overdue_count(self) -> int:
        """Число просроченных мероприятий."""
        return self.activities.filter(
            due_date__lt=timezone.localdate(),
            status__in=[ActivityStatus.PENDING, ActivityStatus.IN_PROGRESS],
        ).count()

    def recalculate_completion(self) -> int:
        """Пересчитывает процент выполнения плана по мероприятиям."""
        activities = list(self.activities.all())
        if not activities:
            rate = 0
        else:
            total_weight = sum(activity.weight for activity in activities)
            done_weight = sum(
                activity.weight
                for activity in activities
                if activity.status == ActivityStatus.COMPLETED
            )
            rate = round(done_weight * 100 / total_weight) if total_weight else 0

        if rate != self.completion_rate:
            self.completion_rate = rate
            self.save(update_fields=["completion_rate", "updated_at"])
        return rate

    @property
    def days_left(self) -> int:
        return (self.end_date - timezone.localdate()).days

    @property
    def is_overdue(self) -> bool:
        return self.status == PlanStatus.ACTIVE and self.end_date < timezone.localdate()


class ActivityType(models.TextChoices):
    """Вид мероприятия развития."""

    TRAINING = "training", _("Обучение")
    MENTORING = "mentoring", _("Наставничество")
    TASK = "task", _("Развивающая задача")
    COURSE = "course", _("Курс")
    CONFERENCE = "conference", _("Конференция")
    ROTATION = "rotation", _("Ротация")
    INTERNSHIP = "internship", _("Стажировка")
    PROJECT = "project", _("Проектная работа")
    SELF_STUDY = "self_study", _("Самостоятельное изучение")


#: Виды мероприятий, относимые к развитию на рабочем месте (доля «70»).
ON_THE_JOB_TYPES = frozenset(
    {
        ActivityType.TASK,
        ActivityType.ROTATION,
        ActivityType.INTERNSHIP,
        ActivityType.PROJECT,
    }
)


class ActivityStatus(models.TextChoices):
    """Состояние мероприятия. Значения перенесены из исходной схемы."""

    PENDING = "pending", _("Запланировано")
    IN_PROGRESS = "in_progress", _("Выполняется")
    COMPLETED = "completed", _("Выполнено")
    OVERDUE = "overdue", _("Просрочено")
    CANCELLED = "cancelled", _("Отменено")


class DevelopmentActivityQuerySet(models.QuerySet):
    """Выборки мероприятий развития."""

    def open(self) -> Self:
        return self.filter(status__in=[ActivityStatus.PENDING, ActivityStatus.IN_PROGRESS])

    def completed(self) -> Self:
        return self.filter(status=ActivityStatus.COMPLETED)

    def overdue(self, on_date: dt.date | None = None) -> Self:
        moment = on_date or timezone.localdate()
        return self.filter(
            due_date__lt=moment,
            status__in=[ActivityStatus.PENDING, ActivityStatus.IN_PROGRESS],
        )

    def due_soon(self, days: int | None = None) -> Self:
        """Мероприятия с приближающимся сроком — основание для напоминания."""
        window = days if days is not None else get_reservix_settings()["ACTIVITY_DUE_SOON_DAYS"]
        today = timezone.localdate()
        return self.filter(
            due_date__gte=today,
            due_date__lte=today + dt.timedelta(days=window),
            status__in=[ActivityStatus.PENDING, ActivityStatus.IN_PROGRESS],
        )

    def with_related(self) -> Self:
        return self.select_related("plan", "plan__employee", "mentor", "competency")


class DevelopmentActivity(UUIDModel, TimeStampedModel):
    """Мероприятие индивидуального плана развития."""

    plan = models.ForeignKey(
        DevelopmentPlan,
        verbose_name=_("план развития"),
        on_delete=models.CASCADE,
        related_name="activities",
    )
    title = models.CharField(_("наименование"), max_length=255)
    activity_type = models.CharField(
        _("вид мероприятия"),
        max_length=20,
        choices=ActivityType.choices,
        default=ActivityType.TRAINING,
        db_index=True,
    )
    description = models.TextField(_("описание"), blank=True)
    competency = models.ForeignKey(
        "competencies.Competency",
        verbose_name=_("развиваемая компетенция"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="development_activities",
        help_text=_("Компетенция, разрыв по которой закрывает мероприятие"),
    )
    resource = models.ForeignKey(
        "development.LearningResource",
        verbose_name=_("учебный ресурс"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activities",
    )
    status = models.CharField(
        _("состояние"),
        max_length=20,
        choices=ActivityStatus.choices,
        default=ActivityStatus.PENDING,
        db_index=True,
    )
    start_date = models.DateField(_("дата начала"), null=True, blank=True)
    due_date = models.DateField(_("срок выполнения"), db_index=True)
    completion_date = models.DateField(_("дата выполнения"), null=True, blank=True)
    responsible = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("ответственный"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="responsible_activities",
    )
    mentor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("наставник"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mentored_activities",
    )
    weight = models.PositiveSmallIntegerField(
        _("вес мероприятия"),
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text=_("Вклад мероприятия в выполнение плана"),
    )
    planned_hours = models.PositiveSmallIntegerField(
        _("плановая трудоёмкость, часов"),
        null=True,
        blank=True,
    )
    cost = models.DecimalField(
        _("стоимость, рублей"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Бюджет мероприятия; суммируется в отчёте по затратам на резерв"),
    )
    result = models.TextField(
        _("результат"),
        blank=True,
        help_text=_("Что освоено и как применено в работе"),
    )
    legacy_id = models.PositiveIntegerField(
        _("идентификатор в исходной базе"),
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )

    objects = DevelopmentActivityQuerySet.as_manager()

    class Meta:
        verbose_name = _("мероприятие развития")
        verbose_name_plural = _("мероприятия развития")
        ordering = ["due_date", "id"]
        indexes = [
            models.Index(fields=["plan", "status"], name="activity_plan_status_idx"),
            models.Index(fields=["due_date", "status"], name="activity_due_idx"),
            models.Index(fields=["mentor"], name="activity_mentor_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(Q(start_date__isnull=True) | Q(due_date__gte=models.F("start_date"))),
                name="activity_period_valid",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status="completed") | Q(status="completed", completion_date__isnull=False)
                ),
                name="activity_completed_requires_date",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return section_url("development:activity_detail", public_id=self.public_id)

    # -- Состояние ----------------------------------------------------------
    @property
    def is_overdue(self) -> bool:
        return (
            self.status in {ActivityStatus.PENDING, ActivityStatus.IN_PROGRESS}
            and self.due_date < timezone.localdate()
        )

    @property
    def days_left(self) -> int:
        return (self.due_date - timezone.localdate()).days

    @property
    def is_on_the_job(self) -> bool:
        """Относится ли мероприятие к развитию на рабочем месте."""
        return self.activity_type in ON_THE_JOB_TYPES

    @property
    def has_evidence(self) -> bool:
        return self.evidences.exists()

    def complete(self, *, on_date: dt.date | None = None, result: str = "") -> None:
        """Отмечает мероприятие выполненным и пересчитывает прогресс плана."""
        self.status = ActivityStatus.COMPLETED
        self.completion_date = on_date or timezone.localdate()
        if result:
            self.result = result
        self.save(update_fields=["status", "completion_date", "result", "updated_at"])
        self.plan.recalculate_completion()


class EvidenceKind(models.TextChoices):
    """Вид подтверждения выполнения мероприятия."""

    CERTIFICATE = "certificate", _("Сертификат")
    REPORT = "report", _("Отчёт")
    FEEDBACK = "feedback", _("Отзыв наставника")
    ARTIFACT = "artifact", _("Рабочий результат")
    LINK = "link", _("Внешняя ссылка")


class ActivityEvidence(TimeStampedModel):
    """Подтверждение выполнения мероприятия развития."""

    activity = models.ForeignKey(
        DevelopmentActivity,
        verbose_name=_("мероприятие"),
        on_delete=models.CASCADE,
        related_name="evidences",
    )
    kind = models.CharField(
        _("вид подтверждения"),
        max_length=20,
        choices=EvidenceKind.choices,
        default=EvidenceKind.CERTIFICATE,
    )
    title = models.CharField(_("наименование"), max_length=255)
    file = models.FileField(
        _("файл"),
        upload_to="development/evidence/%Y/%m/",
        null=True,
        blank=True,
        validators=[
            FileSizeValidator(10 * 1024 * 1024),
            FileExtensionValidator([".pdf", ".jpg", ".jpeg", ".png", ".docx", ".xlsx"]),
        ],
    )
    external_url = models.URLField(
        _("внешняя ссылка"),
        blank=True,
        help_text=_("Ссылка на публичный сертификат или репозиторий"),
    )
    comment = models.TextField(_("комментарий"), blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("приложил"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_evidences",
    )
    is_verified = models.BooleanField(
        _("проверено"),
        default=False,
        help_text=_("Руководитель или наставник подтвердил достоверность"),
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("проверил"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_evidences",
    )
    verified_at = models.DateTimeField(_("дата проверки"), null=True, blank=True)

    class Meta:
        verbose_name = _("подтверждение выполнения")
        verbose_name_plural = _("подтверждения выполнения")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["activity", "is_verified"], name="evidence_activity_idx"),
        ]

    def __str__(self) -> str:
        return self.title


class MentorshipStatus(models.TextChoices):
    """Состояние наставнической пары."""

    PROPOSED = "proposed", _("Предложена")
    ACTIVE = "active", _("Действует")
    PAUSED = "paused", _("Приостановлена")
    COMPLETED = "completed", _("Завершена")
    TERMINATED = "terminated", _("Прекращена досрочно")


class MentorshipQuerySet(models.QuerySet):
    """Выборки наставнических пар."""

    def active(self) -> Self:
        return self.filter(status=MentorshipStatus.ACTIVE)

    def for_mentor(self, user_id: int) -> Self:
        return self.filter(mentor_id=user_id)

    def with_related(self) -> Self:
        return self.select_related("mentor", "mentee", "plan")


class Mentorship(UUIDModel, TimeStampedModel):
    """Наставническая пара: наставник и подопечный."""

    mentor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("наставник"),
        on_delete=models.PROTECT,
        related_name="mentorships_as_mentor",
    )
    mentee = models.ForeignKey(
        "people.Employee",
        verbose_name=_("подопечный"),
        on_delete=models.CASCADE,
        related_name="mentorships",
    )
    plan = models.ForeignKey(
        DevelopmentPlan,
        verbose_name=_("план развития"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mentorships",
    )
    status = models.CharField(
        _("состояние"),
        max_length=20,
        choices=MentorshipStatus.choices,
        default=MentorshipStatus.PROPOSED,
        db_index=True,
    )
    goal = models.TextField(
        _("цель наставничества"),
        help_text=_("Какого результата должна достичь пара"),
    )
    start_date = models.DateField(_("дата начала"), default=timezone.localdate)
    end_date = models.DateField(_("плановое окончание"), null=True, blank=True)
    actual_end_date = models.DateField(_("фактическое окончание"), null=True, blank=True)
    meeting_frequency_days = models.PositiveSmallIntegerField(
        _("периодичность встреч, дней"),
        default=14,
        help_text=_("Используется для напоминаний о встрече"),
    )
    last_meeting_at = models.DateField(_("последняя встреча"), null=True, blank=True)
    meetings_count = models.PositiveSmallIntegerField(_("проведено встреч"), default=0)
    result = models.TextField(_("результат"), blank=True)
    mentee_feedback = models.TextField(
        _("отзыв подопечного"),
        blank=True,
        help_text=_("Оценка полезности наставничества со стороны подопечного"),
    )
    effectiveness = models.PositiveSmallIntegerField(
        _("результативность, баллов"),
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text=_("Оценка кадровой службы по итогам пары"),
    )

    objects = MentorshipQuerySet.as_manager()

    class Meta:
        verbose_name = _("наставничество")
        verbose_name_plural = _("наставничество")
        ordering = ["-start_date"]
        indexes = [
            models.Index(fields=["mentor", "status"], name="mentorship_mentor_idx"),
            models.Index(fields=["mentee", "status"], name="mentorship_mentee_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["mentor", "mentee"],
                condition=Q(status__in=["proposed", "active", "paused"]),
                name="mentorship_unique_active_pair",
            ),
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=models.F("start_date")),
                name="mentorship_period_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.mentor.short_name} → {self.mentee.short_name}"

    def get_absolute_url(self) -> str:
        return section_url("development:mentorship_detail", public_id=self.public_id)

    @property
    def is_meeting_due(self) -> bool:
        """Наступил ли срок очередной встречи наставника с подопечным."""
        if self.status != MentorshipStatus.ACTIVE:
            return False
        if self.last_meeting_at is None:
            return True
        due = self.last_meeting_at + dt.timedelta(days=self.meeting_frequency_days)
        return due <= timezone.localdate()

    @property
    def duration_days(self) -> int:
        end = self.actual_end_date or self.end_date or timezone.localdate()
        return max((end - self.start_date).days, 0)


class ResourceFormat(models.TextChoices):
    """Форма подачи учебного материала."""

    COURSE = "course", _("Онлайн-курс")
    BOOK = "book", _("Книга")
    ARTICLE = "article", _("Статья")
    VIDEO = "video", _("Видеокурс")
    PROGRAM = "program", _("Программа обучения")
    WORKSHOP = "workshop", _("Практикум")


class LearningResource(DescribedModel, ActivatableModel, TimeStampedModel):
    """Учебный ресурс из библиотеки развития."""

    code = models.SlugField(_("код"), max_length=48, unique=True, validators=[code_validator])
    resource_format = models.CharField(
        _("форма"),
        max_length=20,
        choices=ResourceFormat.choices,
        default=ResourceFormat.COURSE,
        db_index=True,
    )
    competencies = models.ManyToManyField(
        "competencies.Competency",
        verbose_name=_("развиваемые компетенции"),
        related_name="learning_resources",
        blank=True,
    )
    provider = models.CharField(
        _("поставщик"),
        max_length=255,
        blank=True,
        help_text=_("Учебный центр, платформа или внутреннее подразделение"),
    )
    url = models.URLField(_("ссылка"), blank=True)
    duration_hours = models.PositiveSmallIntegerField(
        _("длительность, часов"), null=True, blank=True
    )
    cost = models.DecimalField(
        _("стоимость, рублей"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    target_level_min = models.PositiveSmallIntegerField(
        _("подходит с уровня"),
        null=True,
        blank=True,
        help_text=_("Минимальный уровень владения, при котором материал полезен"),
    )
    target_level_max = models.PositiveSmallIntegerField(
        _("подходит до уровня"),
        null=True,
        blank=True,
    )
    rating = models.DecimalField(
        _("средняя оценка"),
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("1")), MaxValueValidator(Decimal("5"))],
        help_text=_("Формируется по отзывам прошедших обучение"),
    )
    is_internal = models.BooleanField(
        _("внутренний ресурс"),
        default=False,
        help_text=_("Материал, разработанный внутри организации"),
    )

    class Meta:
        verbose_name = _("учебный ресурс")
        verbose_name_plural = _("библиотека развития")
        ordering = ["name"]
        indexes = [
            models.Index(fields=["resource_format", "is_active"], name="resource_format_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return section_url("development:resource_detail", pk=self.pk)
