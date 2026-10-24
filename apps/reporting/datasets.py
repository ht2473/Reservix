"""Предметные сборщики отчётов."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db.models import Avg, Count, Q
from django.utils import timezone

from apps.reporting.structure import Column, Metric, ReportDocument, Table, ValueKind
from core.constants import ReadinessLevel, Role

if TYPE_CHECKING:
    from apps.accounts.models import User

#: Подпись сборщика: параметры отбора и пользователь, от имени которого он выполняется.
Builder = Callable[[dict[str, Any], "User"], ReportDocument]


class DatasetError(Exception):
    """Отчёт невозможно собрать при заданных параметрах."""


# ---------------------------------------------------------------------------
# Общие части
# ---------------------------------------------------------------------------
def _document(title: str, subtitle: str, user: User, params: dict[str, Any]) -> ReportDocument:
    """Создаёт заготовку отчёта с реквизитами организации и автора."""
    return ReportDocument(
        title=title,
        subtitle=subtitle,
        organization=str(settings.ORGANIZATION_NAME),
        author=getattr(user, "full_name", "") or getattr(user, "short_name", ""),
        generated_at=timezone.localtime(),
        parameters=_describe(params),
    )


#: Подписи параметров отбора для реквизитов документа.
PARAMETER_LABELS: dict[str, str] = {
    "date_from": "Период с",
    "date_to": "Период по",
    "branch": "Филиал",
    "department": "Подразделение",
    "reserve_group": "Группа резерва",
    "status": "Состояние",
    "readiness": "Уровень готовности",
    "year": "Год",
    "scheme": "Схема оценивания",
}


def _describe(params: dict[str, Any]) -> list[tuple[str, str]]:
    """Приводит параметры отбора к перечню подписей для шапки документа."""
    described: list[tuple[str, str]] = []
    for key, value in params.items():
        if value in (None, "", [], {}):
            continue
        label = PARAMETER_LABELS.get(key, key)
        described.append((label, str(value)))
    if not described:
        described.append(("Отбор", "без ограничений"))
    return described


def _scoped_employees(user: User):
    """Выборка сотрудников в границах видимости пользователя."""
    from apps.people.models import Employee
    from core.permissions import apply_employee_scope

    return apply_employee_scope(Employee.objects.active().with_org(), user)


def _period(params: dict[str, Any]) -> tuple[dt.date, dt.date]:
    """Период отчёта: заданный параметрами либо текущий календарный год."""
    today = timezone.localdate()
    start = params.get("date_from") or dt.date(today.year, 1, 1)
    end = params.get("date_to") or today
    if isinstance(start, str):
        start = dt.date.fromisoformat(start)
    if isinstance(end, str):
        end = dt.date.fromisoformat(end)
    if end < start:
        raise DatasetError("Дата окончания периода раньше даты начала")
    return start, end


# ---------------------------------------------------------------------------
# Кадровый резерв
# ---------------------------------------------------------------------------
def reserve_roster(params: dict[str, Any], user: User) -> ReportDocument:
    """Состав кадрового резерва."""
    from apps.reserve.models import ReserveEntry
    from apps.reserve.services import reserve_summary
    from core.permissions import apply_scope_by_path

    document = _document(
        "Состав кадрового резерва",
        "Резервисты, целевые должности и достигнутый уровень готовности",
        user,
        params,
    )

    entries = ReserveEntry.objects.in_reserve().with_related()
    entries = apply_scope_by_path(entries, user, prefix="employee")

    if params.get("reserve_group"):
        entries = entries.filter(reserve_group__code=params["reserve_group"])
    if params.get("readiness"):
        entries = entries.filter(readiness_level=params["readiness"])

    summary = reserve_summary(user=user)
    document.metrics = [
        Metric("Состоит в резерве", summary.active, ValueKind.INTEGER),
        Metric("Готовы к назначению", summary.ready_now, ValueKind.INTEGER),
        Metric("Доля готовых", summary.ready_share, ValueKind.PERCENT),
        Metric(
            "Просрочен пересмотр",
            summary.overdue_reviews,
            ValueKind.INTEGER,
            note="записи, срок пересмотра которых наступил",
        ),
    ]

    table = Table(
        title="Резервисты",
        columns=[
            Column("employee", "Резервист", width=26),
            Column("position", "Текущая должность", width=26),
            Column("department", "Подразделение", width=22),
            Column("target", "Целевая должность", width=26),
            Column("group", "Группа резерва", width=18),
            Column("status", "Состояние", width=14),
            Column("index", "Индекс", ValueKind.NUMBER, width=10),
            Column("readiness", "Готовность", width=18),
            Column("included", "Включён", ValueKind.DATE, width=12),
            Column("review", "Пересмотр", ValueKind.DATE, width=12),
        ],
    )

    for entry in entries.order_by("employee__last_name"):
        table.rows.append(
            {
                "employee": entry.employee.full_name,
                "position": entry.employee.position_title or str(entry.employee.position),
                "department": str(entry.employee.department) if entry.employee.department else "",
                "target": str(entry.target_position),
                "group": str(entry.reserve_group) if entry.reserve_group else "",
                "status": entry.get_status_display(),
                "index": entry.readiness_index,
                "readiness": entry.get_readiness_level_display(),
                "included": entry.inclusion_date,
                "review": entry.review_date,
            }
        )

    document.add_table(table)
    document.notes.append(
        "Индекс готовности рассчитан по действующей схеме оценивания. "
        "Уровень готовности определяется сравнением индекса с порогами "
        f"{settings.RESERVIX['READINESS_THRESHOLD_HIGH']} и "
        f"{settings.RESERVIX['READINESS_THRESHOLD_MEDIUM']} баллов."
    )
    return document


def readiness_ranking(params: dict[str, Any], user: User) -> ReportDocument:
    """Рейтинг готовности с разбором по критериям."""
    from apps.analytics.models import ReadinessSnapshot
    from apps.analytics.services import resolve_scheme
    from core.permissions import apply_scope_by_path

    scheme = resolve_scheme()
    document = _document(
        "Рейтинг готовности резервистов",
        f"Схема оценивания: {scheme.name}",
        user,
        {**params, "scheme": scheme.name},
    )

    snapshots = (
        ReadinessSnapshot.objects.filter(is_current=True, scheme=scheme)
        .with_related()
        .order_by("-index_value")
    )
    snapshots = apply_scope_by_path(snapshots, user, prefix="entry__employee")

    criteria = list(scheme.weights.select_related("criterion").order_by("-weight"))

    columns = [
        Column("rank", "№", ValueKind.INTEGER, width=6),
        Column("employee", "Резервист", width=26),
        Column("target", "Целевая должность", width=26),
        Column("index", "Индекс", ValueKind.NUMBER, width=10),
        Column("level", "Уровень", width=18),
        Column("coverage", "Обеспечено весом", ValueKind.PERCENT, width=14),
    ]
    columns.extend(
        Column(f"c_{weight.criterion.code}", weight.criterion.code.upper(), ValueKind.NUMBER, 8)
        for weight in criteria
    )

    table = Table(
        title="Ранжирование",
        columns=columns,
        note=(
            "Столбцы с кодами критериев содержат нормированные значения по шкале 0–100. "
            "Доля обеспеченного веса показывает, какая часть модели опиралась на данные: "
            "необеспеченный критерий исключается из свёртки, а его вес распределяется "
            "между остальными."
        ),
    )

    for position, snapshot in enumerate(snapshots, start=1):
        row: dict[str, Any] = {
            "rank": position,
            "employee": snapshot.entry.employee.full_name,
            "target": str(snapshot.entry.target_position),
            "index": snapshot.index_value,
            "level": ReadinessLevel(snapshot.readiness_level).label,
            "coverage": _coverage_of(snapshot),
        }
        for weight in criteria:
            values = snapshot.criterion_values.get(weight.criterion.code) or {}
            row[f"c_{weight.criterion.code}"] = (
                values.get("normalized") if isinstance(values, dict) else None
            )
        table.rows.append(row)

    document.add_table(table)

    if criteria:
        weights_table = Table(
            title="Веса критериев",
            columns=[
                Column("code", "Код", width=10),
                Column("name", "Критерий", width=40),
                Column("weight", "Вес", ValueKind.NUMBER, width=10),
                Column("share", "Доля", ValueKind.PERCENT, width=10),
            ],
            note=f"Способ определения весов: {scheme.get_method_display()}.",
        )
        for weight in criteria:
            weights_table.rows.append(
                {
                    "code": weight.criterion.code.upper(),
                    "name": weight.criterion.name,
                    "weight": weight.weight,
                    "share": weight.percent,
                }
            )
        document.add_table(weights_table)

    return document


def _coverage_of(snapshot) -> float | None:
    """Доля обеспеченного веса из снимка расчёта, проценты."""
    meta = snapshot.criterion_values.get("_meta")
    if isinstance(meta, dict):
        value = meta.get("coverage")
        if value is not None:
            return round(float(value) * 100, 1)
    return None


# ---------------------------------------------------------------------------
# Преемственность
# ---------------------------------------------------------------------------
def succession_coverage(params: dict[str, Any], user: User) -> ReportDocument:
    """Обеспеченность критичных должностей преемниками."""
    from apps.succession.services import coverage_report, key_positions, uncovered_positions

    document = _document(
        "Обеспеченность критичных должностей",
        "Покрытие, глубина скамейки и немедленная готовность",
        user,
        params,
    )

    report = coverage_report()
    document.metrics = [
        Metric("Критичных должностей", report.total, ValueKind.INTEGER),
        Metric("Покрытие", report.coverage, ValueKind.PERCENT, "хотя бы один преемник"),
        Metric("Глубина скамейки", report.bench_depth, ValueKind.NUMBER, "преемников на должность"),
        Metric(
            "Готовы сейчас",
            report.ready_coverage,
            ValueKind.PERCENT,
            "должностей с преемником, готовым к назначению",
        ),
    ]

    positions_table = Table(
        title="Критичные должности",
        columns=[
            Column("position", "Должность", width=32),
            Column("department", "Подразделение", width=24),
            Column("criticality", "Критичность", ValueKind.INTEGER, width=12),
            Column("risk", "Риск освобождения", width=18),
            Column("required", "Требуется преемников", ValueKind.INTEGER, width=14),
            Column("successors", "Есть преемников", ValueKind.INTEGER, width=14),
            Column("ready", "Готовых сейчас", ValueKind.INTEGER, width=14),
        ],
    )

    # Обеспеченность уже посчитана агрегатами в `key_positions()`: свойства модели выполняют по
    # запросу на каждой строке, и тридцать критичных должностей стоили бы шестидесяти обращений к
    # базе.
    positions: Iterable[Any] = key_positions()
    for key_position in positions:
        positions_table.rows.append(
            {
                "position": str(key_position.position),
                "department": str(key_position.position.department or ""),
                "criticality": key_position.criticality,
                "risk": key_position.get_vacancy_risk_display(),
                "required": key_position.required_successors,
                "successors": key_position.successors_total,
                "ready": key_position.successors_ready,
            }
        )
    document.add_table(positions_table)

    uncovered = Table(
        title="Должности без преемников",
        columns=[
            Column("position", "Должность", width=36),
            Column("department", "Подразделение", width=28),
            Column("criticality", "Критичность", ValueKind.INTEGER, width=12),
            Column("risk", "Риск освобождения", width=20),
        ],
        note="Перечень отсортирован по приоритету планирования: критичность, умноженная на риск.",
    )
    for key_position in uncovered_positions():
        uncovered.rows.append(
            {
                "position": str(key_position.position),
                "department": str(key_position.position.department or ""),
                "criticality": key_position.criticality,
                "risk": key_position.get_vacancy_risk_display(),
            }
        )
    document.add_table(uncovered)
    return document


def appointments_log(params: dict[str, Any], user: User) -> ReportDocument:
    """Назначения за период с указанием источника кандидата."""
    from apps.succession.models import Appointment, AppointmentKind

    start, end = _period(params)
    document = _document(
        "Назначения на должности",
        f"Период с {start:%d.%m.%Y} по {end:%d.%m.%Y}",
        user,
        {**params, "date_from": start, "date_to": end},
    )

    appointments = (
        Appointment.objects.with_related()
        .filter(appointment_date__range=(start, end))
        .order_by("-appointment_date")
    )

    total = appointments.count()
    from_reserve = appointments.filter(kind=AppointmentKind.FROM_RESERVE).count()
    document.metrics = [
        Metric("Назначений за период", total, ValueKind.INTEGER),
        Metric("Из кадрового резерва", from_reserve, ValueKind.INTEGER),
        Metric(
            "Доля назначений из резерва",
            round(from_reserve * 100 / total, 1) if total else 0,
            ValueKind.PERCENT,
            "показатель результативности подготовки преемников",
        ),
    ]

    table = Table(
        title="Назначения",
        columns=[
            Column("date", "Дата", ValueKind.DATE, width=12),
            Column("employee", "Назначен", width=26),
            Column("position", "Должность", width=30),
            Column("kind", "Источник", width=22),
            Column("predecessor", "Предшественник", width=24),
            Column("order", "Приказ", width=14),
        ],
    )
    for appointment in appointments:
        table.rows.append(
            {
                "date": appointment.appointment_date,
                "employee": appointment.employee.full_name,
                "position": str(appointment.position),
                "kind": appointment.get_kind_display(),
                "predecessor": (
                    appointment.predecessor.full_name if appointment.predecessor else ""
                ),
                "order": appointment.order_number,
            }
        )
    document.add_table(table)
    return document


# ---------------------------------------------------------------------------
# Развитие
# ---------------------------------------------------------------------------
def development_progress(params: dict[str, Any], user: User) -> ReportDocument:
    """Выполнение индивидуальных планов развития."""
    from apps.development.models import ActivityStatus, DevelopmentPlan, PlanStatus
    from core.permissions import apply_scope_by_path

    document = _document(
        "Выполнение планов развития",
        "Состояние индивидуальных планов и мероприятий",
        user,
        params,
    )

    plans = DevelopmentPlan.objects.with_related()
    plans = apply_scope_by_path(plans, user, prefix="employee")
    if params.get("status"):
        plans = plans.filter(status=params["status"])

    today = timezone.localdate()
    plans = plans.annotate(
        activities_total=Count("activities", distinct=True),
        activities_done=Count(
            "activities",
            filter=Q(activities__status=ActivityStatus.COMPLETED),
            distinct=True,
        ),
        activities_overdue=Count(
            "activities",
            filter=Q(
                activities__due_date__lt=today,
                activities__status__in=[ActivityStatus.PENDING, ActivityStatus.IN_PROGRESS],
            ),
            distinct=True,
        ),
    )

    aggregate = plans.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(status=PlanStatus.ACTIVE)),
        average=Avg("completion_rate"),
    )
    document.metrics = [
        Metric("Планов развития", aggregate["total"] or 0, ValueKind.INTEGER),
        Metric("Действующих", aggregate["active"] or 0, ValueKind.INTEGER),
        Metric(
            "Среднее выполнение",
            round(aggregate["average"] or 0, 1),
            ValueKind.PERCENT,
            "по весам мероприятий, а не по их числу",
        ),
    ]

    table = Table(
        title="Планы развития",
        columns=[
            Column("employee", "Сотрудник", width=26),
            Column("title", "План", width=30),
            Column("status", "Состояние", width=16),
            Column("period", "Период", width=22),
            Column("completion", "Выполнение", ValueKind.PERCENT, width=12),
            Column("total", "Мероприятий", ValueKind.INTEGER, width=12),
            Column("done", "Выполнено", ValueKind.INTEGER, width=12),
            Column("overdue", "Просрочено", ValueKind.INTEGER, width=12),
            Column("supervisor", "Руководитель", width=24),
        ],
    )
    for plan in plans.order_by("employee__last_name"):
        table.rows.append(
            {
                "employee": plan.employee.full_name,
                "title": plan.title,
                "status": plan.get_status_display(),
                "period": f"{plan.start_date:%d.%m.%Y} — {plan.end_date:%d.%m.%Y}",
                "completion": plan.completion_rate,
                "total": plan.activities_total,
                "done": plan.activities_done,
                "overdue": plan.activities_overdue,
                "supervisor": plan.supervisor.short_name if plan.supervisor else "",
            }
        )
    document.add_table(table)
    return document


def mentorship_report(params: dict[str, Any], user: User) -> ReportDocument:
    """Наставничество: пары, срок сопровождения и результативность."""
    from apps.development.models import Mentorship, MentorshipStatus

    document = _document(
        "Наставничество",
        "Пары «наставник — подопечный», ход и результат сопровождения",
        user,
        params,
    )

    pairs = Mentorship.objects.with_related()
    if user.role == Role.MENTOR:
        pairs = pairs.for_mentor(user.pk)
    else:
        from core.permissions import apply_scope_by_path

        pairs = apply_scope_by_path(pairs, user, prefix="mentee")

    active = pairs.filter(status=MentorshipStatus.ACTIVE).count()
    document.metrics = [
        Metric("Пар наставничества", pairs.count(), ValueKind.INTEGER),
        Metric("Действующих", active, ValueKind.INTEGER),
        Metric(
            "Средняя оценка эффективности",
            round(pairs.aggregate(value=Avg("effectiveness"))["value"] or 0, 2),
            ValueKind.NUMBER,
            "по пятибалльной шкале, оценка кадровой службы",
        ),
    ]

    table = Table(
        title="Пары наставничества",
        columns=[
            Column("mentor", "Наставник", width=26),
            Column("mentee", "Подопечный", width=26),
            Column("status", "Состояние", width=16),
            Column("start", "Начало", ValueKind.DATE, width=12),
            Column("meetings", "Встреч", ValueKind.INTEGER, width=10),
            Column("frequency", "Ритм, дней", ValueKind.INTEGER, width=12),
            Column("last", "Последняя встреча", ValueKind.DATE, width=14),
            Column("effectiveness", "Эффективность", ValueKind.NUMBER, width=12),
        ],
    )
    for pair in pairs.order_by("mentor__last_name", "mentee__last_name"):
        table.rows.append(
            {
                "mentor": pair.mentor.full_name,
                "mentee": pair.mentee.full_name,
                "status": pair.get_status_display(),
                "start": pair.start_date,
                "meetings": pair.meetings_count,
                "frequency": pair.meeting_frequency_days,
                "last": pair.last_meeting_at,
                "effectiveness": pair.effectiveness,
            }
        )
    document.add_table(table)
    return document


# ---------------------------------------------------------------------------
# Оценка и аналитика
# ---------------------------------------------------------------------------
def assessment_summary(params: dict[str, Any], user: User) -> ReportDocument:
    """Итоги кампаний оценки: охват, завершённость, средние уровни."""
    from apps.assessment.models import AssessmentCampaign, SessionStatus

    start, end = _period(params)
    document = _document(
        "Итоги кампаний оценки",
        f"Период с {start:%d.%m.%Y} по {end:%d.%m.%Y}",
        user,
        {**params, "date_from": start, "date_to": end},
    )

    # Завершённость считается аннотацией, а не свойством модели: свойство
    # `completion_rate` выполняет два запроса на каждой кампании.
    campaigns = (
        AssessmentCampaign.objects.filter(period_start__lte=end, period_end__gte=start)
        .select_related("competency_model", "branch", "department")
        .annotate(
            sessions_total=Count("sessions", distinct=True),
            sessions_done=Count(
                "sessions",
                filter=Q(sessions__status__in=[SessionStatus.SUBMITTED, SessionStatus.APPROVED]),
                distinct=True,
            ),
        )
        .order_by("-period_start")
    )

    table = Table(
        title="Кампании",
        columns=[
            Column("name", "Кампания", width=32),
            Column("model", "Модель компетенций", width=26),
            Column("status", "Состояние", width=16),
            Column("period", "Оцениваемый период", width=24),
            Column("deadline", "Срок сдачи", ValueKind.DATE, width=12),
            Column("total", "Анкет", ValueKind.INTEGER, width=10),
            Column("done", "Заполнено", ValueKind.INTEGER, width=12),
            Column("rate", "Завершённость", ValueKind.PERCENT, width=14),
        ],
    )
    for campaign in campaigns:
        table.rows.append(
            {
                "name": campaign.name,
                "model": str(campaign.competency_model),
                "status": campaign.get_status_display(),
                "period": f"{campaign.period_start:%d.%m.%Y} — {campaign.period_end:%d.%m.%Y}",
                "deadline": campaign.deadline,
                "total": campaign.sessions_total,
                "done": campaign.sessions_done,
                "rate": (
                    round(campaign.sessions_done * 100 / campaign.sessions_total, 1)
                    if campaign.sessions_total
                    else 0
                ),
            }
        )

    document.metrics = [
        Metric("Кампаний за период", len(table.rows), ValueKind.INTEGER),
        Metric(
            "Заполнено анкет",
            sum(row["done"] for row in table.rows),
            ValueKind.INTEGER,
        ),
    ]
    document.add_table(table)
    return document


def nine_box_distribution(params: dict[str, Any], user: User) -> ReportDocument:
    """Распределение по матрице «результативность — потенциал»."""
    from apps.analytics.services import nine_box_grid

    document = _document(
        "Распределение по матрице 9-box",
        "Результативность и потенциал сотрудников после калибровки",
        user,
        params,
    )

    grid = nine_box_grid(with_people=False)
    document.metrics = [
        Metric("Размещено сотрудников", grid.total, ValueKind.INTEGER),
        Metric("Талант-пул", grid.talent_share, ValueKind.PERCENT, "три верхние правые ячейки"),
        Metric(
            "Концентрация",
            grid.concentration,
            ValueKind.PERCENT,
            "доля наиболее заполненной ячейки; выше 40 % оценка перестаёт различать людей",
        ),
        Metric("Скорректировано калибровкой", grid.adjusted, ValueKind.INTEGER),
    ]

    table = Table(
        title="Ячейки матрицы",
        columns=[
            Column("cell", "Ячейка", width=30),
            Column("performance", "Результативность", width=18),
            Column("potential", "Потенциал", width=18),
            Column("count", "Сотрудников", ValueKind.INTEGER, width=12),
            Column("share", "Доля", ValueKind.PERCENT, width=10),
            Column("pool", "Талант-пул", ValueKind.BOOLEAN, width=12),
        ],
    )
    levels = {0: "низкая", 1: "средняя", 2: "высокая"}
    for row in grid.rows:
        for cell in row:
            table.rows.append(
                {
                    "cell": cell.label,
                    "performance": levels.get(cell.row, ""),
                    "potential": levels.get(cell.column, ""),
                    "count": cell.count,
                    "share": cell.share,
                    "pool": cell.is_talent,
                }
            )
    document.add_table(table)
    return document


def reserve_health_report(params: dict[str, Any], user: User) -> ReportDocument:
    """Метрики здоровья кадрового резерва."""
    from apps.analytics.services import reserve_health

    document = _document(
        "Здоровье кадрового резерва",
        "Сводные показатели состояния и результативности резерва",
        user,
        params,
    )

    health = reserve_health(user=user)
    document.metrics = [
        Metric("Состоит в резерве", health.entries, ValueKind.INTEGER),
        Metric("Готовы к назначению", health.ready_now, ValueKind.INTEGER),
        Metric("Доля готовых", health.ready_share, ValueKind.PERCENT),
        Metric("Средний индекс готовности", health.average_index, ValueKind.NUMBER),
        Metric("Обеспеченность критичных должностей", health.coverage_share, ValueKind.PERCENT),
        Metric("Глубина скамейки", health.bench_depth, ValueKind.NUMBER),
        Metric(
            "Доля назначений из резерва",
            health.reserve_conversion,
            ValueKind.PERCENT,
            "итоговый показатель: закрываются ли должности подготовленными людьми",
        ),
        Metric("Талант-пул", health.talent_pool, ValueKind.INTEGER),
        Metric("Растут по индексу", health.growing, ValueKind.INTEGER),
        Metric("Не изменились", health.stalled, ValueKind.INTEGER),
    ]

    levels = Table(
        title="Распределение по уровням готовности",
        columns=[
            Column("level", "Уровень", width=30),
            Column("count", "Резервистов", ValueKind.INTEGER, width=14),
            Column("share", "Доля", ValueKind.PERCENT, width=12),
        ],
    )
    for level in ReadinessLevel:
        count = health.by_level.get(level.value, 0)
        levels.rows.append(
            {
                "level": level.label,
                "count": count,
                "share": round(count * 100 / health.entries, 1) if health.entries else 0,
            }
        )
    document.add_table(levels)
    return document


# ---------------------------------------------------------------------------
# Кадровый состав
# ---------------------------------------------------------------------------
def unit_summary(params: dict[str, Any], user: User) -> ReportDocument:
    """Сводка по подразделению для руководителя."""
    from apps.development.models import DevelopmentPlan, PlanStatus
    from apps.reserve.models import ReserveEntry

    document = _document(
        "Сводка по подразделению",
        "Состав, участие в резерве и развитие сотрудников",
        user,
        params,
    )

    employees = _scoped_employees(user)
    entries = {
        entry.employee_id: entry
        for entry in ReserveEntry.objects.in_reserve().with_related().filter(employee__in=employees)
    }
    plans = {
        plan.employee_id: plan
        for plan in DevelopmentPlan.objects.filter(employee__in=employees, status=PlanStatus.ACTIVE)
    }

    document.metrics = [
        Metric("Сотрудников", employees.count(), ValueKind.INTEGER),
        Metric("В кадровом резерве", len(entries), ValueKind.INTEGER),
        Metric("С действующим планом развития", len(plans), ValueKind.INTEGER),
    ]

    table = Table(
        title="Сотрудники",
        columns=[
            Column("employee", "Сотрудник", width=26),
            Column("position", "Должность", width=28),
            Column("hired", "В организации с", ValueKind.DATE, width=14),
            Column("reserve", "Резерв", width=24),
            Column("index", "Индекс готовности", ValueKind.NUMBER, width=12),
            Column("plan", "План развития", width=16),
            Column("completion", "Выполнение", ValueKind.PERCENT, width=12),
        ],
    )
    for employee in employees.order_by("last_name"):
        entry = entries.get(employee.pk)
        plan = plans.get(employee.pk)
        table.rows.append(
            {
                "employee": employee.full_name,
                "position": employee.position_title or str(employee.position),
                "hired": employee.hire_date,
                "reserve": str(entry.target_position) if entry else "",
                "index": entry.readiness_index if entry else None,
                "plan": "есть" if plan else "",
                "completion": plan.completion_rate if plan else None,
            }
        )
    document.add_table(table)
    return document


# ---------------------------------------------------------------------------
# Реестр сборщиков
# ---------------------------------------------------------------------------
#: Соответствие кода сборщика и функции.
BUILDERS: dict[str, Builder] = {
    "reserve_roster": reserve_roster,
    "readiness_ranking": readiness_ranking,
    "succession_coverage": succession_coverage,
    "appointments_log": appointments_log,
    "development_progress": development_progress,
    "mentorship_report": mentorship_report,
    "assessment_summary": assessment_summary,
    "nine_box_distribution": nine_box_distribution,
    "reserve_health": reserve_health_report,
    "unit_summary": unit_summary,
}


def get_builder(code: str) -> Builder:
    """Возвращает сборщик по коду."""
    builder = BUILDERS.get(code)
    if builder is None:
        raise DatasetError(f"Сборщик отчёта не найден: {code}")
    return builder
