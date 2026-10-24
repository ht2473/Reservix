"""Методы аналитического ядра."""

from decimal import Decimal

from ninja import Query, Router
from ninja.errors import HttpError
from ninja.pagination import paginate

from django.db.models import Avg, Count
from django.http import HttpRequest
from django.shortcuts import get_object_or_404

from apps.analytics.models import (
    NineBoxPlacement,
    ReadinessSnapshot,
    ScoringCriterion,
    ScoringScheme,
)
from apps.api.auth import current_user
from apps.api.schemas import (
    NineBoxDistributionSchema,
    ReadinessBreakdownSchema,
    ReadinessForecastSchema,
    ReadinessSnapshotSchema,
    ReserveMetricsSchema,
    SchemeComparisonSchema,
    ScoringCriterionSchema,
    ScoringSchemeSchema,
    StabilityReportSchema,
)
from apps.people.models import Employee
from apps.reserve.models import ACTIVE_RESERVE_STATUSES, ReserveEntry
from apps.reserve.services import reserve_funnel as reserve_funnel_of
from apps.succession.models import Appointment, AppointmentKind, KeyPosition
from core.constants import NineBoxCell, ReadinessLevel
from core.permissions import apply_employee_scope, apply_scope_by_path

router = Router(tags=["Аналитика"])


@router.get(
    "/criteria",
    response=list[ScoringCriterionSchema],
    summary="Критерии готовности",
    description=(
        "Частные показатели, из которых складывается композитный индекс "
        "готовности резервиста. Каждый нормируется к диапазону 0–100 и имеет "
        "собственный источник данных."
    ),
)
def list_criteria(request: HttpRequest, active: bool | None = None) -> list[ScoringCriterion]:
    """Возвращает справочник частных критериев готовности."""
    queryset = ScoringCriterion.objects.all()
    if active is not None:
        queryset = queryset.filter(is_active=active)
    return list(queryset)


@router.get(
    "/schemes",
    response=list[ScoringSchemeSchema],
    summary="Схемы оценивания",
    description=(
        "Наборы весов критериев с указанием происхождения: экспертное "
        "назначение, метод анализа иерархий или энтропийный метод. Для схем, "
        "построенных методом анализа иерархий, приводится отношение "
        "согласованности суждений эксперта."
    ),
)
def list_schemes(request: HttpRequest, usable: bool = Query(default=False)) -> list[ScoringScheme]:
    """Возвращает схемы оценивания вместе с весами критериев."""
    queryset = ScoringScheme.objects.with_weights()
    if usable:
        queryset = queryset.usable()
    return list(queryset)


@router.get(
    "/snapshots",
    response=list[ReadinessSnapshotSchema],
    summary="Результаты расчёта готовности",
    description=(
        "История значений индекса готовности с разбором по частным критериям. "
        "Разбор существен: индекс без него — число, которое нечем объяснить "
        "резервисту, а именно объяснение определяет содержание плана развития."
    ),
)
@paginate
def list_snapshots(
    request: HttpRequest,
    entry: int | None = Query(default=None),
    scheme: int | None = Query(default=None),
    current_only: bool = Query(default=True),
):
    """Возвращает результаты расчёта индекса готовности."""
    queryset = apply_scope_by_path(
        ReadinessSnapshot.objects.with_related(), current_user(request), prefix="entry__employee"
    )
    if entry is not None:
        queryset = queryset.filter(entry_id=entry)
    if scheme is not None:
        queryset = queryset.filter(scheme_id=scheme)
    if current_only:
        queryset = queryset.filter(is_current=True)
    return queryset


@router.get(
    "/nine-box",
    response=NineBoxDistributionSchema,
    summary="Распределение по матрице 9-box",
    description=(
        "Заполнение ячеек матрицы «результативность — потенциал» по "
        "действующим оценкам. Перекос распределения — самостоятельный "
        "результат: если девять десятых сотрудников оказались в центральной "
        "ячейке, оценка не различает людей и нуждается в калибровке."
    ),
)
def nine_box(
    request: HttpRequest,
    branch: int | None = Query(default=None),
    department: int | None = Query(default=None),
) -> dict[str, object]:
    """Возвращает распределение сотрудников по ячейкам матрицы 9-box."""
    queryset = apply_scope_by_path(
        NineBoxPlacement.objects.current(), current_user(request), prefix="employee"
    )
    if branch is not None:
        queryset = queryset.filter(employee__branch_id=branch)
    if department is not None:
        queryset = queryset.filter(employee__department_id=department)

    counts = dict(
        queryset.values_list("cell").annotate(total=Count("id")).values_list("cell", "total")
    )
    total = sum(counts.values())

    cells = [
        {
            "cell": value,
            "label": str(label),
            "count": counts.get(value, 0),
            "share": round(counts.get(value, 0) * 100 / total, 1) if total else 0.0,
        }
        for value, label in NineBoxCell.choices
    ]

    return {"total": total, "cells": cells}


@router.get(
    "/metrics",
    response=ReserveMetricsSchema,
    summary="Показатели здоровья резерва",
    description=(
        "Сводные показатели работы с кадровым резервом: охват, готовность, "
        "обеспеченность критичных должностей и доля назначений из резерва. "
        "Последний показатель — итоговый: он отвечает на вопрос, работает ли "
        "подготовка преемников или должности по-прежнему закрываются наймом."
    ),
)
def metrics(
    request: HttpRequest,
    branch: int | None = Query(default=None),
    year: int | None = Query(default=None),
) -> dict[str, object]:
    """Считает показатели здоровья кадрового резерва."""
    employees = apply_employee_scope(
        Employee.objects.active().filter(is_external=False), current_user(request)
    )
    entries = apply_scope_by_path(
        ReserveEntry.objects.all(), current_user(request), prefix="employee"
    )
    key_positions = KeyPosition.objects.active()
    appointments = Appointment.objects.all()

    if branch is not None:
        employees = employees.filter(branch_id=branch)
        entries = entries.filter(employee__branch_id=branch)
        key_positions = key_positions.filter(position__department__branch_id=branch)
        appointments = appointments.filter(employee__branch_id=branch)
    if year is not None:
        appointments = appointments.in_year(year)

    employees_total = employees.count()
    active_entries = entries.filter(status__in=ACTIVE_RESERVE_STATUSES)
    reserve_entries = active_entries.count()
    ready_now = active_entries.filter(readiness_level=ReadinessLevel.HIGH).count()

    key_total = key_positions.count()
    covered = sum(1 for position in key_positions if position.is_covered)

    appointments_total = appointments.count()
    from_reserve = appointments.filter(kind=AppointmentKind.FROM_RESERVE).count()
    internal = appointments.exclude(kind=AppointmentKind.EXTERNAL).count()

    average = active_entries.aggregate(mean=Avg("readiness_index"))["mean"]

    return {
        "employees_total": employees_total,
        "reserve_entries": reserve_entries,
        "reserve_share": _share(reserve_entries, employees_total),
        "ready_now": ready_now,
        "ready_share": _share(ready_now, reserve_entries),
        "key_positions": key_total,
        "covered_key_positions": covered,
        "coverage_share": _share(covered, key_total),
        "appointments_total": appointments_total,
        "appointments_from_reserve": from_reserve,
        "internal_appointment_share": _share(internal, appointments_total),
        "average_readiness_index": (
            round(float(average), 2) if isinstance(average, Decimal | float) else None
        ),
    }


@router.get(
    "/reserve-funnel",
    response=list[dict[str, object]],
    summary="Воронка кадрового резерва",
    description=(
        "Численность на каждой стадии: кандидат, резервист, готовый к "
        "назначению, назначенный. Воронка показывает, где процесс "
        "останавливается: резерв, из которого никого не назначают, — "
        "формальность, а не инструмент."
    ),
)
def reserve_funnel(request: HttpRequest) -> list[dict[str, object]]:
    """Возвращает численность резерва по стадиям процесса."""
    return reserve_funnel_of(user=current_user(request))


@router.get(
    "/breakdown/{entry_id}",
    response=ReadinessBreakdownSchema,
    summary="Разбор индекса готовности",
    description=(
        "Составляющие композитного индекса одной записи резерва: применённые "
        "веса, частные оценки и вклад каждого критерия в итог. В отличие от "
        "метода снимков, разбор считается на действительных данных, а не "
        "читается из сохранённого расчёта: потребителю, спрашивающему «почему "
        "столько», нужно текущее состояние, а не состояние на момент "
        "последнего пересчёта."
    ),
)
def breakdown(
    request: HttpRequest, entry_id: int, scheme: str | None = Query(default=None)
) -> dict[str, object]:
    """Возвращает разбор индекса готовности записи резерва."""
    from apps.analytics.services import AnalyticsError, explain

    entry = get_object_or_404(
        apply_scope_by_path(
            ReserveEntry.objects.with_related(), current_user(request), prefix="employee"
        ),
        pk=entry_id,
    )
    selected = ScoringScheme.objects.filter(code=scheme).first() if scheme else None
    try:
        result = explain(entry, selected)
    except AnalyticsError as error:
        raise HttpError(409, str(error)) from error
    if result is None:  # pragma: no cover - запись есть, разбор пуст
        raise HttpError(409, "Разбор индекса не построен")

    resolved = selected or ScoringScheme.objects.default()
    return {
        "entry_id": entry.pk,
        "scheme": resolved.code if resolved else "",
        "aggregation": result.aggregation,
        "index_value": result.index_value,
        "readiness_level": result.level,
        "coverage": result.covered_weight,
        "is_reliable": result.is_reliable,
        "rank": result.rank,
        "candidates_total": result.candidates_total,
        "contributions": [
            {
                "code": item.code,
                "name": item.name,
                "value": item.value.normalized,
                "weight": round(item.weight, 5),
                "points": item.points,
                "has_data": item.value.has_data,
                "note": item.value.note,
            }
            for item in result.contributions
        ],
    }


@router.get(
    "/forecast/{entry_id}",
    response=ReadinessForecastSchema,
    summary="Прогноз достижения готовности",
    description=(
        "Срок выхода индекса на порог готовности по истории расчётов. Метод — "
        "линейная экстраполяция: прогноз обязан быть объяснимым резервисту и "
        "проверяемым на бумаге, а вероятность, выданная обучаемой моделью, "
        "такой проверке не поддаётся. Отсутствие прогноза возвращается "
        "содержательным ответом в поле note, а не ошибкой."
    ),
)
def forecast(request: HttpRequest, entry_id: int) -> dict[str, object]:
    """Возвращает прогноз срока достижения готовности."""
    from apps.analytics.services import readiness_forecast

    entry = get_object_or_404(
        apply_scope_by_path(ReserveEntry.objects.all(), current_user(request), prefix="employee"),
        pk=entry_id,
    )
    result = readiness_forecast(entry)
    return {
        "entry_id": result.entry_id,
        "current_index": result.current_index,
        "target_index": result.target_index,
        "slope_per_month": result.slope_per_month,
        "months_to_target": result.months_to_target,
        "expected_date": result.expected_date,
        "observations": result.observations,
        "note": result.note,
    }


@router.get(
    "/comparison",
    response=SchemeComparisonSchema,
    summary="Сравнение схем оценивания",
    description=(
        "Расходятся ли выводы, полученные разными способами назначения весов. "
        "Рядом с корреляцией Спирмена приводится совпадение состава головной "
        "части списка: корреляция 0,9 при полностью разной первой десятке "
        "достижима согласием в хвосте, который на кадровые решения не влияет."
    ),
)
def comparison(
    request: HttpRequest,
    top: int = Query(default=10, ge=3, le=50),
    limit: int = Query(default=400, ge=10, le=2000),
) -> dict[str, object]:
    """Сравнивает ранжирования всех пригодных к расчёту схем."""
    from apps.analytics.services import AnalyticsError, compare_schemes

    schemes = list(ScoringScheme.objects.with_weights().usable())
    entries = apply_scope_by_path(
        ReserveEntry.objects.in_reserve(), current_user(request), prefix="employee"
    ).with_related()[:limit]

    try:
        result = compare_schemes(schemes, entries, top=top)
    except AnalyticsError as error:
        raise HttpError(409, str(error)) from error

    return {
        "entries": len(result.rankings[0].entry_ids) if result.rankings else 0,
        "top_size": result.top_size,
        "verdict": result.verdict,
        "min_correlation": result.min_correlation,
        "pairs": [
            {
                "left": key[0],
                "right": key[1],
                "correlation": value,
                "top_agreement": result.top_agreement.get(key, 0.0),
            }
            for key, value in result.correlations.items()
        ],
    }


@router.get(
    "/stability",
    response=StabilityReportSchema,
    summary="Устойчивость ранжирования",
    description=(
        "Как меняется порядок кандидатов при возмущении весов критериев на "
        "10, 20 и 30 процентов. Ранжирование, разваливающееся при возмущении "
        "в десятую часть, не может служить основанием кадрового решения: веса "
        "всегда приблизительны, и эксперт не различает 0,18 и 0,20."
    ),
)
def stability(
    request: HttpRequest,
    scheme: str | None = Query(default=None),
    top: int = Query(default=10, ge=3, le=50),
    limit: int = Query(default=400, ge=10, le=2000),
) -> dict[str, object]:
    """Возвращает отчёт об устойчивости ранжирования."""
    from apps.analytics.services import AnalyticsError
    from apps.analytics.services import stability as stability_report

    selected = ScoringScheme.objects.filter(code=scheme).first() if scheme else None
    entries = apply_scope_by_path(
        ReserveEntry.objects.in_reserve(), current_user(request), prefix="employee"
    ).with_related()[:limit]

    try:
        report, case = stability_report(selected, entries, top=top)
    except AnalyticsError as error:
        raise HttpError(409, str(error)) from error

    by_criterion: dict[int, list] = {}
    for outcome in report.outcomes:
        if outcome.criterion_index is not None:
            by_criterion.setdefault(outcome.criterion_index, []).append(outcome)

    rows = []
    for index, outcomes in sorted(by_criterion.items()):
        if index >= len(case.criteria):
            continue
        worst = min(outcomes, key=lambda item: item.correlation)
        rows.append(
            {
                "code": case.criteria[index].code,
                "name": case.criteria[index].name,
                "worst_correlation": worst.correlation,
                "worst_delta": worst.delta,
                "stable_share": worst.stable_share,
                "max_rank_shift": max(item.max_rank_shift for item in outcomes),
            }
        )

    critical = (
        case.criteria[report.critical_criterion].code
        if report.critical_criterion is not None and case.criteria
        else None
    )
    resolved = selected or ScoringScheme.objects.default()
    return {
        "scheme": resolved.code if resolved else "",
        "entries": len(case.entries),
        "entries_total": case.entries_total,
        "criteria": len(case.criteria),
        "worst_correlation": report.worst_correlation,
        "average_stability": report.average_stability,
        "is_robust": report.is_robust,
        "critical_criterion": critical,
        "by_criterion": rows,
    }


def _share(part: int, whole: int) -> float:
    """Доля в процентах с одним знаком после запятой."""
    return round(part * 100 / whole, 1) if whole else 0.0
