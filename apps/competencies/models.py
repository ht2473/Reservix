"""Компетентностная модель."""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from core.models import (
    ActivatableModel,
    ActiveQuerySet,
    DescribedModel,
    OrderedModel,
    ReferenceModel,
    TimeStampedModel,
)
from core.settings_types import get_reservix_settings
from core.validators import code_validator, validate_competency_level

#: Шаг весов: три знака после запятой достаточно, чтобы распределить вес между
#: двумя десятками компетенций без накопления ошибки округления.
WEIGHT_DECIMAL_PLACES = 3
#: Допустимое отклонение суммы весов от единицы. Веса вводит человек, и
#: требование точного равенства сделало бы форму невыполнимой.
WEIGHT_SUM_TOLERANCE = Decimal("0.005")


class CompetencyKind(models.TextChoices):
    """Тип компетенции."""

    CORPORATE = "corporate", _("Корпоративная")
    MANAGERIAL = "managerial", _("Управленческая")
    PROFESSIONAL = "professional", _("Профессиональная")
    PERSONAL = "personal", _("Личностная")


class CompetencyCategory(ReferenceModel):
    """Категория компетенций — раздел справочника."""

    kind = models.CharField(
        _("тип"),
        max_length=20,
        choices=CompetencyKind.choices,
        default=CompetencyKind.CORPORATE,
        db_index=True,
    )
    color = models.CharField(
        _("цвет"),
        max_length=7,
        blank=True,
        help_text=_("Шестнадцатеричный код для диаграмм, например #2F6F5E"),
    )

    class Meta(ReferenceModel.Meta):
        verbose_name = _("категория компетенций")
        verbose_name_plural = _("категории компетенций")

    def get_absolute_url(self) -> str:
        return reverse("competencies:category_detail", kwargs={"pk": self.pk})


class CompetencyQuerySet(ActiveQuerySet):
    """Выборки справочника компетенций."""

    def of_kind(self, *kinds: str) -> Self:
        return self.filter(kind__in=kinds)

    def managerial(self) -> Self:
        return self.filter(kind=CompetencyKind.MANAGERIAL)

    def testable(self) -> Self:
        """Компетенции, проверяемые встроенным тестированием."""
        return self.filter(is_testable=True, is_active=True)

    def with_scale(self) -> Self:
        """Подгружает уровни шкалы: карточка компетенции иначе даёт N+1."""
        return self.prefetch_related("scale_levels")

    def with_category(self) -> Self:
        return self.select_related("category")


class Competency(DescribedModel, OrderedModel, ActivatableModel, TimeStampedModel):
    """Компетенция — наблюдаемая характеристика поведения."""

    code = models.SlugField(_("код"), max_length=48, unique=True, validators=[code_validator])
    category = models.ForeignKey(
        CompetencyCategory,
        verbose_name=_("категория"),
        on_delete=models.PROTECT,
        related_name="competencies",
    )
    kind = models.CharField(
        _("тип"),
        max_length=20,
        choices=CompetencyKind.choices,
        default=CompetencyKind.CORPORATE,
        db_index=True,
    )
    definition = models.TextField(
        _("определение"),
        blank=True,
        help_text=_("Какое поведение считается проявлением компетенции"),
    )
    development_hint = models.TextField(
        _("рекомендации по развитию"),
        blank=True,
        help_text=_("Подставляется в индивидуальный план развития при выявлении разрыва"),
    )
    is_testable = models.BooleanField(
        _("проверяется тестом"),
        default=False,
        help_text=_("Для таких компетенций доступно встроенное тестирование"),
    )
    is_core = models.BooleanField(
        _("сквозная"),
        default=False,
        db_index=True,
        help_text=_("Обязательна для всех должностей организации"),
    )

    objects = CompetencyQuerySet.as_manager()

    class Meta:
        verbose_name = _("компетенция")
        verbose_name_plural = _("компетенции")
        ordering = ["category__sort_order", "sort_order", "name"]
        indexes = [
            models.Index(fields=["kind", "is_active"], name="competency_kind_active_idx"),
            models.Index(fields=["category", "sort_order"], name="competency_category_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("competencies:competency_detail", kwargs={"pk": self.pk})

    def level_description(self, level: int) -> str:
        """Поведенческие индикаторы указанного уровня владения."""
        scale_level = self.scale_levels.filter(level=level).first()
        return scale_level.indicators if scale_level else ""

    @property
    def has_full_scale(self) -> bool:
        """Признак полностью описанной шкалы."""
        return len(self.scale_levels.all()) == self.expected_scale_size()

    @staticmethod
    def expected_scale_size() -> int:
        """Число уровней полной шкалы согласно настройкам организации."""
        options = get_reservix_settings()
        return options["COMPETENCY_SCALE_MAX"] - options["COMPETENCY_SCALE_MIN"] + 1


class CompetencyScaleLevel(TimeStampedModel):
    """Уровень владения компетенцией с поведенческими индикаторами."""

    competency = models.ForeignKey(
        Competency,
        verbose_name=_("компетенция"),
        on_delete=models.CASCADE,
        related_name="scale_levels",
    )
    level = models.PositiveSmallIntegerField(
        _("уровень"),
        validators=[validate_competency_level],
        help_text=_("Значение по шкале оценки компетенций"),
    )
    name = models.CharField(
        _("наименование уровня"),
        max_length=100,
        help_text=_("Например: «Уверенное применение»"),
    )
    indicators = models.TextField(
        _("поведенческие индикаторы"),
        help_text=_("Наблюдаемые проявления, соответствующие уровню"),
    )

    class Meta:
        verbose_name = _("уровень шкалы")
        verbose_name_plural = _("уровни шкалы")
        ordering = ["competency", "level"]
        constraints = [
            models.UniqueConstraint(
                fields=["competency", "level"],
                name="scalelevel_unique_per_competency",
            ),
        ]

    def __str__(self) -> str:
        return str(
            _("%(competency)s: уровень %(level)s — %(name)s")
            % {"competency": self.competency.name, "level": self.level, "name": self.name}
        )


class CompetencyModelQuerySet(ActiveQuerySet):
    """Выборки моделей компетенций."""

    def published(self) -> Self:
        return self.filter(is_active=True, is_published=True)

    def for_position(self, position_id: int) -> Self:
        """Модели, применимые к должности."""
        return self.filter(positions__id=position_id).distinct()

    def with_requirements(self) -> Self:
        return self.prefetch_related("requirements__competency")


class CompetencyModel(DescribedModel, ActivatableModel, TimeStampedModel):
    """Модель компетенций — эталонный набор требований для группы должностей."""

    code = models.SlugField(_("код"), max_length=48, unique=True, validators=[code_validator])
    version = models.PositiveSmallIntegerField(
        _("версия"),
        default=1,
        help_text=_("Увеличивается при пересмотре состава требований"),
    )
    grade = models.ForeignKey(
        "org.PositionGrade",
        verbose_name=_("типовой грейд"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="competency_models",
        help_text=_("Модель применяется к должностям этого грейда по умолчанию"),
    )
    positions = models.ManyToManyField(
        "org.Position",
        verbose_name=_("должности"),
        related_name="competency_models",
        blank=True,
        help_text=_("Должности, требования к которым описывает модель"),
    )
    competencies = models.ManyToManyField(
        Competency,
        verbose_name=_("компетенции"),
        through="CompetencyRequirement",
        related_name="models",
    )
    valid_from = models.DateField(
        _("действует с"),
        null=True,
        blank=True,
        help_text=_("Дата введения модели в действие"),
    )
    is_published = models.BooleanField(
        _("опубликована"),
        default=False,
        help_text=_("Опубликованная модель доступна для кампаний оценки"),
    )

    objects = CompetencyModelQuerySet.as_manager()

    class Meta:
        verbose_name = _("модель компетенций")
        verbose_name_plural = _("модели компетенций")
        ordering = ["name", "-version"]
        indexes = [
            models.Index(fields=["is_active", "is_published"], name="compmodel_published_idx"),
        ]

    def __str__(self) -> str:
        return str(
            _("%(name)s (версия %(version)s)") % {"name": self.name, "version": self.version}
        )

    def get_absolute_url(self) -> str:
        return reverse("competencies:model_detail", kwargs={"pk": self.pk})

    # -- Веса ---------------------------------------------------------------
    @property
    def weight_sum(self) -> Decimal:
        """Сумма весов требований модели."""
        total = self.requirements.aggregate(total=Sum("weight"))["total"]
        return total if total is not None else Decimal("0")

    @property
    def is_weight_balanced(self) -> bool:
        """Проверяет, что веса требований образуют распределение."""
        return abs(self.weight_sum - Decimal("1")) <= WEIGHT_SUM_TOLERANCE

    def normalized_weights(self) -> dict[int, Decimal]:
        """Веса требований, приведённые к сумме единица."""
        total = self.weight_sum
        if total <= 0:
            requirements = list(self.requirements.values_list("competency_id", flat=True))
            if not requirements:
                return {}
            equal = Decimal("1") / Decimal(len(requirements))
            return dict.fromkeys(requirements, equal)

        return {
            requirement.competency_id: requirement.weight / total
            for requirement in self.requirements.all()
        }

    @property
    def critical_competency_ids(self) -> list[int]:
        """Компетенции, недобор по которым закрывает путь к назначению."""
        return list(
            self.requirements.filter(is_critical=True).values_list("competency_id", flat=True)
        )


class CompetencyRequirementQuerySet(models.QuerySet):
    """Выборки требований модели компетенций."""

    def critical(self) -> Self:
        return self.filter(is_critical=True)

    def with_competency(self) -> Self:
        return self.select_related("competency", "competency__category")


class CompetencyRequirement(TimeStampedModel):
    """Требование модели: целевой уровень компетенции и её вес."""

    competency_model = models.ForeignKey(
        CompetencyModel,
        verbose_name=_("модель компетенций"),
        on_delete=models.CASCADE,
        related_name="requirements",
    )
    competency = models.ForeignKey(
        Competency,
        verbose_name=_("компетенция"),
        on_delete=models.PROTECT,
        related_name="requirements",
    )
    target_level = models.PositiveSmallIntegerField(
        _("целевой уровень"),
        validators=[validate_competency_level],
        help_text=_("Уровень владения, требуемый должностью"),
    )
    minimum_level = models.PositiveSmallIntegerField(
        _("минимально допустимый уровень"),
        null=True,
        blank=True,
        validators=[validate_competency_level],
        help_text=_("Ниже этого значения кандидат не рассматривается"),
    )
    weight = models.DecimalField(
        _("вес"),
        max_digits=5,
        decimal_places=WEIGHT_DECIMAL_PLACES,
        default=Decimal("0.100"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
        help_text=_("Доля компетенции в оценке соответствия, от 0 до 1"),
    )
    is_critical = models.BooleanField(
        _("критичное требование"),
        default=False,
        help_text=_("Недобор по критичному требованию блокирует включение в резерв"),
    )
    comment = models.CharField(_("пояснение"), max_length=255, blank=True)

    objects = CompetencyRequirementQuerySet.as_manager()

    class Meta:
        verbose_name = _("требование к компетенции")
        verbose_name_plural = _("требования к компетенциям")
        ordering = ["-weight", "competency__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["competency_model", "competency"],
                name="requirement_unique_per_model",
            ),
            models.CheckConstraint(
                condition=(
                    Q(minimum_level__isnull=True) | Q(minimum_level__lte=models.F("target_level"))
                ),
                name="requirement_minimum_not_above_target",
            ),
        ]
        indexes = [
            models.Index(fields=["competency_model", "-weight"], name="requirement_model_idx"),
        ]

    def __str__(self) -> str:
        return str(
            _("%(competency)s → уровень %(level)s")
            % {"competency": self.competency.name, "level": self.target_level}
        )

    def gap_for(self, actual_level: float | None) -> float:
        """Разрыв между целевым и фактическим уровнем."""
        if actual_level is None:
            return float(self.target_level)
        return max(0.0, float(self.target_level) - float(actual_level))

    def is_satisfied_by(self, actual_level: float | None) -> bool:
        """Проверяет выполнение требования фактическим уровнем."""
        if actual_level is None:
            return False
        threshold = self.minimum_level if self.minimum_level is not None else self.target_level
        return float(actual_level) >= float(threshold)
