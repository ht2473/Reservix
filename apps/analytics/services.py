"""Аналитическое ядро: индекс готовности, схемы весов, устойчивость, 9-box."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from django.db import transaction
from django.db.models import Avg, Count, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.analytics import criteria as criteria_module
from apps.analytics import mcda
from apps.analytics.criteria import CriterionValue
from apps.analytics.models import (
    AggregationMethod,
    CalibrationSession,
    CalibrationStatus,
    NineBoxPlacement,
    NineBoxPlacementQuerySet,
    ReadinessSnapshot,
    ScoringCriterion,
    ScoringScheme,
    ScoringWeight,
    WeightingMethod,
)
from apps.org.models import Position
from apps.people.models import Employee
from apps.reserve.models import ReserveEntry
from core.constants import NINE_BOX_MATRIX, NineBoxCell, ReadinessLevel, Signal
from core.settings_types import get_reservix_settings
from core.utils import clamp, shift_months

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.succession.services import CoverageReport

logger = logging.getLogger("reservix.analytics")

#: Наименьшее число резервистов, при котором энтропийные веса имеют смысл.
MIN_SAMPLE_FOR_ENTROPY = 10

#: Наименьшая доля записей, у которых критерий должен быть обеспечен данными, чтобы он участвовал в
#: методах, требующих полной матрицы решений — TOPSIS, энтропийных весах и анализе устойчивости.
MIN_CRITERION_COVERAGE = 0.75

#: Наименьшее число снимков расчёта, по которым строится прогноз готовности.
MIN_SNAPSHOTS_FOR_FORECAST = 3


class AnalyticsError(Exception):
    """Ошибка расчёта или конфигурации аналитического ядра."""


# ---------------------------------------------------------------------------
# Справочник критериев и схема по умолчанию
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CriterionSpec:
    """Описание критерия методики для первичного заполнения справочника."""

    code: str
    name: str
    source: str
    unit: str
    description: str
    methodology: str
    default_weight: Decimal


#: Шесть критериев композитного индекса готовности резервиста.
DEFAULT_CRITERIA: tuple[CriterionSpec, ...] = (
    CriterionSpec(
        code="k1-competency",
        name="Компетентностное соответствие",
        source=criteria_module.SOURCE_COMPETENCY,
        unit="баллы",
        description=(
            "Насколько фактические уровни компетенций отвечают требованиям целевой должности."
        ),
        methodology=(
            "Взвешенный разбор «факт против профиля»: по каждому требованию модели "
            "компетенций целевой должности берётся сведённый фактический уровень, "
            "рассчитывается доля выполнения требования, доли складываются с весами "
            "требований и приводятся к шкале 0–100. Требование без единой оценки в "
            "расчёт доли не входит и снижает покрытие, а не результат."
        ),
        default_weight=Decimal("0.30"),
    ),
    CriterionSpec(
        code="k2-performance",
        name="Результативность",
        source=criteria_module.SOURCE_PERFORMANCE,
        unit="баллы",
        description="Достижение целей за оцениваемый период по оси матрицы 9-box.",
        methodology=(
            "Значение оси результативности действующего размещения сотрудника в "
            "матрице «результативность — потенциал», шкала 0–100. Предварительное "
            "размещение, вычисленное системой, помечается отдельно от подтверждённого "
            "руководителем или скорректированного на сессии калибровки."
        ),
        default_weight=Decimal("0.20"),
    ),
    CriterionSpec(
        code="k3-potential",
        name="Потенциал",
        source=criteria_module.SOURCE_POTENTIAL,
        unit="баллы",
        description="Обучаемость, запас роста к собственной должности и мобильность.",
        methodology=(
            "Взвешенная свёртка трёх наблюдаемых величин: среднего результата "
            "встроенного тестирования за 24 месяца (вес 0,45), соответствия "
            "требованиям собственной должности как меры запаса роста (0,35) и "
            "заявленной готовности к перемещению (0,20). Веса нормируются по "
            "фактически присутствующим слагаемым."
        ),
        default_weight=Decimal("0.15"),
    ),
    CriterionSpec(
        code="k4-development",
        name="Исполнение плана развития",
        source=criteria_module.SOURCE_DEVELOPMENT,
        unit="доля",
        description="Доля выполненных мероприятий ИПР с учётом сложности и просрочек.",
        methodology=(
            "Отношение зачтённого веса мероприятий к весу тех, срок которых наступил. "
            "Мероприятие, выполненное в срок, засчитывается полностью, выполненное "
            "позже срока — с коэффициентом 0,7, невыполненное просроченное — нулём. "
            "Запланированное на будущее в знаменатель не входит."
        ),
        default_weight=Decimal("0.15"),
    ),
    CriterionSpec(
        code="k5-experience",
        name="Профессиональный опыт",
        source=criteria_module.SOURCE_EXPERIENCE,
        unit="месяцы опыта",
        description="Формальное соответствие профилю должности и кросс-функциональный опыт.",
        methodology=(
            "Взвешенная свёртка: доля выполненных формальных требований профиля "
            "целевой должности (0,50), отношение совокупного стажа к требуемому с "
            "насыщением на уровне требования (0,30) и кросс-функциональный опыт по "
            "числу подразделений в кадровой истории при норме в три подразделения "
            "(0,20). Если профиль должности не заполнен, первое слагаемое не "
            "участвует, а требование к стажу заменяется нормой организации в "
            "десять лет."
        ),
        default_weight=Decimal("0.10"),
    ),
    CriterionSpec(
        code="k6-feedback",
        name="Оценка 360°",
        source=criteria_module.SOURCE_FEEDBACK_360,
        unit="баллы",
        description="Сведённая оценка руководителя, коллег и подчинённых.",
        methodology=(
            "Средний балл сессий опроса 360°, взвешенный по отношению респондента к "
            "оцениваемому: руководитель 0,40, коллеги 0,25, подчинённые 0,25, внешние "
            "респонденты 0,10. Самооценка в итог не входит. Веса нормируются по "
            "группам, которые фактически ответили."
        ),
        default_weight=Decimal("0.10"),
    ),
)


@transaction.atomic
def ensure_criteria() -> list[ScoringCriterion]:
    """Создаёт или обновляет справочник шести критериев методики."""
    result: list[ScoringCriterion] = []
    for order, spec in enumerate(DEFAULT_CRITERIA, start=1):
        criterion, created = ScoringCriterion.objects.get_or_create(
            code=spec.code,
            defaults={
                "name": spec.name,
                "description": spec.description,
                "source": spec.source,
                "unit": spec.unit,
                "methodology": spec.methodology,
                "sort_order": order * 10,
                "is_active": True,
            },
        )
        if not created:
            criterion.source = spec.source
            criterion.methodology = spec.methodology
            criterion.sort_order = order * 10
            criterion.save(update_fields=["source", "methodology", "sort_order", "updated_at"])
        result.append(criterion)
    return result


@transaction.atomic
def ensure_default_scheme(*, author: User | None = None) -> ScoringScheme:
    """Создаёт экспертную схему весов по умолчанию, если её ещё нет."""
    criteria = ensure_criteria()
    existing = ScoringScheme.objects.default()
    if existing is not None:
        return existing

    scheme = ScoringScheme.objects.create(
        code="expert-baseline",
        name="Экспертная схема (базовая)",
        description=(
            "Веса назначены кадровой службой исходя из приоритетов организации: "
            "решающим признано соответствие компетенций целевой должности."
        ),
        method=WeightingMethod.EXPERT,
        aggregation=AggregationMethod.WEIGHTED_SUM,
        is_default=True,
        is_active=True,
        source_note=(
            "Значения приняты при внедрении системы как отправная точка методики. "
            "Подлежат пересмотру по результатам сравнения со схемой, построенной "
            "методом анализа иерархий."
        ),
        created_by=author,
    )
    by_code = {criterion.code: criterion for criterion in criteria}
    for order, spec in enumerate(DEFAULT_CRITERIA, start=1):
        ScoringWeight.objects.create(
            scheme=scheme,
            criterion=by_code[spec.code],
            weight=spec.default_weight,
            raw_value=spec.default_weight,
            rank=order,
            comment="Экспертное назначение при внедрении",
        )
    scheme.calculated_at = timezone.now()
    scheme.save(update_fields=["calculated_at", "updated_at"])
    return scheme


def resolve_scheme(scheme: ScoringScheme | None = None) -> ScoringScheme:
    """Возвращает схему для расчёта: указанную либо действующую по умолчанию."""
    resolved = scheme or ScoringScheme.objects.with_weights().default()
    if resolved is None:
        raise AnalyticsError(str(_("Схема оценивания по умолчанию не задана")))
    if not resolved.is_ready:
        raise AnalyticsError(
            str(
                _("Схема «%(name)s» не пригодна к расчёту: веса неполны или суждения противоречивы")
            )
            % {"name": resolved.name}
        )
    return resolved


@dataclass(frozen=True, slots=True)
class CompleteCase:
    """Подматрица решений, пригодная для методов, требующих полноты данных."""

    criteria: tuple[ScoringCriterion, ...]
    entries: tuple[ReserveEntry, ...]
    matrix: tuple[tuple[float, ...], ...]
    dropped_criteria: tuple[ScoringCriterion, ...]
    entries_total: int

    @property
    def is_usable(self) -> bool:
        return len(self.criteria) >= 2 and len(self.entries) >= 2

    @property
    def entries_share(self) -> float:
        """Доля выборки, вошедшая в матрицу, проценты."""
        return round(len(self.entries) * 100 / self.entries_total, 1) if self.entries_total else 0.0


def complete_case(
    entries: list[ReserveEntry],
    criteria: list[ScoringCriterion],
    values: dict[int, dict[str, CriterionValue]],
    *,
    min_coverage: float = MIN_CRITERION_COVERAGE,
) -> CompleteCase:
    """Строит полную подматрицу решений по правилу, описанному в :class:`CompleteCase`."""
    total = len(entries)
    if not entries or not criteria:
        return CompleteCase((), (), (), tuple(criteria), total)

    kept: list[ScoringCriterion] = []
    dropped: list[ScoringCriterion] = []
    for criterion in criteria:
        measured = sum(1 for entry in entries if values[entry.pk][criterion.code].has_data)
        (kept if measured / total >= min_coverage else dropped).append(criterion)

    rows = [
        entry
        for entry in entries
        if all(values[entry.pk][criterion.code].has_data for criterion in kept)
    ]
    matrix = tuple(
        tuple(values[entry.pk][criterion.code].normalized for criterion in kept) for entry in rows
    )
    return CompleteCase(tuple(kept), tuple(rows), matrix, tuple(dropped), total)


# ---------------------------------------------------------------------------
# Расчёт индекса готовности
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Contribution:
    """Вклад одного критерия в итоговый индекс — строка разбора для интерфейса."""

    code: str
    name: str
    weight: float
    value: CriterionValue

    @property
    def points(self) -> float:
        """Сколько баллов итогового индекса дал критерий."""
        return round(self.weight * self.value.normalized, 2)

    @property
    def share(self) -> float:
        """Доля критерия в применённых весах, проценты."""
        return round(self.weight * 100, 1)

    @property
    def signal(self) -> str:
        """Сигнальная окраска частной оценки."""
        if not self.value.has_data:
            return Signal.NEUTRAL
        if self.value.normalized >= 75:
            return Signal.READY
        if self.value.normalized >= 55:
            return Signal.DEVELOP
        return Signal.RISK


@dataclass(slots=True)
class IndexBreakdown:
    """Результат расчёта индекса для одной записи резерва."""

    entry_id: int
    employee_id: int
    position_id: int
    index_value: float
    contributions: list[Contribution] = field(default_factory=list)
    covered_weight: float = 0.0
    aggregation: str = AggregationMethod.WEIGHTED_SUM
    rank: int | None = None
    candidates_total: int | None = None

    @property
    def level(self) -> str:
        """Уровень готовности по порогам организации."""
        options = get_reservix_settings()
        return ReadinessLevel.from_index(
            self.index_value,
            (options["READINESS_THRESHOLD_HIGH"], options["READINESS_THRESHOLD_MEDIUM"]),
        )

    @property
    def coverage_percent(self) -> float:
        """Доля веса критериев, обеспеченных данными, проценты."""
        return round(self.covered_weight * 100, 1)

    @property
    def is_reliable(self) -> bool:
        """Достаточно ли данных, чтобы опираться на индекс в кадровом решении."""
        return self.covered_weight >= 2 / 3

    @property
    def weakest(self) -> list[Contribution]:
        """Критерии, которые сильнее прочих тянут индекс вниз."""
        measured = [item for item in self.contributions if item.value.has_data]
        return sorted(measured, key=lambda item: (item.value.normalized - 100) * item.weight)[:3]

    @property
    def missing(self) -> list[Contribution]:
        """Критерии, не обеспеченные данными."""
        return [item for item in self.contributions if not item.value.has_data]


def compute_index(
    entries: list[ReserveEntry],
    scheme: ScoringScheme | None = None,
    *,
    on_date: dt.date | None = None,
    aggregation: str | None = None,
) -> list[IndexBreakdown]:
    """Считает композитный индекс готовности для набора записей резерва."""
    resolved = resolve_scheme(scheme)
    method = aggregation or resolved.aggregation
    if not entries:
        return []

    weights = list(resolved.weights.select_related("criterion").order_by("criterion__sort_order"))
    active = [weight for weight in weights if weight.criterion.is_active]
    if not active:
        raise AnalyticsError(
            str(_("В схеме «%(name)s» нет действующих критериев")) % {"name": resolved.name}
        )

    context = criteria_module.load_context(entries, on_date=on_date)
    values: dict[int, dict[str, CriterionValue]] = {}
    for entry in entries:
        values[entry.pk] = {
            weight.criterion.code: criteria_module.evaluate(weight.criterion.source, entry, context)
            for weight in active
        }

    results = [_breakdown_for(entry, active, values[entry.pk], method) for entry in entries]

    if method == AggregationMethod.TOPSIS:
        _apply_topsis(results, entries, active, values)

    _assign_ranks(results)
    return results


def _breakdown_for(
    entry: ReserveEntry,
    weights: list[ScoringWeight],
    values: dict[str, CriterionValue],
    method: str,
) -> IndexBreakdown:
    """Сводит частные оценки одной записи в индекс, перенормируя веса."""
    covered = [weight for weight in weights if values[weight.criterion.code].has_data]
    covered_mass = float(sum(weight.weight for weight in covered))
    total_mass = float(sum(weight.weight for weight in weights))
    coverage = covered_mass / total_mass if total_mass > 0 else 0.0

    contributions: list[Contribution] = []
    for weight in weights:
        value = values[weight.criterion.code]
        applied = (
            float(weight.weight) / covered_mass if value.has_data and covered_mass > 0 else 0.0
        )
        contributions.append(
            Contribution(
                code=weight.criterion.code,
                name=weight.criterion.name,
                weight=applied,
                value=value,
            )
        )

    if not covered:
        index_value = 0.0
    elif method == AggregationMethod.GEOMETRIC:
        matrix = [[item.value.normalized for item in contributions if item.value.has_data]]
        vector = [item.weight for item in contributions if item.value.has_data]
        index_value = mcda.weighted_geometric(matrix, vector)[0]
    else:
        # Значение TOPSIS перезаписывается позже: он требует всей группы, а здесь известна одна
        # запись.
        index_value = sum(item.weight * item.value.normalized for item in contributions)

    return IndexBreakdown(
        entry_id=entry.pk,
        employee_id=entry.employee_id,
        position_id=entry.target_position_id,
        index_value=round(clamp(index_value), 2),
        contributions=contributions,
        covered_weight=round(coverage, 4),
        aggregation=method,
    )


def _apply_topsis(
    results: list[IndexBreakdown],
    entries: list[ReserveEntry],
    weights: list[ScoringWeight],
    values: dict[int, dict[str, CriterionValue]],
) -> None:
    """Пересчитывает индексы группы методом TOPSIS."""
    criteria = [weight.criterion for weight in weights]
    weight_by_code = {weight.criterion.code: float(weight.weight) for weight in weights}
    case = complete_case(entries, criteria, values)

    if not case.is_usable:
        for result in results:
            result.aggregation = AggregationMethod.WEIGHTED_SUM
        return

    vector = [weight_by_code[criterion.code] for criterion in case.criteria]
    closeness = mcda.topsis([list(row) for row in case.matrix], vector).closeness
    scored = {entry.pk: value for entry, value in zip(case.entries, closeness, strict=True)}

    for result in results:
        value = scored.get(result.entry_id)
        if value is None:
            result.aggregation = AggregationMethod.WEIGHTED_SUM
        else:
            result.index_value = round(clamp(value), 2)


def _assign_ranks(results: list[IndexBreakdown]) -> None:
    """Расставляет ранги кандидатов внутри каждой целевой должности."""
    by_position: dict[int, list[IndexBreakdown]] = {}
    for result in results:
        by_position.setdefault(result.position_id, []).append(result)

    for group in by_position.values():
        ordered = sorted(group, key=lambda item: (-item.index_value, item.entry_id))
        for place, result in enumerate(ordered, start=1):
            result.rank = place
            result.candidates_total = len(group)


@dataclass(slots=True)
class RecalculationResult:
    """Итог пересчёта индексов: что обновлено и на каких данных."""

    scheme: ScoringScheme
    entries_total: int = 0
    snapshots_created: int = 0
    entries_updated: int = 0
    level_changes: int = 0
    average_index: float = 0.0
    average_coverage: float = 0.0
    unreliable: int = 0

    @property
    def is_operational(self) -> bool:
        """Записан ли результат в рабочие поля записей резерва."""
        return self.scheme.is_default


@transaction.atomic
def recalculate(
    entries: QuerySet[ReserveEntry] | None = None,
    scheme: ScoringScheme | None = None,
    *,
    actor: User | None = None,
    on_date: dt.date | None = None,
    notify_changes: bool = True,
) -> RecalculationResult:
    """Пересчитывает индексы, сохраняет снимки и обновляет записи резерва."""
    resolved = resolve_scheme(scheme)
    queryset = entries if entries is not None else ReserveEntry.objects.in_reserve()
    rows = list(
        queryset.select_related(
            "employee", "employee__department", "target_position", "target_position__grade"
        )
    )
    result = RecalculationResult(scheme=resolved, entries_total=len(rows))
    if not rows:
        return result

    breakdowns = compute_index(rows, resolved, on_date=on_date)
    by_entry = {entry.pk: entry for entry in rows}
    moment = timezone.now()

    # Прежние снимки той же схемы перестают быть текущими одним запросом:
    # снятие признака по одному стоило бы ещё двухсот обращений к базе.
    ReadinessSnapshot.objects.filter(
        entry_id__in=list(by_entry), scheme=resolved, is_current=True
    ).update(is_current=False)

    snapshots: list[ReadinessSnapshot] = []
    changed_levels: list[tuple[ReserveEntry, str]] = []
    touched_positions: set[int] = set()

    for breakdown in breakdowns:
        entry = by_entry[breakdown.entry_id]
        snapshots.append(
            ReadinessSnapshot(
                entry=entry,
                scheme=resolved,
                index_value=Decimal(str(breakdown.index_value)),
                readiness_level=breakdown.level,
                criterion_values=_snapshot_payload(breakdown),
                rank_in_position=breakdown.rank,
                candidates_total=breakdown.candidates_total,
                calculated_at=moment,
                is_current=True,
            )
        )
        result.average_index += breakdown.index_value
        result.average_coverage += breakdown.covered_weight
        if not breakdown.is_reliable:
            result.unreliable += 1

        if resolved.is_default:
            previous_level = entry.readiness_level
            entry.apply_readiness(breakdown.index_value)
            result.entries_updated += 1
            touched_positions.add(entry.target_position_id)
            if entry.readiness_level != previous_level:
                changed_levels.append((entry, previous_level))

    ReadinessSnapshot.objects.bulk_create(snapshots)
    result.snapshots_created = len(snapshots)
    result.average_index = round(result.average_index / len(breakdowns), 2)
    result.average_coverage = round(result.average_coverage / len(breakdowns), 4)
    result.level_changes = len(changed_levels)

    if resolved.is_default:
        from apps.reserve.services import rerank_position

        for position_id in touched_positions:
            rerank_position(position_id)
        if notify_changes:
            _notify_level_changes(changed_levels, actor=actor)

    logger.info(
        "Пересчитан индекс готовности",
        extra={
            "scheme": resolved.code,
            "entries": result.entries_total,
            "snapshots": result.snapshots_created,
            "levels": result.level_changes,
        },
    )
    return result


def _snapshot_payload(breakdown: IndexBreakdown) -> dict[str, object]:
    """Готовит разбор индекса к хранению в снимке."""
    payload: dict[str, object] = {
        "_meta": {
            "aggregation": breakdown.aggregation,
            "coverage": breakdown.covered_weight,
            "reliable": breakdown.is_reliable,
        }
    }
    for item in breakdown.contributions:
        entry = item.value.as_dict()
        entry["weight"] = round(item.weight, 5)
        entry["points"] = item.points
        entry["name"] = item.name
        payload[item.code] = entry
    return payload


def _notify_level_changes(
    changes: list[tuple[ReserveEntry, str]], *, actor: User | None = None
) -> None:
    """Уведомляет резервистов о смене уровня готовности."""
    from apps.notifications.models import NotificationKind
    from apps.notifications.services import notify

    for entry, _previous in changes:
        if entry.employee.user is None:
            continue
        notify(
            entry.employee.user,
            NotificationKind.READINESS_CHANGED,
            str(_("Уровень готовности изменён: %(level)s"))
            % {"level": entry.get_readiness_level_display()},
            obj=entry,
            actor=actor,
        )


def readiness_index(entry: ReserveEntry, *, on_date: dt.date | None = None) -> float:
    """Композитный индекс готовности одной записи резерва."""
    breakdowns = compute_index([entry], on_date=on_date)
    return breakdowns[0].index_value if breakdowns else 0.0


def explain(entry: ReserveEntry, scheme: ScoringScheme | None = None) -> IndexBreakdown | None:
    """Полный разбор индекса записи резерва — для карточки резервиста."""
    breakdowns = compute_index([entry], scheme)
    return breakdowns[0] if breakdowns else None


# ---------------------------------------------------------------------------
# Схемы весов
# ---------------------------------------------------------------------------
@transaction.atomic
def apply_expert_weights(
    scheme: ScoringScheme, weights: dict[int, Decimal], *, note: str = ""
) -> ScoringScheme:
    """Записывает экспертные веса, нормируя их сумму к единице."""
    total = sum(weights.values(), Decimal("0"))
    if total <= 0:
        raise AnalyticsError(str(_("Сумма весов должна быть положительной")))

    ordered = sorted(weights.items(), key=lambda item: item[1], reverse=True)
    ranks = {criterion_id: place for place, (criterion_id, _value) in enumerate(ordered, start=1)}

    scheme.weights.all().delete()
    for criterion_id, raw in weights.items():
        ScoringWeight.objects.create(
            scheme=scheme,
            criterion_id=criterion_id,
            weight=(raw / total).quantize(Decimal("0.00001")),
            raw_value=raw,
            rank=ranks[criterion_id],
        )

    scheme.method = WeightingMethod.EXPERT
    scheme.pairwise_matrix = None
    scheme.consistency_ratio = None
    scheme.is_consistent = True
    scheme.calculated_at = timezone.now()
    if note:
        scheme.source_note = note
    scheme.save(
        update_fields=[
            "method",
            "pairwise_matrix",
            "consistency_ratio",
            "is_consistent",
            "calculated_at",
            "source_note",
            "updated_at",
        ]
    )
    return scheme


@transaction.atomic
def build_ahp_weights(
    scheme: ScoringScheme,
    judgements: dict[tuple[int, int], float],
    criteria: list[ScoringCriterion],
    *,
    note: str = "",
) -> mcda.AHPResult:
    """Строит веса методом анализа иерархий из суждений верхнего треугольника."""
    if not criteria:
        raise AnalyticsError(str(_("Не выбраны критерии для парного сравнения")))

    try:
        matrix = mcda.pairwise_from_judgements(len(criteria), judgements)
        outcome = mcda.ahp_weights(matrix)
    except mcda.MCDAError as error:
        raise AnalyticsError(str(error)) from error

    threshold = get_reservix_settings()["AHP_MAX_CONSISTENCY_RATIO"]
    ordered = sorted(range(len(criteria)), key=lambda index: outcome.weights[index], reverse=True)
    ranks = {index: place for place, index in enumerate(ordered, start=1)}

    scheme.weights.all().delete()
    for index, criterion in enumerate(criteria):
        ScoringWeight.objects.create(
            scheme=scheme,
            criterion=criterion,
            weight=Decimal(str(round(outcome.weights[index], 5))),
            raw_value=Decimal(str(round(outcome.weights[index], 5))),
            rank=ranks[index],
            comment="Компонента собственного вектора матрицы парных сравнений",
        )

    scheme.method = WeightingMethod.AHP
    scheme.pairwise_matrix = {
        "criteria": [criterion.code for criterion in criteria],
        "matrix": [[round(value, 4) for value in row] for row in matrix],
        "lambda_max": round(outcome.lambda_max, 4),
        "consistency_index": round(outcome.consistency_index, 4),
    }
    scheme.consistency_ratio = Decimal(str(round(outcome.consistency_ratio, 4)))
    scheme.is_consistent = outcome.is_consistent(threshold)
    scheme.calculated_at = timezone.now()
    if note:
        scheme.source_note = note
    scheme.save(
        update_fields=[
            "method",
            "pairwise_matrix",
            "consistency_ratio",
            "is_consistent",
            "calculated_at",
            "source_note",
            "updated_at",
        ]
    )
    return outcome


@dataclass(frozen=True, slots=True)
class EntropyRow:
    """Строка разбора энтропийного расчёта по одному критерию."""

    criterion: ScoringCriterion
    entropy: float
    divergence: float
    weight: float

    @property
    def is_indiscriminate(self) -> bool:
        """Не различает ли критерий кандидатов."""
        return self.entropy >= 0.995


@dataclass(frozen=True, slots=True)
class EntropyOutcome:
    """Результат построения энтропийной схемы вместе с его основаниями."""

    result: mcda.EntropyResult
    criteria: tuple[ScoringCriterion, ...]
    excluded: tuple[ScoringCriterion, ...]
    sample: int
    sample_total: int

    @property
    def sample_share(self) -> float:
        """Доля выборки, вошедшая в матрицу решений, проценты."""
        return round(self.sample * 100 / self.sample_total, 1) if self.sample_total else 0.0

    @property
    def rows(self) -> list[EntropyRow]:
        """Разбор по критериям: энтропия, различающая способность, вес."""
        return [
            EntropyRow(
                criterion=criterion,
                entropy=round(self.result.entropies[index], 4),
                divergence=round(self.result.divergences[index], 4),
                weight=round(self.result.weights[index], 5),
            )
            for index, criterion in enumerate(self.criteria)
        ]


@transaction.atomic
def build_entropy_weights(
    scheme: ScoringScheme,
    criteria: list[ScoringCriterion],
    *,
    entries: QuerySet[ReserveEntry] | None = None,
    note: str = "",
) -> EntropyOutcome:
    """Выводит веса из фактического разброса значений критериев."""
    queryset = entries if entries is not None else ReserveEntry.objects.in_reserve()
    rows = list(
        queryset.select_related("employee", "target_position", "target_position__grade")[:2000]
    )
    if len(rows) < MIN_SAMPLE_FOR_ENTROPY:
        raise AnalyticsError(
            str(
                _(
                    "Энтропийные веса требуют не менее %(count)d записей резерва, "
                    "получено %(actual)d"
                )
            )
            % {"count": MIN_SAMPLE_FOR_ENTROPY, "actual": len(rows)}
        )

    context = criteria_module.load_context(rows)
    values = {
        entry.pk: {
            criterion.code: criteria_module.evaluate(criterion.source, entry, context)
            for criterion in criteria
        }
        for entry in rows
    }
    case = complete_case(rows, criteria, values)
    usable = list(case.criteria)
    if not case.is_usable:
        raise AnalyticsError(
            str(_("Достаточного покрытия данными не набрал ни один критерий выборки"))
        )

    try:
        outcome = mcda.entropy_weights([list(row) for row in case.matrix])
    except mcda.MCDAError as error:
        raise AnalyticsError(str(error)) from error

    weight_by_code = {
        criterion.code: outcome.weights[index] for index, criterion in enumerate(usable)
    }
    ordered = sorted(criteria, key=lambda item: weight_by_code.get(item.code, 0.0), reverse=True)
    ranks = {criterion.code: place for place, criterion in enumerate(ordered, start=1)}

    scheme.weights.all().delete()
    for criterion in criteria:
        value = weight_by_code.get(criterion.code, 0.0)
        ScoringWeight.objects.create(
            scheme=scheme,
            criterion=criterion,
            weight=Decimal(str(round(value, 5))),
            raw_value=Decimal(str(round(value, 5))),
            rank=ranks[criterion.code],
            comment=(
                "Различающая способность по выборке"
                if criterion in usable
                else "Исключён: покрытие данными ниже трёх четвертей выборки"
            ),
        )

    excluded = [criterion.name for criterion in case.dropped_criteria]
    scheme.method = WeightingMethod.ENTROPY
    scheme.pairwise_matrix = None
    scheme.consistency_ratio = None
    scheme.is_consistent = True
    scheme.calculated_at = timezone.now()
    scheme.source_note = note or (
        f"Веса выведены на {timezone.localdate():%d.%m.%Y} из {len(case.entries)} записей "
        f"резерва, полных по отобранным критериям, — {case.entries_share} % выборки "
        f"в {case.entries_total} записей."
        + (f" Исключены по недостаточному покрытию: {', '.join(excluded)}." if excluded else "")
    )
    scheme.save(
        update_fields=[
            "method",
            "pairwise_matrix",
            "consistency_ratio",
            "is_consistent",
            "calculated_at",
            "source_note",
            "updated_at",
        ]
    )
    return EntropyOutcome(
        result=outcome,
        criteria=case.criteria,
        excluded=case.dropped_criteria,
        sample=len(case.entries),
        sample_total=case.entries_total,
    )


# ---------------------------------------------------------------------------
# Сравнение схем и устойчивость ранжирования
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SchemeRanking:
    """Ранжирование резервистов по одной схеме."""

    scheme: ScoringScheme
    entry_ids: tuple[int, ...]
    scores: tuple[float, ...]
    ranks: tuple[float, ...]

    def rank_of(self, entry_id: int) -> float | None:
        for index, candidate in enumerate(self.entry_ids):
            if candidate == entry_id:
                return self.ranks[index]
        return None


@dataclass(slots=True)
class SchemeComparison:
    """Сопоставление ранжирований, полученных разными схемами весов."""

    rankings: list[SchemeRanking] = field(default_factory=list)
    #: Корреляция Спирмена между парами схем, ключ — коды схем.
    correlations: dict[tuple[str, str], float] = field(default_factory=dict)
    #: Совпадение состава первой десятки между парами схем.
    top_agreement: dict[tuple[str, str], float] = field(default_factory=dict)
    #: Резервисты, чей ранг между схемами расходится сильнее всего.
    volatile: list[tuple[int, float]] = field(default_factory=list)
    top_size: int = 10

    @property
    def min_correlation(self) -> float:
        return min(self.correlations.values(), default=1.0)

    @property
    def verdict(self) -> str:
        """Словесный вывод для отчёта."""
        value = self.min_correlation
        if value >= 0.9:
            return str(_("Ранжирования согласованы: выбор схемы почти не влияет на порядок"))
        if value >= 0.7:
            return str(_("Ранжирования расходятся умеренно: различия затрагивают середину списка"))
        return str(_("Ранжирования расходятся существенно: выбор схемы определяет результат"))


def compare_schemes(
    schemes: list[ScoringScheme],
    entries: QuerySet[ReserveEntry] | None = None,
    *,
    top: int = 10,
) -> SchemeComparison:
    """Считает индексы по нескольким схемам и сравнивает полученные ранжирования."""
    if len(schemes) < 2:
        raise AnalyticsError(str(_("Для сравнения нужны хотя бы две схемы оценивания")))

    queryset = entries if entries is not None else ReserveEntry.objects.in_reserve()
    rows = list(queryset.select_related("employee", "target_position", "target_position__grade"))
    comparison = SchemeComparison(top_size=top)
    if len(rows) < 2:
        return comparison

    entry_ids = tuple(entry.pk for entry in rows)
    for scheme in schemes:
        breakdowns = compute_index(rows, scheme)
        by_entry = {item.entry_id: item.index_value for item in breakdowns}
        scores = tuple(by_entry.get(entry_id, 0.0) for entry_id in entry_ids)
        comparison.rankings.append(
            SchemeRanking(
                scheme=scheme,
                entry_ids=entry_ids,
                scores=scores,
                ranks=mcda.ranks(scores),
            )
        )

    for first in range(len(comparison.rankings)):
        for second in range(first + 1, len(comparison.rankings)):
            left = comparison.rankings[first]
            right = comparison.rankings[second]
            key = (left.scheme.code, right.scheme.code)
            comparison.correlations[key] = mcda.spearman(left.scores, right.scores)
            comparison.top_agreement[key] = mcda.rank_agreement(left.scores, right.scores, top=top)

    spreads = [
        (
            entry_id,
            max(ranking.ranks[index] for ranking in comparison.rankings)
            - min(ranking.ranks[index] for ranking in comparison.rankings),
        )
        for index, entry_id in enumerate(entry_ids)
    ]
    comparison.volatile = sorted(spreads, key=lambda item: -item[1])[:10]
    return comparison


def stability(
    scheme: ScoringScheme | None = None,
    entries: QuerySet[ReserveEntry] | None = None,
    *,
    deltas: tuple[float, ...] = (0.10, 0.20, 0.30),
    top: int = 10,
) -> tuple[mcda.SensitivityReport, CompleteCase]:
    """Проверяет устойчивость ранжирования к неточности весов."""
    resolved = resolve_scheme(scheme)
    queryset = entries if entries is not None else ReserveEntry.objects.in_reserve()
    rows = list(queryset.select_related("employee", "target_position", "target_position__grade"))
    empty = CompleteCase((), (), (), (), len(rows))
    if len(rows) < 2:
        return mcda.SensitivityReport(), empty

    weights = [
        weight
        for weight in resolved.weights.select_related("criterion").order_by("criterion__sort_order")
        if weight.criterion.is_active
    ]
    context = criteria_module.load_context(rows)
    values = {
        entry.pk: {
            weight.criterion.code: criteria_module.evaluate(weight.criterion.source, entry, context)
            for weight in weights
        }
        for entry in rows
    }
    case = complete_case(rows, [weight.criterion for weight in weights], values)
    if not case.is_usable:
        return mcda.SensitivityReport(), case

    weight_by_code = {weight.criterion.code: float(weight.weight) for weight in weights}
    vector = [weight_by_code[criterion.code] for criterion in case.criteria]
    report = mcda.sensitivity(
        [list(row) for row in case.matrix],
        vector,
        deltas=deltas,
        aggregate="topsis" if resolved.aggregation == AggregationMethod.TOPSIS else "weighted_sum",
        top=top,
    )
    return report, case


# ---------------------------------------------------------------------------
# Матрица 9-box
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Coordinates:
    """Предлагаемые координаты сотрудника в матрице «результативность — потенциал»."""

    performance: float
    potential: float
    performance_note: str = ""
    potential_note: str = ""
    has_data: bool = True

    @property
    def cell(self) -> str:
        return NineBoxPlacement.resolve_cell(self.performance, self.potential)


def suggest_coordinates(
    employees: list[Employee], *, on_date: dt.date | None = None
) -> dict[int, Coordinates]:
    """Вычисляет предварительное размещение сотрудников в матрице 9-box."""
    from apps.assessment.models import AttemptStatus, TestAttempt
    from apps.competencies.services import build_profiles, resolve_competency_model

    moment = on_date or timezone.localdate()
    window_start = shift_months(moment, -criteria_module.WINDOW_MONTHS)
    result: dict[int, Coordinates] = {}
    if not employees:
        return result

    # -- Результативность и запас роста по собственной должности -------------
    performance: dict[int, float] = {}
    growth: dict[int, float] = {}
    by_position: dict[int, list[Employee]] = {}
    for employee in employees:
        if employee.position_id is not None:
            by_position.setdefault(employee.position_id, []).append(employee)

    # Разряды должностей забираются одним запросом: обращение к
    # `employee.position.grade_id` в цикле стоило бы запроса на должность.
    grades = dict(Position.objects.filter(pk__in=list(by_position)).values_list("pk", "grade_id"))
    for position_id, group in by_position.items():
        model = resolve_competency_model(position_id=position_id, grade_id=grades.get(position_id))
        if model is None:
            continue
        for employee_id, profile in build_profiles(group, model, on_date=moment).items():
            level = criteria_module.demonstrated_level(profile)
            if level is not None:
                performance[employee_id] = level
                headroom = criteria_module.growth_headroom(profile)
                if headroom is not None:
                    growth[employee_id] = headroom

    # -- Потенциал: тестирование и мобильность -------------------------------
    tests: dict[int, float] = {}
    for row in (
        TestAttempt.objects.filter(
            employee_id__in=[employee.pk for employee in employees],
            status=AttemptStatus.CHECKED,
            started_at__date__gte=window_start,
            score__isnull=False,
        )
        .values("employee_id")
        .annotate(mean=Avg("score"))
    ):
        tests[row["employee_id"]] = float(row["mean"])

    for employee in employees:
        performance_value = performance.get(employee.pk)
        if performance_value is None:
            continue

        mobility_score = criteria_module.MOBILITY_SCORE.get(employee.mobility, 40.0)
        headroom = growth.get(employee.pk, 50.0)
        test_score = tests.get(employee.pk)
        if test_score is None:
            # Без результатов тестирования потенциал опирается на запас роста и
            # мобильность. Оценка грубее, и в примечании это сказано прямо.
            potential_value = clamp(0.7 * headroom + 0.3 * mobility_score)
            potential_note = str(_("Тестирование не проводилось: оценка по запасу роста"))
        else:
            potential_value = clamp(0.5 * test_score + 0.35 * headroom + 0.15 * mobility_score)
            potential_note = str(_("Средний результат тестирования — %(score).0f")) % {
                "score": test_score
            }

        result[employee.pk] = Coordinates(
            performance=round(performance_value, 2),
            potential=round(potential_value, 2),
            performance_note=str(_("Средний подтверждённый уровень компетенций")),
            potential_note=potential_note,
        )
    return result


@transaction.atomic
def place_employee(
    employee: Employee,
    *,
    performance: float,
    potential: float,
    session: CalibrationSession | None = None,
    actor: User | None = None,
    comment: str = "",
    period_start: dt.date | None = None,
    period_end: dt.date | None = None,
) -> NineBoxPlacement:
    """Размещает сотрудника в матрице, снимая признак действующего с прежнего."""
    NineBoxPlacement.objects.filter(employee=employee, is_current=True).update(is_current=False)
    placement = NineBoxPlacement.objects.create(
        employee=employee,
        session=session,
        performance=Decimal(str(round(clamp(performance), 2))),
        potential=Decimal(str(round(clamp(potential), 2))),
        placed_by=actor,
        comment=comment,
        period_start=period_start,
        period_end=period_end,
        is_current=True,
    )
    _sync_key_talent(employee, placement)
    return placement


@transaction.atomic
def auto_place(
    employees: list[Employee],
    *,
    session: CalibrationSession | None = None,
    overwrite: bool = False,
) -> dict[str, int]:
    """Проставляет предварительные размещения по данным системы."""
    suggestions = suggest_coordinates(employees)
    existing = {
        row["employee_id"]: row["placed_by_id"]
        for row in NineBoxPlacement.objects.filter(
            employee_id__in=[employee.pk for employee in employees], is_current=True
        ).values("employee_id", "placed_by_id")
    }

    summary = {"placed": 0, "skipped": 0, "no_data": 0}
    for employee in employees:
        coordinates = suggestions.get(employee.pk)
        if coordinates is None:
            summary["no_data"] += 1
            continue
        # Размещение, выставленное человеком, автоматика не пересматривает;
        # собственное прежнее предложение — обновляет.
        if not overwrite and existing.get(employee.pk) is not None:
            summary["skipped"] += 1
            continue

        place_employee(
            employee,
            performance=coordinates.performance,
            potential=coordinates.potential,
            session=session,
            actor=None,
            comment=f"{coordinates.performance_note}. {coordinates.potential_note}",
        )
        summary["placed"] += 1

    logger.info("Предварительное размещение в матрице 9-box", extra={"summary": summary})
    return summary


@transaction.atomic
def calibrate(
    placement: NineBoxPlacement,
    *,
    performance: float,
    potential: float,
    comment: str,
    actor: User | None = None,
    session: CalibrationSession | None = None,
) -> NineBoxPlacement:
    """Корректирует размещение по итогам обсуждения руководителями."""
    if not comment.strip():
        raise AnalyticsError(str(_("Изменение оценки требует обоснования")))

    if placement.initial_performance is None:
        placement.initial_performance = placement.performance
        placement.initial_potential = placement.potential

    placement.performance = Decimal(str(round(clamp(performance), 2)))
    placement.potential = Decimal(str(round(clamp(potential), 2)))
    placement.comment = comment
    placement.is_adjusted = True
    placement.placed_by = actor or placement.placed_by
    if session is not None:
        placement.session = session
    placement.save()

    _sync_key_talent(placement.employee, placement)
    return placement


def _sync_key_talent(employee: Employee, placement: NineBoxPlacement) -> None:
    """Приводит отметку ключевого сотрудника в соответствие с ячейкой матрицы."""
    is_talent = placement.is_talent
    if employee.is_key_talent != is_talent:
        employee.is_key_talent = is_talent
        employee.save(update_fields=["is_key_talent", "updated_at"])


@dataclass(frozen=True, slots=True)
class GridCell:
    """Ячейка матрицы 9-box со списком попавших в неё сотрудников."""

    code: str
    label: str
    row: int
    column: int
    count: int
    share: float
    placements: tuple[NineBoxPlacement, ...] = ()

    @property
    def is_talent(self) -> bool:
        from core.constants import TALENT_POOL_CELLS

        return self.code in TALENT_POOL_CELLS

    @property
    def zone(self) -> int:
        """Уровень заливки ячейки, 1–4."""
        return max(1, self.row + self.column)


@dataclass(slots=True)
class NineBoxGrid:
    """Матрица 9-box целиком: три ряда по три ячейки и показатели распределения."""

    rows: list[list[GridCell]] = field(default_factory=list)
    total: int = 0
    talent_pool: int = 0
    adjusted: int = 0
    unplaced: int = 0

    @property
    def talent_share(self) -> float:
        return round(self.talent_pool * 100 / self.total, 1) if self.total else 0.0

    @property
    def concentration(self) -> float:
        """Доля наиболее заполненной ячейки — мера перекоса распределения."""
        counts = [cell.count for row in self.rows for cell in row]
        return round(max(counts, default=0) * 100 / self.total, 1) if self.total else 0.0

    @property
    def is_discriminating(self) -> bool:
        """Различает ли оценка сотрудников."""
        return self.concentration < 40.0


def nine_box_grid(
    placements: QuerySet[NineBoxPlacement] | None = None, *, with_people: bool = True
) -> NineBoxGrid:
    """Строит матрицу 9-box по действующим размещениям."""
    # Приведение типа выражает уже действующую гарантию: сюда приходит либо собственная выборка
    # модели, либо выборка, полученная из неё отбором, — обе остаются выборкой размещений.
    queryset = cast(NineBoxPlacementQuerySet, placements or NineBoxPlacement.objects.current())
    if with_people:
        queryset = queryset.with_related()

    grid = NineBoxGrid()
    buckets: dict[str, list[NineBoxPlacement]] = {}
    counts: dict[str, int] = {}

    if with_people:
        for placement in queryset:
            buckets.setdefault(placement.cell, []).append(placement)
        counts = {cell: len(items) for cell, items in buckets.items()}
        grid.adjusted = sum(1 for items in buckets.values() for item in items if item.is_adjusted)
    else:
        counts = dict(
            queryset.values_list("cell").annotate(total=Count("id")).values_list("cell", "total")
        )
        grid.adjusted = queryset.filter(is_adjusted=True).count()

    grid.total = sum(counts.values())
    labels = dict(NineBoxCell.choices)

    # Ряды выводятся сверху вниз: высокая результативность в первом ряду —
    # так матрицу читают в talent review, и переворот осей ломает узнавание.
    for row_index in (2, 1, 0):
        row: list[GridCell] = []
        for column_index in (0, 1, 2):
            code = str(NINE_BOX_MATRIX[(row_index, column_index)])
            count = counts.get(code, 0)
            row.append(
                GridCell(
                    code=code,
                    label=str(labels.get(code, code)),
                    row=row_index,
                    column=column_index,
                    count=count,
                    share=round(count * 100 / grid.total, 1) if grid.total else 0.0,
                    placements=tuple(buckets.get(code, ())),
                )
            )
        grid.rows.append(row)

    from core.constants import TALENT_POOL_CELLS

    grid.talent_pool = sum(counts.get(str(cell), 0) for cell in TALENT_POOL_CELLS)
    grid.unplaced = (
        Employee.objects.active().filter(is_external=False).count()
        - NineBoxPlacement.objects.current().values("employee_id").distinct().count()
    )
    return grid


@transaction.atomic
def complete_calibration(
    session: CalibrationSession, *, decisions: str = "", actor: User | None = None
) -> CalibrationSession:
    """Завершает сессию калибровки, фиксируя протокол."""
    if session.status in {CalibrationStatus.COMPLETED, CalibrationStatus.CANCELLED}:
        raise AnalyticsError(
            str(_("Сессия калибровки уже закрыта: %(status)s"))
            % {"status": session.get_status_display()}
        )

    session.status = CalibrationStatus.COMPLETED
    session.completed_at = timezone.now()
    if decisions:
        session.decisions = decisions
    session.save(update_fields=["status", "completed_at", "decisions", "updated_at"])

    logger.info(
        "Сессия калибровки завершена",
        extra={
            "session": str(session.public_id),
            "placements": session.placement_count,
            "adjusted": session.adjusted_count,
        },
    )
    return session


def calibration_effect(session: CalibrationSession) -> dict[str, float]:
    """Измеряет, насколько калибровка изменила оценки."""
    rows = [
        (
            float(performance),
            float(potential),
            float(was_performance or 0),
            float(was_potential or 0),
        )
        for performance, potential, was_performance, was_potential in session.placements.filter(
            is_adjusted=True, initial_performance__isnull=False, initial_potential__isnull=False
        ).values_list("performance", "potential", "initial_performance", "initial_potential")
    ]
    if not rows:
        return {"adjusted": 0, "performance_shift": 0.0, "potential_shift": 0.0, "moved_cells": 0}

    performance_shift = sum(row[0] - row[2] for row in rows) / len(rows)
    potential_shift = sum(row[1] - row[3] for row in rows) / len(rows)
    moved = sum(
        1
        for row in rows
        if NineBoxPlacement.resolve_cell(row[0], row[1])
        != NineBoxPlacement.resolve_cell(row[2], row[3])
    )
    return {
        "adjusted": len(rows),
        "performance_shift": round(performance_shift, 2),
        "potential_shift": round(potential_shift, 2),
        "moved_cells": moved,
    }


# ---------------------------------------------------------------------------
# Прогноз и показатели здоровья резерва
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReadinessForecast:
    """Прогноз достижения готовности по истории индекса."""

    entry_id: int
    slope_per_month: float
    current_index: float
    target_index: float
    months_to_target: float | None
    expected_date: dt.date | None
    observations: int
    note: str = ""

    @property
    def is_available(self) -> bool:
        return self.expected_date is not None


def readiness_forecast(
    entry: ReserveEntry, *, scheme: ScoringScheme | None = None
) -> ReadinessForecast:
    """Оценивает срок достижения готовности по истории расчётов индекса."""
    resolved = scheme or ScoringScheme.objects.default()
    options = get_reservix_settings()
    target = float(options["READINESS_THRESHOLD_HIGH"])

    queryset = ReadinessSnapshot.objects.filter(entry=entry).order_by("calculated_at")
    if resolved is not None:
        queryset = queryset.filter(scheme=resolved)
    snapshots = list(queryset.values("calculated_at", "index_value"))

    current = float(entry.readiness_index) if entry.readiness_index is not None else 0.0
    if len(snapshots) < MIN_SNAPSHOTS_FOR_FORECAST:
        return ReadinessForecast(
            entry_id=entry.pk,
            slope_per_month=0.0,
            current_index=current,
            target_index=target,
            months_to_target=None,
            expected_date=None,
            observations=len(snapshots),
            note=str(_("Для прогноза нужно не менее трёх расчётов индекса")),
        )

    origin = snapshots[0]["calculated_at"]
    months = [
        (row["calculated_at"] - origin).total_seconds() / (30.44 * 24 * 3600) for row in snapshots
    ]
    indices = [float(row["index_value"]) for row in snapshots]
    current = indices[-1]

    slope = _least_squares_slope(months, indices)
    if current >= target:
        return ReadinessForecast(
            entry_id=entry.pk,
            slope_per_month=round(slope, 3),
            current_index=round(current, 2),
            target_index=target,
            months_to_target=0.0,
            expected_date=timezone.localdate(),
            observations=len(snapshots),
            note=str(_("Порог готовности достигнут")),
        )
    if slope <= 0.05:
        return ReadinessForecast(
            entry_id=entry.pk,
            slope_per_month=slope,
            current_index=current,
            target_index=target,
            months_to_target=None,
            expected_date=None,
            observations=len(snapshots),
            note=str(_("Индекс не растёт: срок достижения готовности не определён")),
        )

    remaining = (target - current) / slope
    return ReadinessForecast(
        entry_id=entry.pk,
        slope_per_month=round(slope, 3),
        current_index=round(current, 2),
        target_index=target,
        months_to_target=round(remaining, 1),
        expected_date=shift_months(timezone.localdate(), max(1, round(remaining))),
        observations=len(snapshots),
    )


def _least_squares_slope(xs: list[float], ys: list[float]) -> float:
    """Наклон прямой наименьших квадратов; ноль при вырожденной выборке."""
    count = len(xs)
    if count < 2:
        return 0.0
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    variance = sum((value - mean_x) ** 2 for value in xs)
    if variance <= mcda.EPSILON:
        return 0.0
    covariance = sum(
        (x_value - mean_x) * (y_value - mean_y) for x_value, y_value in zip(xs, ys, strict=True)
    )
    return covariance / variance


@dataclass(slots=True)
class ReserveHealth:
    """Показатели здоровья кадрового резерва."""

    entries: int = 0
    ready_now: int = 0
    average_index: float = 0.0
    average_coverage: float = 0.0
    by_level: dict[str, int] = field(default_factory=dict)
    key_positions: int = 0
    covered_positions: int = 0
    bench_depth: float = 0.0
    appointments_year: int = 0
    appointments_from_reserve: int = 0
    talent_pool: int = 0
    growing: int = 0
    stalled: int = 0

    @property
    def ready_share(self) -> float:
        return round(self.ready_now * 100 / self.entries, 1) if self.entries else 0.0

    @property
    def coverage_share(self) -> float:
        return (
            round(self.covered_positions * 100 / self.key_positions, 1)
            if self.key_positions
            else 0.0
        )

    @property
    def reserve_conversion(self) -> float:
        """Доля назначений, закрытых резервистами, — итоговый показатель работы."""
        return (
            round(self.appointments_from_reserve * 100 / self.appointments_year, 1)
            if self.appointments_year
            else 0.0
        )

    @property
    def signal(self) -> str:
        """Общая оценка состояния резерва по доле готовых и обеспеченности."""
        if self.ready_share >= 30 and self.coverage_share >= 70:
            return Signal.READY
        if self.ready_share >= 15 or self.coverage_share >= 50:
            return Signal.DEVELOP
        return Signal.RISK


def reserve_health(
    *,
    user: User | None = None,
    succession_coverage: CoverageReport | None = None,
) -> ReserveHealth:
    """Собирает показатели здоровья резерва в границах видимости пользователя."""
    from apps.succession.models import Appointment, AppointmentKind
    from core.permissions import apply_scope_by_path

    entries = ReserveEntry.objects.in_reserve()
    placements = NineBoxPlacement.objects.current()
    if user is not None:
        entries = apply_scope_by_path(entries, user, prefix="employee")
        placements = apply_scope_by_path(placements, user, prefix="employee")

    health = ReserveHealth()
    health.entries = entries.count()
    for row in entries.values("readiness_level").annotate(total=Count("id")):
        health.by_level[row["readiness_level"]] = row["total"]
    health.ready_now = health.by_level.get(ReadinessLevel.HIGH, 0)

    average = entries.aggregate(mean=Avg("readiness_index"))["mean"]
    health.average_index = round(float(average), 2) if average is not None else 0.0

    coverage = ReadinessSnapshot.objects.filter(entry__in=entries, is_current=True).values_list(
        "criterion_values", flat=True
    )
    shares = [
        payload["_meta"]["coverage"]
        for payload in coverage
        if isinstance(payload, dict) and isinstance(payload.get("_meta"), dict)
    ]
    health.average_coverage = round(sum(shares) / len(shares) * 100, 1) if shares else 0.0

    # Обеспеченность берётся из отчёта раздела преемственности, а не считается заново.
    from apps.succession.services import coverage_report

    coverage_data = succession_coverage if succession_coverage is not None else coverage_report()
    health.key_positions = coverage_data.total
    health.covered_positions = coverage_data.covered
    health.bench_depth = coverage_data.bench_depth

    from core.constants import TALENT_POOL_CELLS

    health.talent_pool = placements.filter(cell__in=TALENT_POOL_CELLS).count()

    year_start = shift_months(timezone.localdate(), -12)
    appointments = Appointment.objects.filter(appointment_date__gte=year_start)
    health.appointments_year = appointments.count()
    health.appointments_from_reserve = appointments.filter(
        kind=AppointmentKind.FROM_RESERVE
    ).count()

    health.growing, health.stalled = _dynamics(entries)
    return health


def _dynamics(entries: QuerySet[ReserveEntry]) -> tuple[int, int]:
    """Считает, у скольких резервистов индекс вырос, а у скольких — нет."""
    history: dict[int, list[float]] = {}
    for row in (
        ReadinessSnapshot.objects.filter(entry__in=entries)
        .order_by("entry_id", "-calculated_at")
        .values("entry_id", "index_value")[:5000]
    ):
        bucket = history.setdefault(row["entry_id"], [])
        if len(bucket) < 2:
            bucket.append(float(row["index_value"]))

    growing = sum(1 for values in history.values() if len(values) == 2 and values[0] > values[1])
    stalled = sum(1 for values in history.values() if len(values) == 2 and values[0] <= values[1])
    return growing, stalled


def index_distribution(entries: QuerySet[ReserveEntry] | None = None) -> list[dict[str, object]]:
    """Распределение резервистов по десятибалльным интервалам индекса."""
    queryset = entries if entries is not None else ReserveEntry.objects.in_reserve()
    buckets = [0] * 10
    for value in queryset.exclude(readiness_index__isnull=True).values_list(
        "readiness_index", flat=True
    ):
        index = min(int(float(value or 0) // 10), 9)
        buckets[index] += 1

    total = sum(buckets)
    return [
        {
            "from": position * 10,
            "to": position * 10 + 10,
            "count": count,
            "share": round(count * 100 / total, 1) if total else 0.0,
        }
        for position, count in enumerate(buckets)
    ]


def criterion_averages(scheme: ScoringScheme | None = None) -> list[dict[str, object]]:
    """Средние значения частных критериев по резерву — профиль сильных и слабых мест."""
    resolved = scheme or ScoringScheme.objects.default()
    if resolved is None:
        return []

    payloads = ReadinessSnapshot.objects.filter(scheme=resolved, is_current=True).values_list(
        "criterion_values", flat=True
    )
    totals: dict[str, list[float]] = {}
    names: dict[str, str] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for code, value in payload.items():
            if code == "_meta" or not isinstance(value, dict) or not value.get("has_data"):
                continue
            totals.setdefault(code, []).append(float(value.get("normalized", 0)))
            names[code] = str(value.get("name", code))

    measured_total = len(payloads)
    averages = [
        (
            sum(values) / len(values),
            {
                "code": code,
                "name": names.get(code, code),
                "average": round(sum(values) / len(values), 1),
                "measured": len(values),
                "coverage": (
                    round(len(values) * 100 / measured_total, 1) if measured_total else 0.0
                ),
            },
        )
        for code, values in totals.items()
    ]
    # Слабые критерии идут первыми: страница отвечает на вопрос «что тянет
    # резерв вниз», а не «чем он хорош».
    return [row for _average, row in sorted(averages, key=lambda item: item[0])]
