"""Оценка компетенций."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Self

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Avg, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import (
    ActivatableModel,
    AuthoredModel,
    DescribedModel,
    OrderedModel,
    TimeStampedModel,
    UUIDModel,
)
from core.settings_types import get_reservix_settings
from core.utils import generate_token
from core.validators import code_validator, validate_competency_level

#: Веса категорий респондентов при сведении оценки 360°.
RESPONDENT_WEIGHTS: dict[str, Decimal] = {
    "manager": Decimal("0.40"),
    "peer": Decimal("0.25"),
    "subordinate": Decimal("0.20"),
    "external": Decimal("0.10"),
    "self": Decimal("0.05"),
}


class AssessmentMethod(models.TextChoices):
    """Метод получения оценки."""

    TEST = "test", _("Тестирование")
    INTERVIEW = "interview", _("Интервью по компетенциям")
    ASSESSMENT_360 = "360", _("Оценка 360 градусов")
    EXAM = "exam", _("Экзамен")
    SELF = "self", _("Самооценка")
    MANAGER = "manager", _("Оценка руководителя")
    EXPERT = "expert", _("Экспертная оценка")
    ASSESSMENT_CENTER = "center", _("Центр оценки")


#: Методы, при которых балл вычисляется системой, а не вводится человеком.
COMPUTED_METHODS = frozenset({AssessmentMethod.TEST, AssessmentMethod.EXAM})


class CampaignStatus(models.TextChoices):
    """Состояние кампании оценки."""

    DRAFT = "draft", _("Черновик")
    ACTIVE = "active", _("Идёт")
    REVIEW = "review", _("Сведение результатов")
    CLOSED = "closed", _("Завершена")
    CANCELLED = "cancelled", _("Отменена")


class AssessmentCampaignQuerySet(models.QuerySet):
    """Выборки кампаний оценки."""

    def active(self) -> Self:
        return self.filter(status=CampaignStatus.ACTIVE)

    def open_for_input(self) -> Self:
        """Кампании, в которые ещё принимаются оценки."""
        return self.filter(status=CampaignStatus.ACTIVE, deadline__gte=timezone.localdate())

    def overdue(self) -> Self:
        """Идущие кампании с истёкшим сроком — требуют решения организатора."""
        return self.filter(status=CampaignStatus.ACTIVE, deadline__lt=timezone.localdate())

    def for_year(self, year: int) -> Self:
        return self.filter(period_start__year=year)


class AssessmentCampaign(UUIDModel, DescribedModel, AuthoredModel, TimeStampedModel):
    """Кампания оценки — организационная рамка процедуры."""

    code = models.SlugField(_("код"), max_length=48, unique=True, validators=[code_validator])
    competency_model = models.ForeignKey(
        "competencies.CompetencyModel",
        verbose_name=_("модель компетенций"),
        on_delete=models.PROTECT,
        related_name="campaigns",
        help_text=_("Эталон, относительно которого оцениваются участники"),
    )
    status = models.CharField(
        _("состояние"),
        max_length=20,
        choices=CampaignStatus.choices,
        default=CampaignStatus.DRAFT,
        db_index=True,
    )
    period_start = models.DateField(_("начало периода"))
    period_end = models.DateField(_("окончание периода"))
    deadline = models.DateField(
        _("срок внесения оценок"),
        help_text=_("После этой даты сессии закрываются для редактирования"),
    )
    methods = models.JSONField(
        _("методы оценки"),
        default=list,
        blank=True,
        help_text=_("Перечень методов, применяемых в кампании"),
    )
    branch = models.ForeignKey(
        "org.Branch",
        verbose_name=_("филиал"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assessment_campaigns",
        help_text=_("Пусто — кампания охватывает всю организацию"),
    )
    department = models.ForeignKey(
        "org.Department",
        verbose_name=_("подразделение"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assessment_campaigns",
        help_text=_("Ограничивает состав участников подразделением и вложенными в него"),
    )
    is_self_assessment_required = models.BooleanField(
        _("требуется самооценка"),
        default=True,
        help_text=_("Расхождение самооценки и оценки руководителя — предмет обсуждения"),
    )
    is_360_enabled = models.BooleanField(
        _("включена оценка 360°"),
        default=False,
        help_text=_("Добавляет к кампании опрос коллег и подчинённых"),
    )
    is_anonymous_360 = models.BooleanField(
        _("анонимная оценка 360°"),
        default=True,
        help_text=_("Респонденты скрыты от оцениваемого; агрегат виден"),
    )
    instructions = models.TextField(
        _("инструкция участникам"),
        blank=True,
        help_text=_("Текст выводится оценивающему перед заполнением формы"),
    )
    closed_at = models.DateTimeField(_("завершена"), null=True, blank=True)

    objects = AssessmentCampaignQuerySet.as_manager()

    class Meta:
        verbose_name = _("кампания оценки")
        verbose_name_plural = _("кампании оценки")
        ordering = ["-period_start", "name"]
        indexes = [
            models.Index(fields=["status", "-period_start"], name="campaign_status_idx"),
            models.Index(fields=["deadline"], name="campaign_deadline_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(period_end__gte=models.F("period_start")),
                name="campaign_period_valid",
            ),
            models.CheckConstraint(
                condition=Q(deadline__gte=models.F("period_start")),
                name="campaign_deadline_after_start",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("assessment:campaign_detail", kwargs={"public_id": self.public_id})

    @property
    def is_open(self) -> bool:
        """Принимает ли кампания оценки в данный момент."""
        return self.status == CampaignStatus.ACTIVE and self.deadline >= timezone.localdate()

    @property
    def days_left(self) -> int:
        """Дней до окончания приёма оценок; отрицательное значение — просрочка."""
        return (self.deadline - timezone.localdate()).days

    @property
    def completion_rate(self) -> float:
        """Доля завершённых сессий, проценты."""
        total = self.sessions.count()
        if not total:
            return 0.0
        done = self.sessions.filter(
            status__in=[SessionStatus.SUBMITTED, SessionStatus.APPROVED]
        ).count()
        return round(done * 100 / total, 1)


class SessionStatus(models.TextChoices):
    """Состояние сессии оценки."""

    PLANNED = "planned", _("Запланирована")
    IN_PROGRESS = "in_progress", _("Заполняется")
    SUBMITTED = "submitted", _("Внесена")
    APPROVED = "approved", _("Утверждена")
    REJECTED = "rejected", _("Отклонена")
    CANCELLED = "cancelled", _("Отменена")


#: Состояния, при которых оценки сессии участвуют в расчётах.
COUNTED_SESSION_STATUSES = frozenset({SessionStatus.SUBMITTED, SessionStatus.APPROVED})


class AssessmentSessionQuerySet(models.QuerySet):
    """Выборки сессий оценки."""

    def counted(self) -> Self:
        """Сессии, результаты которых учитываются в расчётах."""
        return self.filter(status__in=COUNTED_SESSION_STATUSES)

    def pending(self) -> Self:
        """Незавершённые сессии — материал для напоминаний."""
        return self.filter(status__in=[SessionStatus.PLANNED, SessionStatus.IN_PROGRESS])

    def by_assessor(self, user_id: int) -> Self:
        return self.filter(assessor_id=user_id)

    def for_employee(self, employee_id: int) -> Self:
        return self.filter(employee_id=employee_id)

    def with_related(self) -> Self:
        return self.select_related("campaign", "employee", "assessor")


class AssessmentSession(UUIDModel, TimeStampedModel):
    """Сессия оценки — один акт оценивания одним человеком."""

    campaign = models.ForeignKey(
        AssessmentCampaign,
        verbose_name=_("кампания"),
        on_delete=models.CASCADE,
        related_name="sessions",
        null=True,
        blank=True,
        help_text=_("Пусто у внеплановой оценки — например, при выдвижении в резерв"),
    )
    employee = models.ForeignKey(
        "people.Employee",
        verbose_name=_("оцениваемый"),
        on_delete=models.CASCADE,
        related_name="assessment_sessions",
    )
    assessor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("оценивающий"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conducted_sessions",
        help_text=_("Пусто у автоматической оценки по результатам теста"),
    )
    competency_model = models.ForeignKey(
        "competencies.CompetencyModel",
        verbose_name=_("модель компетенций"),
        on_delete=models.PROTECT,
        related_name="sessions",
    )
    target_position = models.ForeignKey(
        "org.Position",
        verbose_name=_("оцениваемая должность"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assessment_sessions",
        help_text=_("Должность, на соответствие которой проводится оценка"),
    )
    method = models.CharField(
        _("метод"),
        max_length=20,
        choices=AssessmentMethod.choices,
        default=AssessmentMethod.MANAGER,
        db_index=True,
    )
    status = models.CharField(
        _("состояние"),
        max_length=20,
        choices=SessionStatus.choices,
        default=SessionStatus.PLANNED,
        db_index=True,
    )
    assessment_date = models.DateField(
        _("дата оценки"),
        default=timezone.localdate,
        db_index=True,
        help_text=_("Дата, к которой относится результат"),
    )
    started_at = models.DateTimeField(_("начата"), null=True, blank=True)
    submitted_at = models.DateTimeField(_("внесена"), null=True, blank=True)
    approved_at = models.DateTimeField(_("утверждена"), null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("утвердил"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_sessions",
    )
    summary = models.TextField(
        _("общий комментарий"),
        blank=True,
        help_text=_("Выводы оценивающего: сильные стороны и зоны развития"),
    )
    rejection_reason = models.CharField(_("причина отклонения"), max_length=255, blank=True)

    objects = AssessmentSessionQuerySet.as_manager()

    class Meta:
        verbose_name = _("сессия оценки")
        verbose_name_plural = _("сессии оценки")
        ordering = ["-assessment_date", "-id"]
        indexes = [
            models.Index(fields=["employee", "-assessment_date"], name="session_employee_idx"),
            models.Index(fields=["campaign", "status"], name="session_campaign_status_idx"),
            models.Index(fields=["method", "status"], name="session_method_idx"),
        ]
        constraints = [
            # Внутри кампании один оценивающий оценивает сотрудника один раз
            # выбранным методом: повторная сессия означала бы двойной учёт.
            models.UniqueConstraint(
                fields=["campaign", "employee", "assessor", "method"],
                condition=Q(campaign__isnull=False),
                name="session_unique_in_campaign",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.employee.short_name} — {self.get_method_display()} ({self.assessment_date})"

    def get_absolute_url(self) -> str:
        return reverse("assessment:session_detail", kwargs={"public_id": self.public_id})

    # -- Жизненный цикл -----------------------------------------------------
    @property
    def is_editable(self) -> bool:
        """Можно ли изменять баллы сессии."""
        return self.status in {SessionStatus.PLANNED, SessionStatus.IN_PROGRESS}

    @property
    def is_counted(self) -> bool:
        return self.status in COUNTED_SESSION_STATUSES

    @property
    def is_self_assessment(self) -> bool:
        return self.method == AssessmentMethod.SELF

    def submit(self) -> None:
        """Переводит сессию в состояние «внесена»."""
        self.status = SessionStatus.SUBMITTED
        self.submitted_at = timezone.now()
        self.save(update_fields=["status", "submitted_at", "updated_at"])

    def approve(self, approver) -> None:
        """Утверждает результаты сессии."""
        self.status = SessionStatus.APPROVED
        self.approved_at = timezone.now()
        self.approved_by = approver
        self.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])

    def reject(self, reason: str) -> None:
        """Возвращает сессию оценивающему с указанием причины."""
        self.status = SessionStatus.REJECTED
        self.rejection_reason = reason[:255]
        self.save(update_fields=["status", "rejection_reason", "updated_at"])

    # -- Результаты ---------------------------------------------------------
    @property
    def average_level(self) -> float | None:
        """Средний уровень по компетенциям сессии."""
        value = self.scores.aggregate(mean=Avg("level"))["mean"]
        return round(float(value), 2) if value is not None else None

    @property
    def scored_count(self) -> int:
        return self.scores.count()

    @property
    def is_complete(self) -> bool:
        """Заполнены ли все требования модели компетенций."""
        required = self.competency_model.requirements.count()
        return required > 0 and self.scored_count >= required


class AssessmentScoreQuerySet(models.QuerySet):
    """Выборки оценок по компетенциям."""

    def counted(self) -> Self:
        """Оценки из сессий, участвующих в расчётах."""
        return self.filter(session__status__in=COUNTED_SESSION_STATUSES)

    def for_competency(self, competency_id: int) -> Self:
        return self.filter(competency_id=competency_id)

    def in_period(self, start: dt.date, end: dt.date) -> Self:
        return self.filter(session__assessment_date__gte=start, session__assessment_date__lte=end)

    def with_related(self) -> Self:
        return self.select_related("competency", "session", "session__employee")


class AssessmentScore(TimeStampedModel):
    """Балл по одной компетенции внутри сессии."""

    session = models.ForeignKey(
        AssessmentSession,
        verbose_name=_("сессия"),
        on_delete=models.CASCADE,
        related_name="scores",
    )
    competency = models.ForeignKey(
        "competencies.Competency",
        verbose_name=_("компетенция"),
        on_delete=models.PROTECT,
        related_name="scores",
    )
    level = models.DecimalField(
        _("уровень"),
        max_digits=3,
        decimal_places=1,
        validators=[validate_competency_level],
        help_text=_("Уровень владения по шкале модели компетенций"),
    )
    raw_score = models.DecimalField(
        _("балл"),
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text=_("Значение в шкале 0–100; заполняется при переносе и тестировании"),
    )
    comment = models.TextField(
        _("комментарий"),
        blank=True,
        help_text=_("Обоснование: наблюдаемое поведение, подтверждающее уровень"),
    )
    evidence = models.TextField(
        _("подтверждение"),
        blank=True,
        help_text=_("Ссылка на проект, задачу или результат"),
    )
    legacy_id = models.PositiveIntegerField(
        _("идентификатор в исходной базе"),
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )

    objects = AssessmentScoreQuerySet.as_manager()

    class Meta:
        verbose_name = _("оценка компетенции")
        verbose_name_plural = _("оценки компетенций")
        ordering = ["competency__name"]
        indexes = [
            models.Index(fields=["session", "competency"], name="score_session_comp_idx"),
            models.Index(fields=["competency", "-created_at"], name="score_competency_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "competency"],
                name="score_unique_per_session",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.competency.name}: {self.level}"

    def save(self, *args, **kwargs) -> None:
        """Достраивает балл 0–100 по уровню, если он не задан явно."""
        if self.raw_score is None and self.level is not None:
            self.raw_score = self.level_to_score(float(self.level))
        super().save(*args, **kwargs)

    @staticmethod
    def level_to_score(level: float) -> Decimal:
        """Переводит уровень шкалы компетенций в балл 0–100."""
        options = get_reservix_settings()
        minimum = options["COMPETENCY_SCALE_MIN"]
        maximum = options["COMPETENCY_SCALE_MAX"]
        span = maximum - minimum
        if span <= 0:
            return Decimal("0.00")
        ratio = (level - minimum) / span
        return Decimal(str(round(max(0.0, min(1.0, ratio)) * 100, 2)))

    @staticmethod
    def score_to_level(score: float) -> Decimal:
        """Переводит балл 0–100 в уровень шкалы компетенций."""
        options = get_reservix_settings()
        minimum = options["COMPETENCY_SCALE_MIN"]
        maximum = options["COMPETENCY_SCALE_MAX"]
        ratio = max(0.0, min(100.0, score)) / 100
        return Decimal(str(round(minimum + ratio * (maximum - minimum), 1)))


class RespondentRelation(models.TextChoices):
    """Отношение респондента к оцениваемому в опросе 360°."""

    SELF = "self", _("Самооценка")
    MANAGER = "manager", _("Руководитель")
    PEER = "peer", _("Коллега")
    SUBORDINATE = "subordinate", _("Подчинённый")
    EXTERNAL = "external", _("Внешний респондент")


class RespondentStatus(models.TextChoices):
    """Состояние приглашения респондента."""

    INVITED = "invited", _("Приглашён")
    IN_PROGRESS = "in_progress", _("Заполняет")
    COMPLETED = "completed", _("Заполнил")
    DECLINED = "declined", _("Отказался")
    EXPIRED = "expired", _("Срок истёк")


class Assessment360Respondent(TimeStampedModel):
    """Участник опроса 360° по конкретному сотруднику."""

    #: Минимальное число ответов в категории, при котором агрегат раскрывается.
    MIN_RESPONSES_FOR_DISCLOSURE = 3

    campaign = models.ForeignKey(
        AssessmentCampaign,
        verbose_name=_("кампания"),
        on_delete=models.CASCADE,
        related_name="respondents",
    )
    employee = models.ForeignKey(
        "people.Employee",
        verbose_name=_("оцениваемый"),
        on_delete=models.CASCADE,
        related_name="assessment_360_respondents",
    )
    respondent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("респондент"),
        on_delete=models.CASCADE,
        related_name="assessment_360_invitations",
    )
    relation = models.CharField(
        _("отношение"),
        max_length=20,
        choices=RespondentRelation.choices,
        db_index=True,
    )
    status = models.CharField(
        _("состояние"),
        max_length=20,
        choices=RespondentStatus.choices,
        default=RespondentStatus.INVITED,
        db_index=True,
    )
    session = models.OneToOneField(
        AssessmentSession,
        verbose_name=_("сессия оценки"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="respondent_invitation",
        help_text=_("Создаётся при первом открытии формы респондентом"),
    )
    access_token = models.CharField(
        _("код доступа"),
        max_length=64,
        unique=True,
        editable=False,
        help_text=_("Ссылка-приглашение для заполнения формы"),
    )
    invited_at = models.DateTimeField(_("приглашён"), auto_now_add=True)
    reminded_at = models.DateTimeField(_("напоминание отправлено"), null=True, blank=True)
    completed_at = models.DateTimeField(_("заполнил"), null=True, blank=True)
    decline_reason = models.CharField(_("причина отказа"), max_length=255, blank=True)

    class Meta:
        verbose_name = _("респондент оценки 360°")
        verbose_name_plural = _("респонденты оценки 360°")
        ordering = ["employee", "relation"]
        indexes = [
            models.Index(fields=["campaign", "status"], name="respondent_campaign_idx"),
            models.Index(fields=["employee", "relation"], name="respondent_employee_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "employee", "respondent"],
                name="respondent_unique_per_employee",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.employee.short_name} ← {self.get_relation_display()}"

    def save(self, *args, **kwargs) -> None:
        if not self.access_token:
            self.access_token = generate_token(48)
        super().save(*args, **kwargs)

    @property
    def weight(self) -> Decimal:
        """Вес категории респондента при сведении оценки 360°."""
        return RESPONDENT_WEIGHTS.get(self.relation, Decimal("0.10"))

    @property
    def is_pending(self) -> bool:
        return self.status in {RespondentStatus.INVITED, RespondentStatus.IN_PROGRESS}

    def mark_completed(self) -> None:
        """Фиксирует заполнение формы респондентом."""
        self.status = RespondentStatus.COMPLETED
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "completed_at", "updated_at"])


# ---------------------------------------------------------------------------
# Встроенное тестирование
# ---------------------------------------------------------------------------
class Test(DescribedModel, ActivatableModel, TimeStampedModel):
    """Тест для проверки профессиональных компетенций."""

    code = models.SlugField(_("код"), max_length=48, unique=True, validators=[code_validator])
    competency = models.ForeignKey(
        "competencies.Competency",
        verbose_name=_("компетенция"),
        on_delete=models.PROTECT,
        related_name="tests",
        help_text=_("Компетенция, уровень которой проверяет тест"),
    )
    instructions = models.TextField(_("инструкция"), blank=True)
    time_limit_minutes = models.PositiveSmallIntegerField(
        _("ограничение времени, минут"),
        default=30,
        validators=[MinValueValidator(1), MaxValueValidator(300)],
    )
    questions_per_attempt = models.PositiveSmallIntegerField(
        _("вопросов в попытке"),
        default=10,
        help_text=_("Вопросы выбираются случайно из банка вопросов теста"),
    )
    passing_score = models.PositiveSmallIntegerField(
        _("проходной балл"),
        default=60,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    max_attempts = models.PositiveSmallIntegerField(
        _("допустимо попыток"),
        default=2,
        help_text=_("Ноль — без ограничения"),
    )
    shuffle_questions = models.BooleanField(_("перемешивать вопросы"), default=True)
    shuffle_options = models.BooleanField(_("перемешивать варианты"), default=True)
    show_result_immediately = models.BooleanField(
        _("показывать результат сразу"),
        default=True,
        help_text=_("Снимите отметку, если результат объявляет HR-специалист"),
    )

    class Meta:
        verbose_name = _("тест")
        verbose_name_plural = _("тесты")
        ordering = ["name"]
        indexes = [
            models.Index(fields=["competency", "is_active"], name="test_competency_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("assessment:test_detail", kwargs={"pk": self.pk})

    @property
    def question_count(self) -> int:
        return self.questions.filter(is_active=True).count()

    @property
    def is_ready(self) -> bool:
        """Достаточно ли вопросов в банке для формирования попытки."""
        return self.question_count >= self.questions_per_attempt


class QuestionKind(models.TextChoices):
    """Тип вопроса теста."""

    SINGLE = "single", _("Один правильный ответ")
    MULTIPLE = "multiple", _("Несколько правильных ответов")
    SCALE = "scale", _("Шкала согласия")
    OPEN = "open", _("Развёрнутый ответ")


#: Типы вопросов, проверяемые автоматически. Развёрнутый ответ требует
#: проверки экспертом, поэтому попытка с такими вопросами не закрывается сама.
AUTO_CHECKED_KINDS = frozenset({QuestionKind.SINGLE, QuestionKind.MULTIPLE, QuestionKind.SCALE})


class TestQuestion(OrderedModel, ActivatableModel, TimeStampedModel):
    """Вопрос банка тестовых заданий."""

    test = models.ForeignKey(
        Test,
        verbose_name=_("тест"),
        on_delete=models.CASCADE,
        related_name="questions",
    )
    text = models.TextField(_("формулировка"))
    kind = models.CharField(
        _("тип"),
        max_length=20,
        choices=QuestionKind.choices,
        default=QuestionKind.SINGLE,
    )
    difficulty = models.PositiveSmallIntegerField(
        _("сложность"),
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text=_("Соответствует уровню шкалы компетенции"),
    )
    weight = models.PositiveSmallIntegerField(
        _("вес в баллах"),
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
    )
    explanation = models.TextField(
        _("пояснение к ответу"),
        blank=True,
        help_text=_("Показывается после завершения попытки"),
    )

    class Meta:
        verbose_name = _("вопрос теста")
        verbose_name_plural = _("вопросы тестов")
        ordering = ["test", "sort_order", "id"]
        indexes = [
            models.Index(fields=["test", "is_active"], name="question_test_idx"),
        ]

    def __str__(self) -> str:
        return self.text[:80]

    @property
    def correct_option_ids(self) -> list[int]:
        return list(self.options.filter(is_correct=True).values_list("id", flat=True))

    @property
    def is_auto_checked(self) -> bool:
        return self.kind in AUTO_CHECKED_KINDS


class TestOption(OrderedModel, TimeStampedModel):
    """Вариант ответа на вопрос теста."""

    question = models.ForeignKey(
        TestQuestion,
        verbose_name=_("вопрос"),
        on_delete=models.CASCADE,
        related_name="options",
    )
    text = models.CharField(_("вариант ответа"), max_length=500)
    is_correct = models.BooleanField(_("правильный"), default=False)
    score_value = models.SmallIntegerField(
        _("балл варианта"),
        default=0,
        help_text=_("Для шкальных вопросов: вклад варианта в результат"),
    )

    class Meta:
        verbose_name = _("вариант ответа")
        verbose_name_plural = _("варианты ответа")
        ordering = ["question", "sort_order", "id"]

    def __str__(self) -> str:
        return self.text[:60]


class AttemptStatus(models.TextChoices):
    """Состояние попытки прохождения теста."""

    IN_PROGRESS = "in_progress", _("Выполняется")
    SUBMITTED = "submitted", _("Завершена")
    EXPIRED = "expired", _("Время истекло")
    CHECKED = "checked", _("Проверена")
    CANCELLED = "cancelled", _("Аннулирована")


class TestAttemptQuerySet(models.QuerySet):
    """Выборки попыток тестирования."""

    def finished(self) -> Self:
        return self.filter(status__in=[AttemptStatus.SUBMITTED, AttemptStatus.CHECKED])

    def passed(self) -> Self:
        return self.filter(is_passed=True)

    def for_employee(self, employee_id: int) -> Self:
        return self.filter(employee_id=employee_id)


class TestAttempt(UUIDModel, TimeStampedModel):
    """Попытка прохождения теста."""

    test = models.ForeignKey(
        Test,
        verbose_name=_("тест"),
        on_delete=models.PROTECT,
        related_name="attempts",
    )
    employee = models.ForeignKey(
        "people.Employee",
        verbose_name=_("испытуемый"),
        on_delete=models.CASCADE,
        related_name="test_attempts",
    )
    session = models.ForeignKey(
        AssessmentSession,
        verbose_name=_("сессия оценки"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="test_attempts",
        help_text=_("Заполняется, если тест проводится в рамках кампании"),
    )
    attempt_number = models.PositiveSmallIntegerField(_("номер попытки"), default=1)
    status = models.CharField(
        _("состояние"),
        max_length=20,
        choices=AttemptStatus.choices,
        default=AttemptStatus.IN_PROGRESS,
        db_index=True,
    )
    started_at = models.DateTimeField(_("начата"), auto_now_add=True)
    finished_at = models.DateTimeField(_("завершена"), null=True, blank=True)
    expires_at = models.DateTimeField(
        _("истекает"),
        null=True,
        blank=True,
        help_text=_("Рассчитывается по ограничению времени теста"),
    )
    score = models.DecimalField(
        _("результат"),
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
    )
    is_passed = models.BooleanField(_("пройден"), default=False, db_index=True)

    objects = TestAttemptQuerySet.as_manager()

    class Meta:
        verbose_name = _("попытка тестирования")
        verbose_name_plural = _("попытки тестирования")
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["employee", "-started_at"], name="attempt_employee_idx"),
            models.Index(fields=["test", "is_passed"], name="attempt_test_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["test", "employee", "attempt_number"],
                name="attempt_unique_number",
            ),
        ]

    def __str__(self) -> str:
        return str(
            _("%(employee)s — %(test)s (попытка %(number)s)")
            % {
                "employee": self.employee.short_name,
                "test": self.test.name,
                "number": self.attempt_number,
            }
        )

    def save(self, *args, **kwargs) -> None:
        """Проставляет момент истечения времени при создании попытки."""
        if self.expires_at is None and self.test_id:
            base = self.started_at or timezone.now()
            self.expires_at = base + dt.timedelta(minutes=self.test.time_limit_minutes)
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and timezone.now() > self.expires_at

    @property
    def duration_seconds(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds())

    @property
    def seconds_left(self) -> int:
        """Остаток времени; ноль, если время вышло."""
        if self.expires_at is None:
            return 0
        return max(0, int((self.expires_at - timezone.now()).total_seconds()))


class TestAnswer(TimeStampedModel):
    """Ответ испытуемого на вопрос попытки."""

    attempt = models.ForeignKey(
        TestAttempt,
        verbose_name=_("попытка"),
        on_delete=models.CASCADE,
        related_name="answers",
    )
    question = models.ForeignKey(
        TestQuestion,
        verbose_name=_("вопрос"),
        on_delete=models.PROTECT,
        related_name="answers",
    )
    selected_options = models.ManyToManyField(
        TestOption,
        verbose_name=_("выбранные варианты"),
        related_name="answers",
        blank=True,
    )
    text_answer = models.TextField(_("развёрнутый ответ"), blank=True)
    is_correct = models.BooleanField(_("верный ответ"), default=False)
    awarded_score = models.DecimalField(
        _("начислено баллов"),
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("проверил"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checked_answers",
        help_text=_("Заполняется для вопросов с развёрнутым ответом"),
    )
    answered_at = models.DateTimeField(_("отвечено"), auto_now=True)

    class Meta:
        verbose_name = _("ответ на вопрос")
        verbose_name_plural = _("ответы на вопросы")
        ordering = ["attempt", "question__sort_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["attempt", "question"],
                name="answer_unique_per_attempt",
            ),
        ]

    def __str__(self) -> str:
        return str(_("Ответ на «%(question)s»") % {"question": self.question.text[:40]})
