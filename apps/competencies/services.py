"""Компетентностное соответствие: профиль, разрывы, лепестковая диаграмма."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.utils.translation import gettext_lazy as _

from apps.assessment.aggregation import CompetencyLevel, employee_levels, team_levels
from apps.competencies.models import (
    Competency,
    CompetencyModel,
    CompetencyRequirement,
)
from core.constants import Signal
from core.settings_types import get_reservix_settings

if TYPE_CHECKING:
    from apps.people.models import Employee

#: Разрыв в уровнях, ниже которого требование считается практически выполненным.
NEGLIGIBLE_GAP = 0.5

#: Разрыв, начиная с которого требование попадает в зону риска.
CRITICAL_GAP = 1.5


@dataclass(frozen=True, slots=True)
class RequirementGap:
    """Разрыв по одному требованию модели компетенций."""

    requirement: CompetencyRequirement
    measured: CompetencyLevel | None
    weight: Decimal

    @property
    def competency(self) -> Competency:
        return self.requirement.competency

    @property
    def target(self) -> int:
        return self.requirement.target_level

    @property
    def actual(self) -> float | None:
        """Фактический уровень; None — компетенция не оценивалась."""
        return self.measured.level if self.measured is not None else None

    @property
    def gap(self) -> float:
        """Недобор до целевого уровня; превышение разрывом не считается."""
        return round(self.requirement.gap_for(self.actual), 2)

    @property
    def is_assessed(self) -> bool:
        return self.measured is not None

    @property
    def is_satisfied(self) -> bool:
        """Выполнено ли требование фактическим уровнем."""
        return self.requirement.is_satisfied_by(self.actual)

    @property
    def is_blocking(self) -> bool:
        """Блокирует ли разрыв включение в резерв."""
        return self.requirement.is_critical and not self.is_satisfied

    @property
    def attainment(self) -> float:
        """Степень выполнения требования, 0–1."""
        if self.actual is None:
            return 0.0

        minimum = get_reservix_settings()["COMPETENCY_SCALE_MIN"]
        span = self.target - minimum
        if span <= 0:
            return 1.0 if self.actual >= self.target else 0.0
        ratio = (self.actual - minimum) / span
        return max(0.0, min(1.0, ratio))

    @property
    def contribution(self) -> float:
        """Вклад требования в итоговое соответствие, процентные пункты."""
        return round(float(self.weight) * self.attainment * 100, 2)

    @property
    def loss(self) -> float:
        """Потеря соответствия из-за разрыва, процентные пункты."""
        return round(float(self.weight) * (1 - self.attainment) * 100, 2)

    @property
    def signal(self) -> str:
        """Сигнальная семантика для оформления строки."""
        if self.is_blocking:
            return Signal.RISK
        if not self.is_assessed:
            return Signal.NEUTRAL
        if self.gap <= NEGLIGIBLE_GAP:
            return Signal.READY
        if self.gap >= CRITICAL_GAP:
            return Signal.RISK
        return Signal.DEVELOP

    # -- Представление --------------------------------------------------------
    @property
    def steps(self) -> list[dict]:
        """Сегменты шкалы для полосы уровня в интерфейсе."""
        options = get_reservix_settings()
        minimum = options["COMPETENCY_SCALE_MIN"]
        maximum = options["COMPETENCY_SCALE_MAX"]
        actual = self.actual

        result = []
        for level in range(minimum, maximum + 1):
            if actual is None:
                state = ""
            elif actual >= level:
                state = "filled"
            elif actual > level - 1:
                # Уровень 3,5 закрашивает третий сегмент целиком и четвёртый
                # наполовину — отсюда отдельное состояние.
                state = "partial"
            else:
                state = ""
            result.append({"level": level, "state": state})
        return result

    @property
    def target_offset(self) -> float:
        """Положение метки целевого уровня на полосе, проценты ширины."""
        options = get_reservix_settings()
        minimum = options["COMPETENCY_SCALE_MIN"]
        maximum = options["COMPETENCY_SCALE_MAX"]
        count = maximum - minimum + 1
        if count <= 0:
            return 0.0
        return round((self.target - minimum + 1) * 100 / count, 2)

    @property
    def delta_class(self) -> str:
        """Модификатор оформления отклонения от целевого уровня."""
        if self.actual is None:
            return "gap-row__delta--negative"
        if self.actual >= self.target:
            return (
                "gap-row__delta--positive" if self.actual > self.target else "gap-row__delta--zero"
            )
        return "gap-row__delta--negative"

    @property
    def delta_label(self) -> str:
        """Отклонение от целевого уровня со знаком."""
        if self.actual is None:
            return "—"
        delta = round(self.actual - self.target, 1)
        if delta == 0:
            return "0"
        return f"+{delta:.1f}".replace(".", ",") if delta > 0 else f"{delta:.1f}".replace(".", ",")

    @property
    def status_label(self) -> str:
        """Словесное описание состояния требования."""
        if not self.is_assessed:
            return str(_("не оценивалась"))
        if self.is_satisfied:
            return str(_("соответствует"))
        if self.gap >= CRITICAL_GAP:
            return str(_("существенный разрыв"))
        return str(_("требуется развитие"))


@dataclass(frozen=True, slots=True)
class CompetencyProfile:
    """Профиль компетенций сотрудника относительно модели требований."""

    employee_id: int
    competency_model: CompetencyModel | None
    gaps: tuple[RequirementGap, ...]
    evaluated_on: dt.date
    extra_levels: dict[int, CompetencyLevel] = field(default_factory=dict)

    # -- Итоговые показатели -------------------------------------------------
    @property
    def match_score(self) -> float:
        """Компетентностное соответствие должности, 0–100."""
        if not self.gaps:
            return 0.0
        total_weight = sum((gap.weight for gap in self.gaps), Decimal("0"))
        if total_weight <= 0:
            return 0.0
        earned = sum(float(gap.weight) * gap.attainment for gap in self.gaps)
        return round(earned / float(total_weight) * 100, 1)

    @property
    def coverage(self) -> float:
        """Доля требований, подтверждённых хотя бы одной оценкой, проценты."""
        if not self.gaps:
            return 0.0
        assessed = sum(1 for gap in self.gaps if gap.is_assessed)
        return round(assessed * 100 / len(self.gaps), 1)

    @property
    def confidence(self) -> float:
        """Средняя достоверность оценённых требований, 0–1."""
        measured = [gap.measured for gap in self.gaps if gap.measured is not None]
        if not measured:
            return 0.0
        return round(sum(level.confidence for level in measured) / len(measured), 3)

    @property
    def average_level(self) -> float | None:
        """Средний фактический уровень по оценённым требованиям."""
        levels = [gap.actual for gap in self.gaps if gap.actual is not None]
        if not levels:
            return None
        return round(sum(levels) / len(levels), 2)

    @property
    def average_target(self) -> float | None:
        """Средний целевой уровень модели — планка должности."""
        if not self.gaps:
            return None
        return round(sum(gap.target for gap in self.gaps) / len(self.gaps), 2)

    # -- Разрезы --------------------------------------------------------------
    @property
    def blocking_gaps(self) -> tuple[RequirementGap, ...]:
        """Невыполненные критичные требования."""
        return tuple(gap for gap in self.gaps if gap.is_blocking)

    @property
    def is_blocked(self) -> bool:
        """Признак наличия непреодолимого препятствия к назначению."""
        return bool(self.blocking_gaps)

    @property
    def satisfied_gaps(self) -> tuple[RequirementGap, ...]:
        return tuple(gap for gap in self.gaps if gap.is_satisfied)

    @property
    def unassessed(self) -> tuple[RequirementGap, ...]:
        return tuple(gap for gap in self.gaps if not gap.is_assessed)

    def development_priorities(self, limit: int = 5) -> tuple[RequirementGap, ...]:
        """Разрывы в порядке убывания стоимости — основа плана развития."""
        ranked = sorted(
            (gap for gap in self.gaps if not gap.is_satisfied),
            key=lambda gap: (-gap.loss, -gap.gap, gap.competency.name),
        )
        return tuple(ranked[:limit])

    @property
    def strengths(self) -> tuple[RequirementGap, ...]:
        """Компетенции, освоенные выше требуемого."""
        ranked = [gap for gap in self.gaps if gap.actual is not None and gap.actual > gap.target]
        ranked.sort(key=lambda gap: (-(gap.actual or 0) + gap.target, gap.competency.name))
        return tuple(ranked)

    def by_category(self) -> list[dict]:
        """Свод по категориям компетенций."""
        buckets: dict[int, dict] = {}
        for gap in self.gaps:
            category = gap.competency.category
            bucket = buckets.setdefault(
                category.pk,
                {
                    "category": category,
                    "gaps": [],
                    "weight": Decimal("0"),
                    "earned": 0.0,
                },
            )
            bucket["gaps"].append(gap)
            bucket["weight"] += gap.weight
            bucket["earned"] += float(gap.weight) * gap.attainment

        result = []
        for bucket in buckets.values():
            weight = float(bucket["weight"])
            levels = [gap.actual for gap in bucket["gaps"] if gap.actual is not None]
            result.append(
                {
                    "category": bucket["category"],
                    "gaps": bucket["gaps"],
                    "count": len(bucket["gaps"]),
                    "weight": round(weight, 3),
                    "match": round(bucket["earned"] / weight * 100, 1) if weight else 0.0,
                    "average_level": round(sum(levels) / len(levels), 2) if levels else None,
                    "average_target": round(
                        sum(gap.target for gap in bucket["gaps"]) / len(bucket["gaps"]), 2
                    ),
                }
            )
        result.sort(key=lambda item: item["category"].sort_order)
        return result


# ---------------------------------------------------------------------------
# Выбор модели компетенций
# ---------------------------------------------------------------------------
def resolve_competency_model(
    *,
    position_id: int | None = None,
    grade_id: int | None = None,
) -> CompetencyModel | None:
    """Определяет модель компетенций, применимую к должности."""
    if position_id is not None:
        direct = (
            CompetencyModel.objects.published()
            .filter(positions__id=position_id)
            .order_by("-version")
            .first()
        )
        if direct is not None:
            return direct

    if grade_id is not None:
        return (
            CompetencyModel.objects.published()
            .filter(grade_id=grade_id)
            .order_by("-version")
            .first()
        )

    return None


def model_for_employee(employee: Employee) -> CompetencyModel | None:
    """Модель компетенций, по которой оценивается сотрудник на своей должности."""
    position = employee.position
    if position is None:
        return None
    return resolve_competency_model(position_id=position.pk, grade_id=position.grade_id)


# ---------------------------------------------------------------------------
# Построение профиля
# ---------------------------------------------------------------------------
def _requirements_of(competency_model: CompetencyModel) -> list[CompetencyRequirement]:
    """Требования модели с предзагруженными компетенциями и категориями."""
    return list(competency_model.requirements.with_competency().all())


def build_profile(
    employee: Employee,
    competency_model: CompetencyModel | None = None,
    *,
    on_date: dt.date | None = None,
    campaign_id: int | None = None,
) -> CompetencyProfile:
    """Строит профиль компетенций сотрудника относительно модели требований."""
    from django.utils import timezone

    resolved = competency_model if competency_model is not None else model_for_employee(employee)
    evaluated_on = on_date or timezone.localdate()

    if resolved is None:
        return CompetencyProfile(
            employee_id=employee.pk,
            competency_model=None,
            gaps=(),
            evaluated_on=evaluated_on,
        )

    requirements = _requirements_of(resolved)
    weights = resolved.normalized_weights()
    levels = employee_levels(
        employee.pk,
        on_date=on_date,
        competency_ids=[requirement.competency_id for requirement in requirements],
        campaign_id=campaign_id,
    )

    gaps = tuple(
        RequirementGap(
            requirement=requirement,
            measured=levels.get(requirement.competency_id),
            weight=weights.get(requirement.competency_id, Decimal("0")),
        )
        for requirement in requirements
    )

    return CompetencyProfile(
        employee_id=employee.pk,
        competency_model=resolved,
        gaps=gaps,
        evaluated_on=evaluated_on,
    )


def build_profiles(
    employees: Sequence[Employee],
    competency_model: CompetencyModel,
    *,
    on_date: dt.date | None = None,
) -> dict[int, CompetencyProfile]:
    """Строит профили списка сотрудников по одной модели требований."""
    from django.utils import timezone

    evaluated_on = on_date or timezone.localdate()
    requirements = _requirements_of(competency_model)
    weights = competency_model.normalized_weights()
    competency_ids = [requirement.competency_id for requirement in requirements]

    measured = team_levels(
        [employee.pk for employee in employees],
        on_date=on_date,
        competency_ids=competency_ids,
    )

    profiles: dict[int, CompetencyProfile] = {}
    for employee in employees:
        levels = measured.get(employee.pk, {})
        profiles[employee.pk] = CompetencyProfile(
            employee_id=employee.pk,
            competency_model=competency_model,
            gaps=tuple(
                RequirementGap(
                    requirement=requirement,
                    measured=levels.get(requirement.competency_id),
                    weight=weights.get(requirement.competency_id, Decimal("0")),
                )
                for requirement in requirements
            ),
            evaluated_on=evaluated_on,
        )
    return profiles


# ---------------------------------------------------------------------------
# Матрица разрывов подразделения
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GapMatrix:
    """Матрица «сотрудники × компетенции» с разрывами в ячейках."""

    competency_model: CompetencyModel
    employees: tuple[Employee, ...]
    profiles: dict[int, CompetencyProfile]
    requirements: tuple[CompetencyRequirement, ...]

    def rows(self) -> list[dict]:
        """Строки матрицы: сотрудник и его разрывы в порядке требований."""
        result: list[dict[str, Any]] = []
        for employee in self.employees:
            profile = self.profiles[employee.pk]
            by_competency = {gap.requirement.competency_id: gap for gap in profile.gaps}
            result.append(
                {
                    "employee": employee,
                    "profile": profile,
                    "cells": [
                        by_competency.get(requirement.competency_id)
                        for requirement in self.requirements
                    ],
                }
            )

        # Ключ сортировки вычисляется отдельной функцией, а не выражением в лямбде: словарь строки
        # содержит значения разных типов, и обращение к полю профиля прямо в ключе невыразимо в
        # системе типов.
        def by_match(row: dict) -> float:
            profile: CompetencyProfile = row["profile"]
            return -profile.match_score

        result.sort(key=by_match)
        return result

    def columns(self) -> list[dict]:
        """Столбцы матрицы: компетенция и её состояние по команде целиком."""
        total = len(self.employees) or 1
        result: list[dict[str, Any]] = []
        for requirement in self.requirements:
            gaps = [
                gap
                for profile in self.profiles.values()
                for gap in profile.gaps
                if gap.requirement.competency_id == requirement.competency_id
            ]
            levels = [gap.actual for gap in gaps if gap.actual is not None]
            failing = sum(1 for gap in gaps if not gap.is_satisfied)
            result.append(
                {
                    "requirement": requirement,
                    "competency": requirement.competency,
                    "target": requirement.target_level,
                    "average_level": round(sum(levels) / len(levels), 2) if levels else None,
                    "failing": failing,
                    "failing_share": round(failing * 100 / total, 1),
                    "assessed_share": round(len(levels) * 100 / total, 1),
                }
            )
        result.sort(key=lambda item: -item["failing_share"])
        return result


def build_gap_matrix(
    employees: Sequence[Employee],
    competency_model: CompetencyModel,
    *,
    on_date: dt.date | None = None,
) -> GapMatrix:
    """Собирает матрицу разрывов для списка сотрудников."""
    profiles = build_profiles(employees, competency_model, on_date=on_date)
    return GapMatrix(
        competency_model=competency_model,
        employees=tuple(employees),
        profiles=profiles,
        requirements=tuple(_requirements_of(competency_model)),
    )


# ---------------------------------------------------------------------------
# Лепестковая диаграмма
# ---------------------------------------------------------------------------
#: Наибольшее число осей диаграммы.
MAX_RADAR_AXES = 12


@dataclass(frozen=True, slots=True)
class RadarAxis:
    """Ось лепестковой диаграммы: одна компетенция."""

    label: str
    short_label: str
    actual: float | None
    target: float
    angle: float
    label_x: float
    label_y: float
    anchor: str


@dataclass(frozen=True, slots=True)
class RadarChart:
    """Готовые к отрисовке данные лепестковой диаграммы."""

    size: int
    center: float
    radius: float
    axes: tuple[RadarAxis, ...]
    rings: tuple[float, ...]
    actual_points: str
    target_points: str
    scale_min: int
    scale_max: int

    @property
    def has_actual(self) -> bool:
        return bool(self.actual_points)


def _polar(center: float, radius: float, angle: float) -> tuple[float, float]:
    """Переводит полярные координаты в декартовы для системы координат SVG."""
    x = center + radius * math.sin(angle)
    y = center - radius * math.cos(angle)
    return round(x, 2), round(y, 2)


def build_radar(
    profile: CompetencyProfile,
    *,
    size: int = 320,
    max_axes: int = MAX_RADAR_AXES,
) -> RadarChart | None:
    """Строит лепестковую диаграмму профиля: факт на фоне требований."""
    if not profile.gaps:
        return None

    options = get_reservix_settings()
    scale_min = options["COMPETENCY_SCALE_MIN"]
    scale_max = options["COMPETENCY_SCALE_MAX"]
    span = scale_max - scale_min or 1

    selected = sorted(profile.gaps, key=lambda gap: (-gap.weight, gap.competency.name))[:max_axes]
    # Порядок осей возвращается к порядку категорий: диаграмма, на которой
    # управленческие компетенции перемешаны с профессиональными, не читается.
    selected.sort(
        key=lambda gap: (
            gap.competency.category.sort_order,
            gap.competency.sort_order,
            gap.competency.name,
        )
    )

    center = size / 2
    radius = center * 0.68
    step = 2 * math.pi / len(selected)

    axes: list[RadarAxis] = []
    actual_points: list[str] = []
    target_points: list[str] = []
    complete = True

    for index, gap in enumerate(selected):
        angle = index * step
        target_ratio = (gap.target - scale_min) / span
        target_x, target_y = _polar(center, radius * target_ratio, angle)
        target_points.append(f"{target_x},{target_y}")

        if gap.actual is None:
            complete = False
        else:
            actual_ratio = (gap.actual - scale_min) / span
            actual_x, actual_y = _polar(center, radius * actual_ratio, angle)
            actual_points.append(f"{actual_x},{actual_y}")

        label_x, label_y = _polar(center, radius + 14, angle)
        axes.append(
            RadarAxis(
                label=gap.competency.name,
                short_label=_abbreviate(gap.competency.name),
                actual=gap.actual,
                target=float(gap.target),
                angle=round(angle, 4),
                label_x=label_x,
                label_y=label_y,
                anchor=_anchor_for(angle),
            )
        )

    return RadarChart(
        size=size,
        center=center,
        radius=radius,
        axes=tuple(axes),
        rings=tuple(
            round(radius * (level - scale_min) / span, 2)
            for level in range(scale_min, scale_max + 1)
        ),
        # Незамкнутый контур не рисуется: ломаная по части осей создаёт ложное
        # впечатление провала там, где оценки просто нет.
        actual_points=" ".join(actual_points) if complete else "",
        target_points=" ".join(target_points),
        scale_min=scale_min,
        scale_max=scale_max,
    )


def _anchor_for(angle: float) -> str:
    """Выравнивание подписи оси относительно её конца."""
    epsilon = 0.15
    normalized = angle % (2 * math.pi)
    if abs(normalized) < epsilon or abs(normalized - math.pi) < epsilon:
        return "middle"
    return "start" if normalized < math.pi else "end"


def _abbreviate(name: str, limit: int = 18) -> str:
    """Сокращает наименование компетенции до размера подписи оси."""
    if len(name) <= limit:
        return name
    words = name.split()
    if len(words) > 1 and len(words[0]) <= limit:
        return words[0] + "…"
    return name[: limit - 1] + "…"
