"""Сведение оценок в уровень владения компетенцией."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from django.utils import timezone

from apps.assessment.models import AssessmentMethod, AssessmentScore
from core.utils import shift_months

#: Степень доверия к методу оценки.
METHOD_WEIGHTS: dict[str, Decimal] = {
    AssessmentMethod.TEST: Decimal("0.25"),
    AssessmentMethod.EXAM: Decimal("0.20"),
    AssessmentMethod.ASSESSMENT_CENTER: Decimal("0.20"),
    AssessmentMethod.MANAGER: Decimal("0.20"),
    AssessmentMethod.EXPERT: Decimal("0.15"),
    AssessmentMethod.INTERVIEW: Decimal("0.15"),
    AssessmentMethod.ASSESSMENT_360: Decimal("0.15"),
    AssessmentMethod.SELF: Decimal("0.05"),
}

#: Вес метода, отсутствующего в перечне. Значение намеренно мало: неизвестный
#: способ получения балла не может весить столько же, сколько проверяемый.
DEFAULT_METHOD_WEIGHT = Decimal("0.05")

#: Окно актуальности наблюдений, месяцев.
DEFAULT_WINDOW_MONTHS = 24

#: Достоверность, ниже которой сведённый уровень считается предварительным.
#: Соответствует ситуации «есть только самооценка либо одно слабое суждение».
LOW_CONFIDENCE_THRESHOLD = 0.25


@dataclass(frozen=True, slots=True)
class Observation:
    """Одно наблюдение уровня компетенции."""

    competency_id: int
    method: str
    level: float
    assessment_date: dt.date
    session_id: int
    comment: str = ""

    @property
    def weight(self) -> Decimal:
        """Вес метода наблюдения."""
        return METHOD_WEIGHTS.get(self.method, DEFAULT_METHOD_WEIGHT)


@dataclass(frozen=True, slots=True)
class CompetencyLevel:
    """Сведённый уровень владения одной компетенцией."""

    competency_id: int
    level: float
    observations: tuple[Observation, ...]
    confidence: float

    @property
    def method_count(self) -> int:
        """Число различных методов, подтверждающих уровень."""
        return len({observation.method for observation in self.observations})

    @property
    def latest_date(self) -> dt.date:
        """Дата самого свежего наблюдения."""
        return max(observation.assessment_date for observation in self.observations)

    @property
    def self_level(self) -> float | None:
        """Уровень по самооценке, если сотрудник оценивал себя."""
        return self._level_of(AssessmentMethod.SELF)

    @property
    def external_level(self) -> float | None:
        """Уровень по внешним наблюдениям — без самооценки."""
        external = [
            observation
            for observation in self.observations
            if observation.method != AssessmentMethod.SELF
        ]
        if not external:
            return None
        return _weighted_mean(external)

    @property
    def self_bias(self) -> float | None:
        """Расхождение самооценки с внешней оценкой."""
        own = self.self_level
        external = self.external_level
        if own is None or external is None:
            return None
        return round(own - external, 2)

    @property
    def is_low_confidence(self) -> bool:
        """Признак недостаточно подтверждённого уровня."""
        return self.confidence < LOW_CONFIDENCE_THRESHOLD

    def _level_of(self, method: str) -> float | None:
        """Уровень по конкретному методу, если наблюдение есть."""
        for observation in self.observations:
            if observation.method == method:
                return observation.level
        return None


# ---------------------------------------------------------------------------
# Расчёт
# ---------------------------------------------------------------------------
def _weighted_mean(observations: Sequence[Observation]) -> float:
    """Взвешенное среднее наблюдений по весам методов."""
    total_weight = sum((observation.weight for observation in observations), Decimal("0"))
    if total_weight <= 0:
        # Вырожденный случай: веса всех методов обнулены настройкой. Тогда
        # взвешенное среднее вырождается в обычное — терять наблюдения нельзя.
        return round(sum(o.level for o in observations) / len(observations), 2)

    weighted = sum(
        (Decimal(str(observation.level)) * observation.weight for observation in observations),
        Decimal("0"),
    )
    return round(float(weighted / total_weight), 2)


def consolidate(observations: Iterable[Observation]) -> dict[int, CompetencyLevel]:
    """Сводит наблюдения в уровень владения по каждой компетенции."""
    by_competency: dict[int, dict[str, Observation]] = {}

    for observation in observations:
        methods = by_competency.setdefault(observation.competency_id, {})
        previous = methods.get(observation.method)
        # Внутри метода побеждает более позднее наблюдение: повторная оценка
        # тем же способом уточняет прежнюю, а не дополняет её.
        if previous is None or observation.assessment_date >= previous.assessment_date:
            methods[observation.method] = observation

    total_possible = sum(METHOD_WEIGHTS.values())
    result: dict[int, CompetencyLevel] = {}

    for competency_id, methods in by_competency.items():
        selected = tuple(
            sorted(methods.values(), key=lambda item: (-float(item.weight), item.method))
        )
        present_weight = sum((item.weight for item in selected), Decimal("0"))
        confidence = (
            float(min(Decimal("1"), present_weight / total_possible)) if total_possible else 0.0
        )
        result[competency_id] = CompetencyLevel(
            competency_id=competency_id,
            level=_weighted_mean(selected),
            observations=selected,
            confidence=round(confidence, 3),
        )

    return result


# ---------------------------------------------------------------------------
# Загрузка наблюдений
# ---------------------------------------------------------------------------
def _window_start(on_date: dt.date, window_months: int) -> dt.date:
    """Начало окна актуальности."""
    return shift_months(on_date, -window_months)


def load_observations(
    employee_ids: Sequence[int],
    *,
    on_date: dt.date | None = None,
    window_months: int = DEFAULT_WINDOW_MONTHS,
    competency_ids: Sequence[int] | None = None,
    campaign_id: int | None = None,
) -> dict[int, list[Observation]]:
    """Читает наблюдения по сотрудникам одним запросом."""
    if not employee_ids:
        return {}

    today = on_date or timezone.localdate()
    queryset = (
        AssessmentScore.objects.counted()
        .filter(
            session__employee_id__in=employee_ids,
            session__assessment_date__lte=today,
            session__assessment_date__gte=_window_start(today, window_months),
        )
        .values_list(
            "session__employee_id",
            "competency_id",
            "session__method",
            "level",
            "session__assessment_date",
            "session_id",
            "comment",
        )
    )

    if competency_ids is not None:
        queryset = queryset.filter(competency_id__in=competency_ids)
    if campaign_id is not None:
        queryset = queryset.filter(session__campaign_id=campaign_id)

    grouped: dict[int, list[Observation]] = {employee_id: [] for employee_id in employee_ids}
    for employee_id, competency_id, method, level, date, session_id, comment in queryset:
        grouped[employee_id].append(
            Observation(
                competency_id=competency_id,
                method=method,
                level=float(level),
                assessment_date=date,
                session_id=session_id,
                comment=comment,
            )
        )

    return grouped


def employee_levels(
    employee_id: int,
    *,
    on_date: dt.date | None = None,
    window_months: int = DEFAULT_WINDOW_MONTHS,
    competency_ids: Sequence[int] | None = None,
    campaign_id: int | None = None,
) -> dict[int, CompetencyLevel]:
    """Сведённые уровни владения компетенциями одного сотрудника."""
    observations = load_observations(
        [employee_id],
        on_date=on_date,
        window_months=window_months,
        competency_ids=competency_ids,
        campaign_id=campaign_id,
    )
    return consolidate(observations.get(employee_id, []))


def team_levels(
    employee_ids: Sequence[int],
    *,
    on_date: dt.date | None = None,
    window_months: int = DEFAULT_WINDOW_MONTHS,
    competency_ids: Sequence[int] | None = None,
) -> dict[int, dict[int, CompetencyLevel]]:
    """Сведённые уровни для списка сотрудников — основа матрицы разрывов."""
    observations = load_observations(
        employee_ids,
        on_date=on_date,
        window_months=window_months,
        competency_ids=competency_ids,
    )
    return {employee_id: consolidate(items) for employee_id, items in observations.items()}
