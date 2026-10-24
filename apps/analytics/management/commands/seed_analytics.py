"""
Наполнение аналитического ядра: схемы весов, матрица 9-box, расчёт индексов.

Запуск::

    uv run python manage.py seed_analytics
    uv run python manage.py seed_analytics --seed 42
    uv run python manage.py seed_analytics --flush
    uv run python manage.py seed_analytics --skip-history
"""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.analytics.models import (
    AggregationMethod,
    CalibrationSession,
    CalibrationStatus,
    NineBoxPlacement,
    ReadinessSnapshot,
    ScoringCriterion,
    ScoringScheme,
    ScoringWeight,
    WeightingMethod,
)
from apps.analytics.services import (
    AnalyticsError,
    auto_place,
    build_ahp_weights,
    build_entropy_weights,
    calibrate,
    complete_calibration,
    ensure_criteria,
    ensure_default_scheme,
    recalculate,
)
from apps.org.models import Branch
from apps.people.models import Employee
from apps.reserve.models import ReserveEntry
from core.utils import shift_months

#: Значение генератора по умолчанию — общее для всех команд наполнения.
DEFAULT_SEED = 20260828

#: Суждения эксперта в шкале Саати для верхнего треугольника матрицы парных сравнений шести
#: критериев.
AHP_JUDGEMENTS: dict[tuple[int, int], float] = {
    (0, 1): 2,
    (0, 2): 2,
    (0, 3): 3,
    (0, 4): 3,
    (0, 5): 4,
    (1, 2): 1,
    (1, 3): 2,
    (1, 4): 2,
    (1, 5): 3,
    (2, 3): 1,
    (2, 4): 2,
    (2, 5): 2,
    (3, 4): 1,
    (3, 5): 2,
    (4, 5): 1,
}

#: Доля размещений в матрице 9-box, пересматриваемых на калибровке.
CALIBRATION_ADJUST_SHARE = 0.26

#: Наибольший сдвиг оценки на калибровке, баллы.
CALIBRATION_MAX_SHIFT = 14

#: Число синтезируемых исторических расчётов на запись резерва, считая текущий.
HISTORY_POINTS = 4

#: Шаг истории, месяцев: расчёт индекса выполняется поквартально.
HISTORY_STEP_MONTHS = 3

#: Обоснования корректировки оценки на сессии калибровки.
CALIBRATION_NOTES: tuple[str, ...] = (
    "Результаты года достигнуты за счёт проекта, начатого предшественником; "
    "самостоятельный вклад ниже первоначальной оценки.",
    "Руководитель оценивал по своей шкале выше среднего по организации: "
    "сопоставление с коллегами того же уровня опустило оценку.",
    "Сотрудник закрыл два направления, оставшиеся без руководителя; "
    "результативность поднята с учётом фактического объёма ответственности.",
    "Потенциал поднят: за год освоена смежная функция без формального обучения, "
    "что тестами не измеряется.",
    "Потенциал снижен: рост остановился на текущей роли, интерес к расширению "
    "зоны ответственности не подтверждён.",
    "Оценка выровнена с сопоставимой должностью в другом филиале: расхождение "
    "объяснялось различием требовательности руководителей, а не сотрудников.",
)

#: Повестка сессий калибровки по филиалам.
SESSION_AGENDA = (
    "Сверка оценок результативности и потенциала руководителями подразделений. "
    "Рассматриваются расхождения предварительного размещения с суждением "
    "непосредственного руководителя, спорные случаи выносятся на обсуждение."
)


class Command(BaseCommand):
    """Наполняет аналитическое ядро: схемы весов, матрицу 9-box, расчёты индекса."""

    help = "Создаёт схемы оценивания, размещения в матрице 9-box и историю расчётов индекса"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
        parser.add_argument(
            "--flush",
            action="store_true",
            help="удалить схемы, размещения и расчёты перед наполнением",
        )
        parser.add_argument(
            "--skip-history",
            action="store_true",
            help="не синтезировать историю расчётов индекса",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        """Наполняет аналитическое ядро целиком в одной транзакции."""
        self.random = random.Random(options["seed"])  # noqa: S311  # nosec B311
        self.today = timezone.localdate()
        self.actor = self._resolve_actor()

        if options["flush"]:
            self._flush()

        criteria = ensure_criteria()
        default_scheme = ensure_default_scheme(author=self.actor)
        self.stdout.write(f"  Критериев готовности: {len(criteria)}")

        placed = self._place_employees()
        sessions = self._run_calibrations()
        result = recalculate(actor=self.actor, notify_changes=False)

        ahp = self._build_ahp_scheme(criteria)
        entropy = self._build_entropy_scheme(criteria)
        topsis = self._build_topsis_scheme(default_scheme)

        history = 0 if options["skip_history"] else self._synthesize_history(default_scheme)

        self.stdout.write(self.style.SUCCESS("\nАналитическое ядро наполнено."))
        self.stdout.write(
            f"  Схем оценивания: {ScoringScheme.objects.count()}"
            f" (по умолчанию — {default_scheme.code})\n"
            f"  Метод анализа иерархий: CR = {ahp}\n"
            f"  Энтропийная схема: {entropy}\n"
            f"  Схема со свёрткой TOPSIS: {topsis}\n"
            f"  Размещений в матрице 9-box: {placed['placed']}"
            f" (без данных — {placed['no_data']}, сохранено решений — {placed['skipped']})\n"
            f"  Сессий калибровки: {sessions['sessions']}"
            f" (скорректировано оценок — {sessions['adjusted']})\n"
            f"  Рассчитано индексов: {result.entries_updated}"
            f" (средний — {result.average_index},"
            f" обеспеченность — {round(result.average_coverage * 100, 1)} %)\n"
            f"  Синтезировано исторических расчётов: {history}"
        )
        if result.unreliable:
            self.stdout.write(
                self.style.WARNING(
                    f"  Записей с обеспеченностью ниже двух третей: {result.unreliable}. "
                    "Это состояние данных, а не сбой: часть критериев не обеспечена "
                    "исходными данными и в свёртке не участвует."
                )
            )

    # -- Подготовка ----------------------------------------------------------
    def _resolve_actor(self):
        """Пользователь кадровой службы, от имени которого ведётся расчёт."""
        from apps.accounts.models import User
        from core.constants import Role

        return (
            User.objects.filter(role=Role.HR, is_active=True).first()
            or User.objects.filter(role=Role.ADMIN, is_active=True).first()
        )

    def _flush(self) -> None:
        """Удаляет расчёты, размещения и схемы, кроме справочника критериев."""
        ReadinessSnapshot.objects.all().delete()
        NineBoxPlacement.objects.all().delete()
        CalibrationSession.objects.all().delete()
        ScoringWeight.objects.all().delete()
        ScoringScheme.objects.all().delete()
        Employee.objects.filter(is_key_talent=True).update(is_key_talent=False)
        self.stdout.write(self.style.WARNING("Схемы, размещения и расчёты удалены"))

    # -- Матрица 9-box -------------------------------------------------------
    def _place_employees(self) -> dict[str, int]:
        """Проставляет предварительные размещения по данным системы."""
        employees = list(
            Employee.objects.active()
            .filter(is_external=False)
            .select_related("position", "position__grade")
        )
        summary = auto_place(employees, overwrite=True)

        period_start = dt.date(self.today.year - 1, 1, 1)
        period_end = dt.date(self.today.year - 1, 12, 31)
        NineBoxPlacement.objects.filter(is_current=True, period_start__isnull=True).update(
            period_start=period_start, period_end=period_end
        )
        return summary

    def _run_calibrations(self) -> dict[str, int]:
        """Проводит сессии калибровки по филиалам."""
        summary = {"sessions": 0, "adjusted": 0}
        facilitator = self.actor

        for branch in Branch.objects.all():
            placements = list(
                NineBoxPlacement.objects.filter(
                    is_current=True, employee__branch=branch
                ).select_related("employee")
            )
            if len(placements) < 5:
                continue

            session = CalibrationSession.objects.create(
                name=f"Калибровка оценок — {branch.name}",
                description=(
                    f"Сверка результативности и потенциала по филиалу «{branch.name}» "
                    f"за {self.today.year - 1} год."
                ),
                held_on=shift_months(self.today, -2),
                status=CalibrationStatus.IN_PROGRESS,
                branch=branch,
                facilitator=facilitator,
                agenda=SESSION_AGENDA,
            )
            summary["sessions"] += 1

            count = max(1, round(len(placements) * CALIBRATION_ADJUST_SHARE))
            for placement in self.random.sample(placements, count):
                shift_performance = self.random.uniform(
                    -CALIBRATION_MAX_SHIFT, CALIBRATION_MAX_SHIFT
                )
                shift_potential = self.random.uniform(-CALIBRATION_MAX_SHIFT, CALIBRATION_MAX_SHIFT)
                calibrate(
                    placement,
                    performance=float(placement.performance) + shift_performance,
                    potential=float(placement.potential) + shift_potential,
                    comment=self.random.choice(CALIBRATION_NOTES),
                    actor=facilitator,
                    session=session,
                )
                summary["adjusted"] += 1

            # Размещения филиала связываются с сессией целиком: на сверке рассматривались все, а не
            # только изменённые, и протокол должен это отражать.
            NineBoxPlacement.objects.filter(
                is_current=True, employee__branch=branch, session__isnull=True
            ).update(session=session)

            complete_calibration(
                session,
                decisions=(
                    f"Рассмотрено размещений: {len(placements)}. "
                    f"Изменено по итогам обсуждения: {count}. "
                    "Остальные оценки подтверждены без изменений."
                ),
                actor=facilitator,
            )
        return summary

    # -- Схемы весов ---------------------------------------------------------
    def _build_ahp_scheme(self, criteria: list[ScoringCriterion]) -> str:
        """Строит схему методом анализа иерархий из зафиксированных суждений."""
        scheme, _created = ScoringScheme.objects.get_or_create(
            code="ahp-hierarchy",
            defaults={
                "name": "Метод анализа иерархий",
                "description": (
                    "Веса выведены из матрицы парных сравнений критериев по шкале "
                    "Саати. Субъективность эксперта сохраняется, но становится "
                    "измеримой: отношение согласованности показывает, насколько его "
                    "суждения непротиворечивы."
                ),
                "method": WeightingMethod.AHP,
                "aggregation": AggregationMethod.WEIGHTED_SUM,
                "is_active": True,
                "created_by": self.actor,
            },
        )
        outcome = build_ahp_weights(
            scheme,
            AHP_JUDGEMENTS,
            criteria,
            note=(
                "Суждения получены от методолога кадровой службы при внедрении "
                "системы. Шкала Саати, верхний треугольник матрицы; нижний "
                "достроен обратной симметрией."
            ),
        )
        # Греческая «лямбда» в вывод не попадает: консоль Windows работает в
        # кодировке cp1251, и знак вне её набора обрывает вывод команды целиком.
        return (
            f"{round(outcome.consistency_ratio, 4)} "
            f"(наибольшее собственное значение — {round(outcome.lambda_max, 3)})"
        )

    def _build_entropy_scheme(self, criteria: list[ScoringCriterion]) -> str:
        """Строит схему энтропийным методом по фактическому разбросу данных."""
        scheme, _created = ScoringScheme.objects.get_or_create(
            code="entropy-data",
            defaults={
                "name": "Энтропийная схема",
                "description": (
                    "Веса выведены из данных: критерий, слабо различающий "
                    "кандидатов, получает малый вес. Эксперт в расчёте не "
                    "участвует — вместе с субъективностью исчезают и приоритеты "
                    "организации."
                ),
                "method": WeightingMethod.ENTROPY,
                "aggregation": AggregationMethod.WEIGHTED_SUM,
                "is_active": True,
                "created_by": self.actor,
            },
        )
        try:
            outcome = build_entropy_weights(scheme, criteria)
        except AnalyticsError as error:
            scheme.is_active = False
            scheme.source_note = f"Веса не выведены: {error}"
            scheme.save(update_fields=["is_active", "source_note", "updated_at"])
            return f"не построена — {error}"

        strongest = max(outcome.rows, key=lambda row: row.weight)
        return (
            f"выборка {outcome.sample} из {outcome.sample_total} записей "
            f"({outcome.sample_share} %), наибольший вес — "
            f"{strongest.criterion.name}, {strongest.weight}"
        )

    def _build_topsis_scheme(self, source: ScoringScheme) -> str:
        """Повторяет веса экспертной схемы со свёрткой методом TOPSIS."""
        scheme, created = ScoringScheme.objects.get_or_create(
            code="expert-topsis",
            defaults={
                "name": "Экспертные веса, свёртка TOPSIS",
                "description": (
                    "Те же веса, что в базовой экспертной схеме, но ранжирование "
                    "выполняется по близости к идеальному решению. Сравнение с "
                    "базовой схемой показывает вклад способа свёртки отдельно от "
                    "вклада весов."
                ),
                "method": WeightingMethod.EXPERT,
                "aggregation": AggregationMethod.TOPSIS,
                "is_active": True,
                "created_by": self.actor,
                "source_note": (
                    "Веса скопированы из схемы «Экспертная схема (базовая)» без "
                    "изменений: различие схем сведено к способу свёртки."
                ),
            },
        )
        if created or not scheme.weights.exists():
            scheme.weights.all().delete()
            for weight in source.weights.all():
                ScoringWeight.objects.create(
                    scheme=scheme,
                    criterion=weight.criterion,
                    weight=weight.weight,
                    raw_value=weight.raw_value,
                    rank=weight.rank,
                    comment="Копия веса экспертной схемы",
                )
            scheme.calculated_at = timezone.now()
            scheme.save(update_fields=["calculated_at", "updated_at"])
        return f"{scheme.weights.count()} весов, свёртка {scheme.get_aggregation_display()}"

    # -- История расчётов ----------------------------------------------------
    def _synthesize_history(self, scheme: ScoringScheme) -> int:
        """Достраивает поквартальную историю индекса обратным ходом от текущего."""
        entries = list(ReserveEntry.objects.in_reserve().exclude(readiness_index__isnull=True))
        if not entries:
            return 0

        snapshots: list[ReadinessSnapshot] = []
        now = timezone.now()

        for entry in entries:
            current = float(entry.readiness_index or 0)
            # Темп роста: у большинства положительный, у пятой части — нулевой
            # или отрицательный.
            quarterly = self.random.choice([2.6, 3.4, 4.1, 1.8, 0.4, -0.9, 2.2, 3.0])
            for step in range(HISTORY_POINTS - 1, 0, -1):
                moment = now - dt.timedelta(days=step * HISTORY_STEP_MONTHS * 30)
                value = current - quarterly * step + self.random.uniform(-1.2, 1.2)
                value = max(0.0, min(100.0, value))
                snapshots.append(
                    ReadinessSnapshot(
                        entry=entry,
                        scheme=scheme,
                        index_value=Decimal(str(round(value, 2))),
                        readiness_level=self._level_of(value),
                        criterion_values={
                            "_meta": {
                                "aggregation": scheme.aggregation,
                                "coverage": 0.0,
                                "reliable": False,
                                "synthetic": True,
                                "note": (
                                    "Демонстрационный расчёт: значение достроено "
                                    "обратным ходом от текущего индекса при "
                                    "наполнении стенда, разбор по критериям не "
                                    "сохранялся."
                                ),
                            }
                        },
                        calculated_at=moment,
                        is_current=False,
                    )
                )

        ReadinessSnapshot.objects.bulk_create(snapshots)
        return len(snapshots)

    @staticmethod
    def _level_of(value: float) -> str:
        """Уровень готовности по порогам организации."""
        from core.constants import ReadinessLevel
        from core.settings_types import get_reservix_settings

        options = get_reservix_settings()
        return ReadinessLevel.from_index(
            value,
            (options["READINESS_THRESHOLD_HIGH"], options["READINESS_THRESHOLD_MEDIUM"]),
        )
