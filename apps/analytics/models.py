"""Аналитическое ядро: хранение схем оценивания и результатов расчёта."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Self

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.constants import NINE_BOX_MATRIX, NineBoxCell
from core.models import (
    ActivatableModel,
    ActiveQuerySet,
    AuthoredModel,
    DescribedModel,
    OrderedModel,
    TimeStampedModel,
    UUIDModel,
)
from core.settings_types import get_reservix_settings
from core.validators import code_validator

#: Точность хранения весов.
WEIGHT_DECIMAL_PLACES = 5


class WeightingMethod(models.TextChoices):
    """Способ получения весов частных критериев."""

    EXPERT = "expert", _("Экспертное назначение")
    AHP = "ahp", _("Метод анализа иерархий")
    ENTROPY = "entropy", _("Энтропийный метод")
    EQUAL = "equal", _("Равные веса")


class AggregationMethod(models.TextChoices):
    """Способ свёртки частных критериев в композитный индекс."""

    WEIGHTED_SUM = "weighted_sum", _("Взвешенная сумма")
    TOPSIS = "topsis", _("TOPSIS")
    GEOMETRIC = "geometric", _("Взвешенное геометрическое среднее")


class CriterionDirection(models.TextChoices):
    """Направление предпочтения по критерию."""

    BENEFIT = "benefit", _("Чем больше, тем лучше")
    COST = "cost", _("Чем меньше, тем лучше")


class ScoringCriterion(DescribedModel, OrderedModel, ActivatableModel, TimeStampedModel):
    """Частный критерий готовности резервиста."""

    code = models.SlugField(_("код"), max_length=48, unique=True, validators=[code_validator])
    source = models.CharField(
        _("источник данных"),
        max_length=64,
        help_text=_("Идентификатор функции расчёта частного показателя"),
    )
    direction = models.CharField(
        _("направление"),
        max_length=10,
        choices=CriterionDirection.choices,
        default=CriterionDirection.BENEFIT,
    )
    unit = models.CharField(
        _("единица измерения"),
        max_length=32,
        blank=True,
        help_text=_("Например: баллы, месяцы, проценты"),
    )
    methodology = models.TextField(
        _("методика расчёта"),
        blank=True,
        help_text=_("Формула и правило нормирования — материал для описания методики"),
    )

    class Meta:
        verbose_name = _("критерий готовности")
        verbose_name_plural = _("критерии готовности")
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("analytics:criterion_detail", kwargs={"pk": self.pk})


class ScoringSchemeQuerySet(ActiveQuerySet["ScoringScheme"]):
    """Выборки схем оценивания."""

    def usable(self) -> Self:
        """Схемы, пригодные для расчёта: активные и согласованные."""
        return self.filter(is_active=True).exclude(method=WeightingMethod.AHP, is_consistent=False)

    def default(self) -> ScoringScheme | None:
        return self.filter(is_default=True, is_active=True).first()

    def with_weights(self) -> Self:
        return self.prefetch_related("weights__criterion")


class ScoringScheme(DescribedModel, ActivatableModel, AuthoredModel, TimeStampedModel):
    """Схема оценивания — набор весов критериев с указанием их происхождения."""

    code = models.SlugField(_("код"), max_length=48, unique=True, validators=[code_validator])
    method = models.CharField(
        _("способ получения весов"),
        max_length=20,
        choices=WeightingMethod.choices,
        default=WeightingMethod.EXPERT,
        db_index=True,
    )
    aggregation = models.CharField(
        _("способ свёртки"),
        max_length=20,
        choices=AggregationMethod.choices,
        default=AggregationMethod.WEIGHTED_SUM,
    )
    criteria = models.ManyToManyField(
        ScoringCriterion,
        verbose_name=_("критерии"),
        through="ScoringWeight",
        related_name="schemes",
    )
    is_default = models.BooleanField(
        _("схема по умолчанию"),
        default=False,
        help_text=_("Применяется, если расчёт запущен без указания схемы"),
    )
    pairwise_matrix = models.JSONField(
        _("матрица парных сравнений"),
        null=True,
        blank=True,
        help_text=_("Суждения эксперта по шкале Саати; заполняется для метода анализа иерархий"),
    )
    consistency_ratio = models.DecimalField(
        _("отношение согласованности"),
        max_digits=6,
        decimal_places=4,
        null=True,
        blank=True,
        help_text=_("Значение выше 0,1 означает противоречивость суждений эксперта"),
    )
    is_consistent = models.BooleanField(
        _("суждения согласованы"),
        default=True,
        help_text=_("Рассчитывается автоматически при построении матрицы"),
    )
    calculated_at = models.DateTimeField(_("веса рассчитаны"), null=True, blank=True)
    source_note = models.TextField(
        _("происхождение весов"),
        blank=True,
        help_text=_("Кто и на каком основании назначил веса; для энтропийной схемы — выборка"),
    )

    objects = ScoringSchemeQuerySet.as_manager()

    class Meta:
        verbose_name = _("схема оценивания")
        verbose_name_plural = _("схемы оценивания")
        ordering = ["-is_default", "name"]
        constraints = [
            # Схема по умолчанию может быть только одна: иначе результат
            # расчёта зависел бы от порядка строк в таблице.
            models.UniqueConstraint(
                fields=["is_default"],
                condition=Q(is_default=True),
                name="scheme_single_default",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_method_display()})"

    def get_absolute_url(self) -> str:
        return reverse("analytics:scheme_detail", kwargs={"pk": self.pk})

    @property
    def weight_map(self) -> dict[str, Decimal]:
        """Веса критериев, ключ — код критерия."""
        return {weight.criterion.code: weight.weight for weight in self.weights.all()}

    @property
    def weight_sum(self) -> Decimal:
        total = sum((weight.weight for weight in self.weights.all()), Decimal("0"))
        return total

    @property
    def is_ready(self) -> bool:
        """Пригодна ли схема к применению."""
        if not self.is_active or not self.weights.exists():
            return False
        if self.method == WeightingMethod.AHP and not self.is_consistent:
            return False
        return abs(self.weight_sum - Decimal("1")) <= Decimal("0.01")

    @property
    def consistency_threshold(self) -> float:
        return get_reservix_settings()["AHP_MAX_CONSISTENCY_RATIO"]


class ScoringWeight(TimeStampedModel):
    """Вес критерия в схеме оценивания."""

    scheme = models.ForeignKey(
        ScoringScheme,
        verbose_name=_("схема оценивания"),
        on_delete=models.CASCADE,
        related_name="weights",
    )
    criterion = models.ForeignKey(
        ScoringCriterion,
        verbose_name=_("критерий"),
        on_delete=models.PROTECT,
        related_name="weights",
    )
    weight = models.DecimalField(
        _("вес"),
        max_digits=8,
        decimal_places=WEIGHT_DECIMAL_PLACES,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
        help_text=_("Нормированная доля критерия в композитном индексе"),
    )
    raw_value = models.DecimalField(
        _("исходное значение"),
        max_digits=10,
        decimal_places=5,
        null=True,
        blank=True,
        help_text=_("Экспертный балл или компонента собственного вектора до нормировки"),
    )
    rank = models.PositiveSmallIntegerField(
        _("ранг важности"),
        null=True,
        blank=True,
        help_text=_("Порядковое место критерия по значимости"),
    )
    comment = models.CharField(_("обоснование"), max_length=255, blank=True)

    class Meta:
        verbose_name = _("вес критерия")
        verbose_name_plural = _("веса критериев")
        ordering = ["-weight"]
        constraints = [
            models.UniqueConstraint(
                fields=["scheme", "criterion"],
                name="weight_unique_per_scheme",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.criterion.name}: {self.weight}"

    @property
    def percent(self) -> float:
        """Вес в процентах — форма, привычная кадровой службе."""
        return round(float(self.weight) * 100, 1)


class ReadinessSnapshotQuerySet(models.QuerySet["ReadinessSnapshot"]):
    """Выборки истории расчётов готовности."""

    def latest_for(self, entry_id: int) -> ReadinessSnapshot | None:
        return self.filter(entry_id=entry_id).order_by("-calculated_at").first()

    def by_scheme(self, scheme_id: int) -> Self:
        return self.filter(scheme_id=scheme_id)

    def in_period(self, start: dt.date, end: dt.date) -> Self:
        return self.filter(calculated_at__date__gte=start, calculated_at__date__lte=end)

    def with_related(self) -> Self:
        return self.select_related("entry", "entry__employee", "scheme")


class ReadinessSnapshot(TimeStampedModel):
    """Результат расчёта индекса готовности на определённый момент."""

    entry = models.ForeignKey(
        "reserve.ReserveEntry",
        verbose_name=_("запись резерва"),
        on_delete=models.CASCADE,
        related_name="readiness_snapshots",
    )
    scheme = models.ForeignKey(
        ScoringScheme,
        verbose_name=_("схема оценивания"),
        on_delete=models.PROTECT,
        related_name="snapshots",
    )
    index_value = models.DecimalField(
        _("индекс готовности"),
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
    )
    readiness_level = models.CharField(
        _("уровень готовности"),
        max_length=20,
        help_text=_("Определяется по индексу и порогам из настроек"),
    )
    criterion_values = models.JSONField(
        _("частные показатели"),
        default=dict,
        help_text=_("Значения критериев до и после нормирования, ключ — код критерия"),
    )
    rank_in_position = models.PositiveSmallIntegerField(
        _("ранг среди претендентов"),
        null=True,
        blank=True,
        help_text=_("Место кандидата в списке преемников на целевую должность"),
    )
    candidates_total = models.PositiveSmallIntegerField(
        _("всего претендентов"),
        null=True,
        blank=True,
        help_text=_("Знаменатель ранга: без него ранг 3 неинтерпретируем"),
    )
    calculated_at = models.DateTimeField(_("рассчитан"), default=timezone.now, db_index=True)
    is_current = models.BooleanField(
        _("текущий расчёт"),
        default=True,
        db_index=True,
        help_text=_("Признак последнего расчёта по данной схеме"),
    )

    objects = ReadinessSnapshotQuerySet.as_manager()

    class Meta:
        verbose_name = _("расчёт готовности")
        verbose_name_plural = _("история расчётов готовности")
        ordering = ["-calculated_at"]
        indexes = [
            models.Index(fields=["entry", "-calculated_at"], name="snapshot_entry_idx"),
            models.Index(fields=["scheme", "is_current"], name="snapshot_scheme_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.entry}: {self.index_value} ({self.scheme.code})"

    @property
    def weakest_criteria(self) -> list[str]:
        """Коды критериев с наименьшими нормированными значениями."""
        normalized = {
            code: values.get("normalized", 0)
            for code, values in self.criterion_values.items()
            if isinstance(values, dict)
        }
        return [code for code, _value in sorted(normalized.items(), key=lambda item: item[1])[:3]]


class CalibrationStatus(models.TextChoices):
    """Состояние сессии калибровки."""

    PLANNED = "planned", _("Запланирована")
    IN_PROGRESS = "in_progress", _("Проводится")
    COMPLETED = "completed", _("Завершена")
    CANCELLED = "cancelled", _("Отменена")


class CalibrationSession(UUIDModel, DescribedModel, TimeStampedModel):
    """Сессия калибровки — совместная сверка оценок руководителями."""

    held_on = models.DateField(_("дата проведения"), db_index=True)
    status = models.CharField(
        _("состояние"),
        max_length=20,
        choices=CalibrationStatus.choices,
        default=CalibrationStatus.PLANNED,
        db_index=True,
    )
    branch = models.ForeignKey(
        "org.Branch",
        verbose_name=_("филиал"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="calibration_sessions",
    )
    department = models.ForeignKey(
        "org.Department",
        verbose_name=_("подразделение"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="calibration_sessions",
    )
    facilitator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("ведущий"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="facilitated_calibrations",
    )
    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("участники"),
        related_name="calibration_sessions",
        blank=True,
    )
    campaign = models.ForeignKey(
        "assessment.AssessmentCampaign",
        verbose_name=_("кампания оценки"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="calibration_sessions",
    )
    agenda = models.TextField(_("повестка"), blank=True)
    decisions = models.TextField(
        _("принятые решения"),
        blank=True,
        help_text=_("Протокол: кто перемещён между ячейками и на каком основании"),
    )
    completed_at = models.DateTimeField(_("завершена"), null=True, blank=True)

    class Meta:
        verbose_name = _("сессия калибровки")
        verbose_name_plural = _("сессии калибровки")
        ordering = ["-held_on"]
        indexes = [
            models.Index(fields=["status", "-held_on"], name="calibration_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.held_on})"

    def get_absolute_url(self) -> str:
        return reverse("analytics:calibration_detail", kwargs={"public_id": self.public_id})

    @property
    def placement_count(self) -> int:
        return self.placements.count()

    @property
    def adjusted_count(self) -> int:
        """Число оценок, изменённых в ходе калибровки."""
        return self.placements.filter(is_adjusted=True).count()


class NineBoxPlacementQuerySet(models.QuerySet):
    """Выборки размещений в матрице 9-box."""

    def current(self) -> Self:
        return self.filter(is_current=True)

    def in_cell(self, cell: str) -> Self:
        return self.filter(cell=cell)

    def talent_pool(self) -> Self:
        """Ячейки, из которых формируется кадровый резерв."""
        from core.constants import TALENT_POOL_CELLS

        return self.filter(cell__in=TALENT_POOL_CELLS, is_current=True)

    def with_related(self) -> Self:
        return self.select_related("employee", "employee__department", "session")


class NineBoxPlacement(TimeStampedModel):
    """Положение сотрудника в матрице «результативность — потенциал»."""

    employee = models.ForeignKey(
        "people.Employee",
        verbose_name=_("сотрудник"),
        on_delete=models.CASCADE,
        related_name="nine_box_placements",
    )
    session = models.ForeignKey(
        CalibrationSession,
        verbose_name=_("сессия калибровки"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="placements",
    )
    performance = models.DecimalField(
        _("результативность"),
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text=_("Достижение целей за оцениваемый период, 0–100"),
    )
    potential = models.DecimalField(
        _("потенциал"),
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text=_("Способность к росту и освоению более сложных задач, 0–100"),
    )
    cell = models.CharField(
        _("ячейка матрицы"),
        max_length=24,
        choices=NineBoxCell.choices,
        db_index=True,
        editable=False,
    )
    initial_performance = models.DecimalField(
        _("результативность до калибровки"),
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )
    initial_potential = models.DecimalField(
        _("потенциал до калибровки"),
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )
    is_adjusted = models.BooleanField(
        _("скорректировано на калибровке"),
        default=False,
        help_text=_("Оценка изменена по итогам обсуждения руководителями"),
    )
    period_start = models.DateField(_("начало периода"), null=True, blank=True)
    period_end = models.DateField(_("окончание периода"), null=True, blank=True)
    placed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("оценку выставил"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nine_box_placements",
    )
    comment = models.TextField(_("комментарий"), blank=True)
    is_current = models.BooleanField(
        _("действующая оценка"),
        default=True,
        db_index=True,
        help_text=_("Признак последнего размещения сотрудника"),
    )

    objects = NineBoxPlacementQuerySet.as_manager()

    class Meta:
        verbose_name = _("размещение в матрице 9-box")
        verbose_name_plural = _("матрица 9-box")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["employee", "is_current"], name="ninebox_employee_idx"),
            models.Index(fields=["cell", "is_current"], name="ninebox_cell_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.employee.short_name}: {self.get_cell_display()}"

    def save(self, *args, **kwargs) -> None:
        """Определяет ячейку матрицы по координатам перед сохранением."""
        self.cell = self.resolve_cell(float(self.performance), float(self.potential))
        super().save(*args, **kwargs)

    @staticmethod
    def resolve_cell(performance: float, potential: float) -> str:
        """Определяет ячейку матрицы по координатам и границам зон из настроек."""
        options = get_reservix_settings()
        low = options["NINE_BOX_LOW_MAX"]
        mid = options["NINE_BOX_MID_MAX"]

        def zone(value: float) -> int:
            if value <= low:
                return 0
            if value <= mid:
                return 1
            return 2

        return NINE_BOX_MATRIX[(zone(performance), zone(potential))]

    @property
    def is_talent(self) -> bool:
        from core.constants import TALENT_POOL_CELLS

        return self.cell in TALENT_POOL_CELLS
