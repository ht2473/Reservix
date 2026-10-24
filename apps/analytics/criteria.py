"""Частные критерии индекса готовности: от данных системы к шкале 0–100."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from django.db.models import Avg, Count, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.assessment.models import (
    Assessment360Respondent,
    AssessmentMethod,
    AssessmentScore,
    AttemptStatus,
    RespondentRelation,
    TestAttempt,
)
from apps.competencies.services import CompetencyProfile, build_profiles, resolve_competency_model
from apps.development.models import ActivityStatus, DevelopmentActivity
from apps.org.models import PositionProfile
from apps.people.models import EmploymentHistory, Mobility
from core.settings_types import get_reservix_settings
from core.utils import clamp, normalize_to_scale, shift_months

# ---------------------------------------------------------------------------
# Коды критериев
# ---------------------------------------------------------------------------
#: Идентификаторы источников данных.
SOURCE_COMPETENCY = "competency_match"
SOURCE_PERFORMANCE = "performance"
SOURCE_POTENTIAL = "potential"
SOURCE_DEVELOPMENT = "development_plan"
SOURCE_EXPERIENCE = "experience"
SOURCE_FEEDBACK_360 = "feedback_360"

#: Порядок критериев в методике.
CRITERION_ORDER: tuple[str, ...] = (
    SOURCE_COMPETENCY,
    SOURCE_PERFORMANCE,
    SOURCE_POTENTIAL,
    SOURCE_DEVELOPMENT,
    SOURCE_EXPERIENCE,
    SOURCE_FEEDBACK_360,
)

#: Окно актуальности исходных данных, месяцев.
WINDOW_MONTHS = 24

#: Вес мнения группы респондентов при сведении оценки 360°.
RELATION_WEIGHTS: dict[str, float] = {
    RespondentRelation.MANAGER: 0.40,
    RespondentRelation.PEER: 0.25,
    RespondentRelation.SUBORDINATE: 0.25,
    RespondentRelation.EXTERNAL: 0.10,
}

#: Доля зачёта мероприятия плана развития, выполненного после срока.
LATE_COMPLETION_CREDIT = 0.7

#: Норма организации по кросс-функциональному опыту: работа в трёх и более
#: подразделениях считается полной кросс-функциональной подготовкой.
CROSS_FUNCTIONAL_TARGET = 3

#: Норма совокупного профессионального опыта, месяцев, применяемая тогда, когда профиль целевой
#: должности не заполнен.
EXPERIENCE_NORM_MONTHS = 120

#: Соответствие готовности к перемещению баллам шкалы 0–100.
MOBILITY_SCORE: dict[str, float] = {
    Mobility.NONE: 0.0,
    Mobility.CITY: 40.0,
    Mobility.REGION: 70.0,
    Mobility.COUNTRY: 100.0,
}


# ---------------------------------------------------------------------------
# Результат расчёта частного критерия
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CriterionValue:
    """Значение частного критерия для одного резервиста."""

    source: str
    raw: float | None
    normalized: float
    has_data: bool
    unit: str = ""
    note: str = ""
    components: tuple[tuple[str, float], ...] = ()

    @classmethod
    def missing(cls, source: str, note: str) -> CriterionValue:
        """Значение критерия, для которого нет исходных данных."""
        return cls(source=source, raw=None, normalized=0.0, has_data=False, note=note)

    def as_dict(self) -> dict[str, object]:
        """Представление для хранения в снимке расчёта."""
        return {
            "raw": round(self.raw, 2) if self.raw is not None else None,
            "normalized": round(self.normalized, 2),
            "has_data": self.has_data,
            "unit": self.unit,
            "note": self.note,
            "components": [
                {"label": label, "value": round(value, 2)} for label, value in self.components
            ],
        }


# ---------------------------------------------------------------------------
# Контекст расчёта
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class CriterionContext:
    """Исходные данные, загруженные разом для всей группы резервистов."""

    on_date: dt.date
    #: Профили соответствия целевой должности, ключ — идентификатор записи резерва.
    target_profiles: dict[int, CompetencyProfile] = field(default_factory=dict)
    #: Профили соответствия собственной должности, ключ — сотрудник.
    own_profiles: dict[int, CompetencyProfile] = field(default_factory=dict)
    #: Действующая оценка результативности из матрицы 9-box, ключ — сотрудник.
    performance: dict[int, float] = field(default_factory=dict)
    #: Подтверждена ли оценка результативности человеком: размещение выставлено руководителем либо
    #: скорректировано на калибровке.
    performance_confirmed: dict[int, bool] = field(default_factory=dict)
    #: Средний результат тестирования, проценты; ключ — сотрудник.
    test_scores: dict[int, float] = field(default_factory=dict)
    #: Сведение оценки 360° по группам респондентов: сотрудник → отношение →
    #: (средний балл 0–100, число ответивших).
    feedback_360: dict[int, dict[str, tuple[float, int]]] = field(default_factory=dict)
    #: Исполнение плана развития: сотрудник → (зачтённый вес, учтённый вес).
    development: dict[int, tuple[float, float]] = field(default_factory=dict)
    #: Профили целевых должностей, ключ — должность.
    position_profiles: dict[int, PositionProfile] = field(default_factory=dict)
    #: Число подразделений в кадровой истории, ключ — сотрудник.
    units_worked: dict[int, int] = field(default_factory=dict)


def load_context(entries: Sequence, *, on_date: dt.date | None = None) -> CriterionContext:
    """Загружает исходные данные всех критериев для набора записей резерва."""
    from apps.analytics.models import NineBoxPlacement

    moment = on_date or timezone.localdate()
    context = CriterionContext(on_date=moment)
    if not entries:
        return context

    employees = {entry.employee_id: entry.employee for entry in entries}
    employee_ids = list(employees)
    window_start = shift_months(moment, -WINDOW_MONTHS)

    _load_target_profiles(entries, context, on_date=moment)
    _load_own_profiles(list(employees.values()), context, on_date=moment)

    # -- Результативность: действующее размещение в матрице 9-box ------------
    for placement in NineBoxPlacement.objects.filter(
        employee_id__in=employee_ids, is_current=True
    ).values_list("employee_id", "performance", "placed_by_id", "is_adjusted"):
        employee_id, value, placed_by_id, is_adjusted = placement
        context.performance[employee_id] = float(value)
        # Автор размещения пуст у предварительной оценки, выставленной системой.
        context.performance_confirmed[employee_id] = bool(is_adjusted or placed_by_id)

    # -- Тестирование --------------------------------------------------------
    for employee_id, mean_score in (
        TestAttempt.objects.filter(
            employee_id__in=employee_ids,
            status=AttemptStatus.CHECKED,
            started_at__date__gte=window_start,
            score__isnull=False,
        )
        .values("employee_id")
        .annotate(mean=Avg("score"))
        .values_list("employee_id", "mean")
    ):
        context.test_scores[employee_id] = float(mean_score)

    _load_feedback_360(employee_ids, context, window_start=window_start)
    _load_development(employee_ids, context, on_date=moment)

    # -- Профили целевых должностей -----------------------------------------
    position_ids = {entry.target_position_id for entry in entries}
    context.position_profiles = {
        profile.position_id: profile
        for profile in PositionProfile.objects.filter(position_id__in=position_ids)
    }

    # -- Кросс-функциональный опыт ------------------------------------------
    for employee_id, units in (
        EmploymentHistory.objects.filter(employee_id__in=employee_ids, department__isnull=False)
        .values("employee_id")
        .annotate(units=Count("department", distinct=True))
        .values_list("employee_id", "units")
    ):
        context.units_worked[employee_id] = units

    return context


def _load_target_profiles(
    entries: Sequence, context: CriterionContext, *, on_date: dt.date
) -> None:
    """Строит профили соответствия целевым должностям, группируя по должности."""
    by_position: dict[int, list] = {}
    for entry in entries:
        by_position.setdefault(entry.target_position_id, []).append(entry)

    for position_id, group in by_position.items():
        position = group[0].target_position
        model = resolve_competency_model(position_id=position_id, grade_id=position.grade_id)
        if model is None:
            continue
        profiles = build_profiles([entry.employee for entry in group], model, on_date=on_date)
        for entry in group:
            profile = profiles.get(entry.employee_id)
            if profile is not None:
                context.target_profiles[entry.pk] = profile


def _load_own_profiles(employees: Sequence, context: CriterionContext, *, on_date: dt.date) -> None:
    """Строит профили соответствия собственным должностям сотрудников."""
    by_position: dict[int, list] = {}
    for employee in employees:
        if employee.position_id is not None:
            by_position.setdefault(employee.position_id, []).append(employee)

    for position_id, group in by_position.items():
        position = group[0].position
        model = resolve_competency_model(position_id=position_id, grade_id=position.grade_id)
        if model is None:
            continue
        profiles = build_profiles(group, model, on_date=on_date)
        context.own_profiles.update(profiles)


def _load_feedback_360(
    employee_ids: Sequence[int], context: CriterionContext, *, window_start: dt.date
) -> None:
    """Сводит оценки 360° по группам респондентов."""
    relations = {
        (row["employee_id"], row["session_id"]): row["relation"]
        for row in Assessment360Respondent.objects.filter(
            employee_id__in=employee_ids, session_id__isnull=False
        ).values("employee_id", "session_id", "relation")
    }
    if not relations:
        return

    session_ids = [session_id for _employee_id, session_id in relations]
    grouped: dict[int, dict[str, list[float]]] = {}

    # Отбор сессий — общий для системы (`AssessmentScore.objects.counted()`): заполненная и
    # утверждённая.
    for session_id, employee_id, mean_level in (
        AssessmentScore.objects.counted()
        .filter(
            session_id__in=session_ids,
            session__assessment_date__gte=window_start,
            session__method=AssessmentMethod.ASSESSMENT_360,
        )
        .values("session_id", "session__employee_id")
        .annotate(mean=Avg("level"))
        .values_list("session_id", "session__employee_id", "mean")
    ):
        relation = relations.get((employee_id, session_id))
        if relation is None or mean_level is None:
            continue
        score = float(AssessmentScore.level_to_score(float(mean_level)))
        grouped.setdefault(employee_id, {}).setdefault(relation, []).append(score)

    for employee_id, by_relation in grouped.items():
        context.feedback_360[employee_id] = {
            relation: (sum(values) / len(values), len(values))
            for relation, values in by_relation.items()
        }


def _load_development(
    employee_ids: Sequence[int], context: CriterionContext, *, on_date: dt.date
) -> None:
    """Считает исполнение планов развития с учётом сложности и просрочек."""
    rows = (
        DevelopmentActivity.objects.filter(plan__employee_id__in=employee_ids)
        .exclude(status=ActivityStatus.CANCELLED)
        .filter(Q(due_date__lte=on_date) | Q(status=ActivityStatus.COMPLETED))
        .values("plan__employee_id", "status", "due_date", "completion_date", "weight")
    )

    totals: dict[int, list[float]] = {}
    for row in rows:
        employee_id = row["plan__employee_id"]
        weight = float(row["weight"] or 1)
        earned = 0.0
        if row["status"] == ActivityStatus.COMPLETED:
            completed_on = row["completion_date"]
            on_time = completed_on is None or completed_on <= row["due_date"]
            earned = weight if on_time else weight * LATE_COMPLETION_CREDIT
        bucket = totals.setdefault(employee_id, [0.0, 0.0])
        bucket[0] += earned
        bucket[1] += weight

    context.development = {
        employee_id: (earned, total) for employee_id, (earned, total) in totals.items() if total > 0
    }


# ---------------------------------------------------------------------------
# Расчёт частных критериев
# ---------------------------------------------------------------------------
def growth_headroom(profile: CompetencyProfile) -> float | None:
    """Запас роста над требованиями собственной должности, шкала 0–100."""
    if not profile.gaps or profile.coverage <= 0:
        return None
    level = profile.average_level
    target = profile.average_target
    if level is None or target is None:
        return None
    return normalize_to_scale(level - target, -1.0, 1.0)


def demonstrated_level(profile: CompetencyProfile) -> float | None:
    """Средний подтверждённый уровень компетенций в шкале 0–100."""
    if not profile.gaps or profile.coverage <= 0 or profile.average_level is None:
        return None
    options = get_reservix_settings()
    return normalize_to_scale(
        profile.average_level, options["COMPETENCY_SCALE_MIN"], options["COMPETENCY_SCALE_MAX"]
    )


def competency_match(entry, context: CriterionContext) -> CriterionValue:
    """K1. Соответствие компетенций требованиям целевой должности."""
    profile = context.target_profiles.get(entry.pk)
    if profile is None or not profile.gaps:
        return CriterionValue.missing(
            SOURCE_COMPETENCY,
            str(_("Для целевой должности не определена модель компетенций")),
        )
    if profile.coverage <= 0:
        return CriterionValue.missing(
            SOURCE_COMPETENCY,
            str(_("Ни одно требование должности не подтверждено оценкой")),
        )

    score = profile.match_score
    return CriterionValue(
        source=SOURCE_COMPETENCY,
        raw=score,
        normalized=clamp(score),
        has_data=True,
        unit=str(_("баллы")),
        note=str(_("Оценкой покрыто %(share).0f %% требований")) % {"share": profile.coverage},
        components=(
            (str(_("Соответствие требованиям")), score),
            (str(_("Покрытие оценкой")), profile.coverage),
        ),
    )


def performance(entry, context: CriterionContext) -> CriterionValue:
    """K2. Результативность за оцениваемый период."""
    value = context.performance.get(entry.employee_id)
    if value is None:
        return CriterionValue.missing(
            SOURCE_PERFORMANCE,
            str(_("Сотрудник не размещён в матрице результативности и потенциала")),
        )
    confirmed = context.performance_confirmed.get(entry.employee_id, False)
    return CriterionValue(
        source=SOURCE_PERFORMANCE,
        raw=value,
        normalized=clamp(value),
        has_data=True,
        unit=str(_("баллы")),
        note=(
            str(_("Оценка подтверждена руководителем"))
            if confirmed
            else str(_("Предварительное размещение по данным системы"))
        ),
    )


def potential(entry, context: CriterionContext) -> CriterionValue:
    """K3. Потенциал: обучаемость и запас роста."""
    parts: list[tuple[str, float, float]] = []

    test_score = context.test_scores.get(entry.employee_id)
    if test_score is not None:
        parts.append((str(_("Обучаемость по тестам")), clamp(test_score), 0.45))

    own_profile = context.own_profiles.get(entry.employee_id)
    headroom = growth_headroom(own_profile) if own_profile is not None else None
    if headroom is not None:
        parts.append((str(_("Запас роста к своей должности")), headroom, 0.35))

    mobility_score = MOBILITY_SCORE.get(entry.employee.mobility)
    if mobility_score is not None:
        parts.append((str(_("Готовность к перемещению")), mobility_score, 0.20))

    if not parts:
        return CriterionValue.missing(
            SOURCE_POTENTIAL, str(_("Нет данных тестирования и оценки собственной должности"))
        )

    total_weight = sum(weight for _label, _value, weight in parts)
    score = sum(value * weight for _label, value, weight in parts) / total_weight

    return CriterionValue(
        source=SOURCE_POTENTIAL,
        raw=score,
        normalized=clamp(score),
        has_data=True,
        unit=str(_("баллы")),
        note=str(_("Учтено слагаемых: %(count)d из 3")) % {"count": len(parts)},
        components=tuple((label, value) for label, value, _weight in parts),
    )


def development_progress(entry, context: CriterionContext) -> CriterionValue:
    """K4. Исполнение индивидуального плана развития."""
    measured = context.development.get(entry.employee_id)
    if measured is None:
        return CriterionValue.missing(
            SOURCE_DEVELOPMENT,
            str(_("План развития не составлен либо срок мероприятий не наступил")),
        )

    earned, total = measured
    share = earned / total if total > 0 else 0.0
    return CriterionValue(
        source=SOURCE_DEVELOPMENT,
        raw=round(share, 4),
        normalized=clamp(share * 100),
        has_data=True,
        unit=str(_("доля")),
        note=str(_("Зачтено %(earned).1f из %(total).1f единиц сложности"))
        % {"earned": earned, "total": total},
    )


def experience(entry, context: CriterionContext) -> CriterionValue:
    """K5. Профессиональный опыт и формальное соответствие профилю должности."""
    employee = entry.employee
    parts: list[tuple[str, float, float]] = []

    profile = context.position_profiles.get(entry.target_position_id)
    if profile is not None:
        parts.append(
            (
                str(_("Формальные требования профиля")),
                clamp(employee.profile_match_ratio(profile) * 100),
                0.50,
            )
        )

    required_months = (
        profile.min_total_experience_years * 12
        if profile is not None and profile.min_total_experience_years > 0
        else EXPERIENCE_NORM_MONTHS
    )
    ratio = employee.total_experience_months / required_months
    parts.append((str(_("Достаточность стажа")), clamp(min(ratio, 1.0) * 100), 0.30))

    units = context.units_worked.get(employee.pk, 1 if employee.department_id else 0)
    cross = clamp((max(units, 1) - 1) / max(CROSS_FUNCTIONAL_TARGET - 1, 1) * 100)
    parts.append((str(_("Кросс-функциональный опыт")), cross, 0.20))

    total_weight = sum(weight for _label, _value, weight in parts)
    score = sum(value * weight for _label, value, weight in parts) / total_weight

    return CriterionValue(
        source=SOURCE_EXPERIENCE,
        raw=float(employee.total_experience_months),
        normalized=clamp(score),
        has_data=True,
        unit=str(_("месяцы опыта")),
        note=str(_("Совокупный опыт — %(months)d мес."))
        % {"months": employee.total_experience_months},
        components=tuple((label, value) for label, value, _weight in parts),
    )


def feedback_360(entry, context: CriterionContext) -> CriterionValue:
    """K6. Оценка 360° со сведением по группам респондентов."""
    by_relation = context.feedback_360.get(entry.employee_id)
    if not by_relation:
        return CriterionValue.missing(
            SOURCE_FEEDBACK_360, str(_("Оценка 360° не проводилась либо результаты не утверждены"))
        )

    scored = [
        (relation, value, count, RELATION_WEIGHTS[relation])
        for relation, (value, count) in by_relation.items()
        if relation in RELATION_WEIGHTS
    ]
    if not scored:
        return CriterionValue.missing(
            SOURCE_FEEDBACK_360, str(_("В опросе 360° ответила только самооценка"))
        )

    total_weight = sum(weight for _relation, _value, _count, weight in scored)
    score = sum(value * weight for _relation, value, _count, weight in scored) / total_weight
    respondents = sum(count for _relation, _value, count, _weight in scored)

    labels = dict(RespondentRelation.choices)
    return CriterionValue(
        source=SOURCE_FEEDBACK_360,
        raw=score,
        normalized=clamp(score),
        has_data=True,
        unit=str(_("баллы")),
        note=str(_("Групп респондентов: %(groups)d, ответов: %(answers)d"))
        % {"groups": len(scored), "answers": respondents},
        components=tuple(
            (str(labels.get(relation, relation)), value)
            for relation, value, _count, _weight in scored
        ),
    )


# ---------------------------------------------------------------------------
# Реестр
# ---------------------------------------------------------------------------
#: Тип функции расчёта частного критерия.
Evaluator = Callable[[object, CriterionContext], CriterionValue]

#: Связь идентификатора источника с функцией расчёта.
EVALUATORS: dict[str, Evaluator] = {
    SOURCE_COMPETENCY: competency_match,
    SOURCE_PERFORMANCE: performance,
    SOURCE_POTENTIAL: potential,
    SOURCE_DEVELOPMENT: development_progress,
    SOURCE_EXPERIENCE: experience,
    SOURCE_FEEDBACK_360: feedback_360,
}


def evaluate(source: str, entry, context: CriterionContext) -> CriterionValue:
    """Вычисляет частный критерий по идентификатору источника."""
    evaluator = EVALUATORS.get(source)
    if evaluator is None:
        return CriterionValue.missing(
            source, str(_("Источник данных «%(source)s» не реализован")) % {"source": source}
        )
    return evaluator(entry, context)


def known_sources() -> tuple[str, ...]:
    """Идентификаторы реализованных источников в порядке методики."""
    return CRITERION_ORDER
