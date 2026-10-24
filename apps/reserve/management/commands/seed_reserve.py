"""
Наполнение кадрового резерва: программы, заявки, записи, движение статусов.

Запуск::

    uv run python manage.py seed_reserve
    uv run python manage.py seed_reserve --seed 42
    uv run python manage.py seed_reserve --flush
"""

from __future__ import annotations

import datetime as dt
import random
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.competencies.services import build_profiles, resolve_competency_model
from apps.org.models import Position
from apps.people.models import Employee
from apps.reserve.models import (
    NominationKind,
    NominationStatus,
    ReserveEntry,
    ReserveGroup,
    ReserveGroupKind,
    ReserveMembership,
    ReserveNomination,
    ReserveStatus,
)
from apps.reserve.services import (
    DEFAULT_REVIEW_MONTHS,
    MAX_ACTIVE_TARGETS,
    ReserveError,
    change_status,
    create_entry,
    refresh_readiness_bulk,
    rerank_position,
)
from core.utils import shift_months

#: Значение генератора по умолчанию — то же, что в остальных командах
#: наполнения: демонстрационный стенд должен воспроизводиться целиком.
DEFAULT_SEED = 20260828

#: Программы подготовки.
GROUPS: tuple[dict, ...] = (
    {
        "code": "operational",
        "name": "Оперативный резерв",
        "kind": ReserveGroupKind.OPERATIONAL,
        "description": (
            "Кандидаты, способные принять должность в течение года. "
            "Подготовка сосредоточена на замещении и вводе в роль."
        ),
        "horizon_months": 12,
        "min_tenure_months": 12,
        "min_readiness_index": 60,
        "capacity": 60,
        "sort_order": 10,
    },
    {
        "code": "strategic",
        "name": "Стратегический резерв",
        "kind": ReserveGroupKind.STRATEGIC,
        "description": (
            "Подготовка на должности верхнего уровня с горизонтом два-три года. "
            "Содержание — ротация, проектное руководство, обучение."
        ),
        "horizon_months": 36,
        "min_tenure_months": 24,
        "min_readiness_index": 45,
        "capacity": 40,
        "sort_order": 20,
    },
    {
        "code": "managerial",
        "name": "Управленческий резерв",
        "kind": ReserveGroupKind.MANAGERIAL,
        "description": (
            "Специалисты без опыта руководства, готовящиеся к первой управленческой роли."
        ),
        "horizon_months": 24,
        "min_tenure_months": 18,
        "min_readiness_index": 50,
        "capacity": 50,
        "sort_order": 30,
    },
    {
        "code": "youth",
        "name": "Молодёжный резерв",
        "kind": ReserveGroupKind.YOUTH,
        "description": (
            "Сотрудники до 35 лет с высоким потенциалом. Программа "
            "рассчитана на удержание и раннее развитие."
        ),
        "horizon_months": 36,
        "max_age": 35,
        "min_tenure_months": 6,
        "min_readiness_index": 40,
        "capacity": 45,
        "sort_order": 40,
    },
    {
        "code": "professional",
        "name": "Профессиональный резерв",
        "kind": ReserveGroupKind.PROFESSIONAL,
        "description": (
            "Замещение ключевых экспертных должностей, не связанных с руководством людьми."
        ),
        "horizon_months": 18,
        "min_tenure_months": 12,
        "min_readiness_index": 55,
        "capacity": 35,
        "sort_order": 50,
    },
)

#: Сколько кандидатов рассматривается на одну целевую должность.
CANDIDATES_PER_POSITION = (2, 4)

#: Нижняя граница соответствия, при которой кандидат вообще рассматривается.
MIN_MATCH = 40.0

#: Распределение исходов по заявкам.
OUTCOME_WEIGHTS: dict[str, float] = {
    "approved": 0.62,
    "hr_review": 0.14,
    "manager_review": 0.12,
    "rejected": 0.09,
    "withdrawn": 0.03,
}

#: Доля одобренных записей, доведённых до состояния «в резерве».
ACTIVATION_RATE = 0.72

#: Доля действующих записей, у которых срок пересмотра уже прошёл. Очередь
#: пересмотра, всегда пустая, не показывает назначения страницы.
OVERDUE_SHARE = 0.12

#: Доля записей, закрытых исключением из резерва. Резерв без выбытия
#: показывает только рост, а метрика оттока не считается вовсе.
EXCLUSION_SHARE = 0.08

#: Формулировки обоснований.
JUSTIFICATIONS: tuple[str, ...] = (
    "Дважды замещал руководителя на период отпуска: подразделение отработало "
    "период без срывов сроков, претензий от смежных служб не поступало.",
    "Ведёт направление с наибольшей нагрузкой, самостоятельно перестроил "
    "распределение задач в группе — время обработки заявок сократилось.",
    "Наставник трёх новых сотрудников за прошедший год, все прошли "
    "испытательный срок. Умеет объяснять и требовать одновременно.",
    "Инициировал и довёл до внедрения изменение регламента приёмки, которое "
    "сняло повторяющиеся разногласия со складом.",
    "Устойчиво выполняет плановые показатели два года подряд, при этом берёт "
    "на себя задачи вне прямых обязанностей и доводит их до результата.",
    "Единственный в подразделении владеет полным циклом процесса: замещение "
    "на этой должности без него сейчас невозможно.",
    "Ведёт переговоры с ключевыми контрагентами без участия руководителя, "
    "результат переговоров устраивает обе стороны.",
    "Разобрался в смежной области и закрыл провал, возникший после ухода "
    "коллеги; сделал это по собственной инициативе.",
)

#: Формулировки отказов. Отказ без объяснения кандидату непонятен, и в
#: демонстрационных данных он должен выглядеть так же, как в работе.
REJECTIONS: tuple[str, ...] = (
    "Не выполнены критичные требования целевой должности; вернуться к "
    "рассмотрению после закрытия разрывов.",
    "Стаж в текущей должности менее года: рекомендуется повторное выдвижение через полгода.",
    "На должность уже подготовлены два кандидата с более высоким индексом; "
    "предложено рассмотреть смежную роль.",
    "Оценка компетенций не завершена: соответствие подтверждено менее чем "
    "наполовину, решение отложено до окончания кампании.",
)

#: Основания исключения из резерва.
EXCLUSIONS: tuple[str, ...] = (
    "Отказ кандидата от подготовки по личным обстоятельствам.",
    "Целевая должность выведена из штатного расписания.",
    "Пересмотр состава: за два года подготовки готовность не выросла.",
)


#: Направления деятельности по префиксу кода подразделения: в резерв выдвигаются
#: сотрудники своего направления, а не любого подразделения с похожим профилем.
FAMILIES = {
    "dev": "it",
    "it": "it",
    "sales": "sales",
    "marketing": "sales",
    "finance": "finance",
    "accounting": "finance",
    "logistics": "supply",
    "warehouse": "supply",
    "supply": "supply",
    "facility": "operations",
    "labor": "operations",
    "security": "operations",
}


def department_family(department) -> str | None:
    """Направление подразделения; без подразделения ограничения нет."""
    if department is None:
        return None
    prefix = department.code.split("-")[0]
    return FAMILIES.get(prefix, prefix)


class Command(BaseCommand):
    """Наполняет кадровый резерв записями, заявками и историей решений."""

    help = "Создаёт программы подготовки, заявки на выдвижение и записи резерва"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
        parser.add_argument(
            "--flush",
            action="store_true",
            help="удалить порождённые записи, сохранив перенесённые из дампа",
        )
        parser.add_argument(
            "--positions",
            type=int,
            default=32,
            help="сколько целевых должностей охватить резервом",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        """Наполняет резерв целиком в одной транзакции."""
        self.random = random.Random(options["seed"])  # noqa: S311  # nosec B311
        self.today = timezone.localdate()

        if options["flush"]:
            self._flush()

        self.actor = self._resolve_actor()
        groups = self._create_groups()
        positions = self._target_positions(options["positions"])
        if not positions:
            raise CommandError(
                "Не найдено ни одной должности с моделью компетенций. "
                "Выполните `seed_competencies` до наполнения резерва."
            )

        nominations = self._create_nominations(positions, groups)
        entries = self._materialize(nominations, groups)
        self._age_entries(entries)

        # Перенесённые из дампа записи создавались до появления моделей компетенций и индекса не
        # имеют.
        refreshed = refresh_readiness_bulk()

        for position_id in {entry.target_position_id for entry in entries}:
            rerank_position(position_id)

        self.stdout.write(self.style.SUCCESS("\nКадровый резерв наполнен."))
        self.stdout.write(
            f"  Программ подготовки: {ReserveGroup.objects.count()}\n"
            f"  Целевых должностей: {len(positions)}\n"
            f"  Заявок: {ReserveNomination.objects.count()}"
            f" (на согласовании — {ReserveNomination.objects.pending().count()})\n"
            f"  Записей резерва: {ReserveEntry.objects.count()}"
            f" (действующих — {ReserveEntry.objects.in_reserve().count()})\n"
            f"  Пересчитано индексов готовности: {refreshed}\n"
            f"  Готовы к назначению: {ReserveEntry.objects.ready_now().count()}\n"
            f"  Участий в программах: {ReserveMembership.objects.count()}"
        )

    # -- Подготовка ---------------------------------------------------------
    def _flush(self) -> None:
        """Удаляет порождённые записи резерва."""
        ReserveNomination.objects.all().delete()
        ReserveMembership.objects.all().delete()
        removed, _ = ReserveEntry.objects.filter(legacy_id__isnull=True).delete()
        self.stdout.write(self.style.WARNING(f"Удалено порождённых записей: {removed}"))

    def _resolve_actor(self):
        """Пользователь, от имени которого принимаются решения."""
        from apps.accounts.models import User
        from core.constants import Role

        return (
            User.objects.filter(role=Role.HR, is_active=True).first()
            or User.objects.filter(role=Role.ADMIN, is_active=True).first()
        )

    def _create_groups(self) -> list[ReserveGroup]:
        """Создаёт программы подготовки, не трогая уже существующие."""
        groups: list[ReserveGroup] = []
        for spec in GROUPS:
            group, _created = ReserveGroup.objects.update_or_create(
                code=spec["code"], defaults=spec
            )
            groups.append(group)
        return groups

    def _target_positions(self, limit: int) -> list[Position]:
        """Должности, на которые формируется резерв."""
        queryset = (
            Position.objects.filter(is_active=True)
            .select_related("grade", "department")
            .order_by("-grade__level", "name")
        )
        selected = [
            position
            for position in queryset
            if position.is_key_position or (position.grade and position.grade.level >= 50)
        ]
        # Должности без модели компетенций отсеиваются: соответствие для них не вычисляется, и
        # резервисты получили бы нулевой индекс — картина хуже отсутствия записей.
        with_model = [
            position
            for position in selected
            if resolve_competency_model(position_id=position.pk, grade_id=position.grade_id)
        ]
        return with_model[:limit]

    # -- Заявки --------------------------------------------------------------
    def _create_nominations(
        self, positions: list[Position], groups: list[ReserveGroup]
    ) -> list[tuple[ReserveNomination, str]]:
        """Порождает заявки на выдвижение по каждой целевой должности."""
        pool = list(Employee.objects.reserve_eligible().with_org())
        taken: set[tuple[int, int]] = set()
        result: list[tuple[ReserveNomination, str]] = []
        # Счётчик целей ведётся в памяти, а не запросом на каждого кандидата: проверка по базе
        # стоила бы запроса на каждую из тысяч рассмотренных пар «сотрудник — должность».
        targets: Counter[int] = Counter()

        for position in positions:
            model = resolve_competency_model(position_id=position.pk, grade_id=position.grade_id)
            if model is None:
                continue

            family = department_family(position.department)
            contenders = [
                employee
                for employee in pool
                if employee.position_id != position.pk
                and (employee.pk, position.pk) not in taken
                and (family is None or department_family(employee.department) == family)
            ]
            profiles = build_profiles(contenders, model)
            ranked = sorted(
                (
                    (employee, profiles[employee.pk])
                    for employee in contenders
                    if employee.pk in profiles and profiles[employee.pk].match_score >= MIN_MATCH
                ),
                key=lambda pair: -pair[1].match_score,
            )

            wanted = self.random.randint(*CANDIDATES_PER_POSITION)
            accepted = 0

            # Круг рассмотрения шире требуемого числа и перемешивается.
            window = ranked[: max(wanted * 8, 24)]
            self.random.shuffle(window)

            for employee, _profile in window:
                if accepted >= wanted:
                    break
                if targets[employee.pk] >= MAX_ACTIVE_TARGETS:
                    continue

                nomination = self._create_nomination(employee, position, groups)
                if nomination is None:
                    continue

                taken.add((employee.pk, position.pk))
                targets[employee.pk] += 1
                accepted += 1
                result.append(nomination)

        return result

    def _create_nomination(
        self, employee: Employee, position: Position, groups: list[ReserveGroup]
    ) -> tuple[ReserveNomination, str] | None:
        """Создаёт одну заявку и доводит её до назначенного исхода."""
        outcome = self._pick_outcome()
        kind = self.random.choices(
            [NominationKind.MANAGER, NominationKind.SELF, NominationKind.HR],
            weights=[0.6, 0.25, 0.15],
        )[0]
        manager = self._manager_user(employee)
        # Инициатор соответствует виду заявки; без учётной записи у сотрудника или без
        # руководителя заявку подаёт кадровая служба.
        if kind == NominationKind.SELF and employee.user is None:
            kind = NominationKind.MANAGER
        if kind == NominationKind.MANAGER and manager is None:
            kind = NominationKind.HR
        author = {
            NominationKind.SELF: employee.user,
            NominationKind.MANAGER: manager,
        }.get(kind, self.actor)

        group = self._group_for(employee, groups)
        # Заявка на рассмотрении подана недавно, решённая — в течение последнего года.
        pending = outcome in {"manager_review", "hr_review"}
        submitted = timezone.now() - dt.timedelta(
            days=self.random.randint(2, 40) if pending else self.random.randint(30, 400)
        )

        nomination = ReserveNomination.objects.create(
            employee=employee,
            target_position=position,
            reserve_group=group,
            kind=kind,
            status=NominationStatus.SUBMITTED,
            nominated_by=author,
            justification=self.random.choice(JUSTIFICATIONS),
            achievements="",
            submitted_at=submitted,
        )
        self._advance(nomination, outcome, submitted, manager)
        return nomination, outcome

    @staticmethod
    def _manager_user(employee: Employee):
        """Учётная запись непосредственного руководителя либо руководителя подразделения."""
        manager = employee.manager
        if manager is not None and manager.user_id is not None:
            return manager.user
        department = employee.department
        head = department.head if department is not None else None
        if head is not None and head.pk != employee.user_id:
            return head
        return None

    def _pick_outcome(self) -> str:
        """Выбирает исход заявки по заданному распределению."""
        options = list(OUTCOME_WEIGHTS)
        weights = [OUTCOME_WEIGHTS[option] for option in options]
        return self.random.choices(options, weights=weights)[0]

    def _advance(
        self, nomination: ReserveNomination, outcome: str, submitted: dt.datetime, manager
    ) -> None:
        """Доводит заявку до назначенного исхода, заполняя ступени маршрута."""
        # Руководитель-инициатор согласует заявку подачей; без руководителя ступень пропускается.
        by_manager = manager is not None and nomination.nominated_by_id == manager.pk
        if outcome == "manager_review" and manager is not None and not by_manager:
            nomination.status = NominationStatus.MANAGER_REVIEW
            nomination.save(update_fields=["status"])
            return
        if outcome == "manager_review":
            outcome = "hr_review"

        if manager is not None:
            nomination.manager_reviewer = manager
            if by_manager:
                nomination.manager_decision_at = submitted
                nomination.manager_comment = "Заявка подана непосредственным руководителем"
            else:
                nomination.manager_decision_at = submitted + dt.timedelta(
                    days=self.random.randint(1, 6)
                )
                nomination.manager_comment = "Кандидатура согласована, готов отпустить сотрудника."

        if outcome == "hr_review":
            nomination.status = NominationStatus.HR_REVIEW
        elif outcome == "withdrawn":
            nomination.status = NominationStatus.WITHDRAWN
            nomination.rejection_reason = "Отозвана инициатором до решения кадровой службы."
        elif outcome == "rejected":
            nomination.status = NominationStatus.REJECTED
            nomination.hr_reviewer = self.actor
            nomination.hr_decision_at = (
                nomination.manager_decision_at or submitted
            ) + dt.timedelta(days=self.random.randint(1, 10))
            nomination.rejection_reason = self.random.choice(REJECTIONS)
            nomination.hr_comment = nomination.rejection_reason
        else:
            nomination.status = NominationStatus.HR_REVIEW

        nomination.save()

    def _group_for(self, employee: Employee, groups: list[ReserveGroup]) -> ReserveGroup:
        """Подбирает программу подготовки по правилам самой программы."""
        age = employee.age
        tenure = employee.tenure_months
        suitable = [
            group
            for group in groups
            if (group.max_age is None or (age is not None and age <= group.max_age))
            and tenure >= group.min_tenure_months
        ]
        return self.random.choice(suitable or groups)

    # -- Записи ---------------------------------------------------------------
    def _materialize(
        self, nominations: list[tuple[ReserveNomination, str]], groups: list[ReserveGroup]
    ) -> list[ReserveEntry]:
        """Создаёт записи резерва по одобренным заявкам."""
        entries: list[ReserveEntry] = []

        for nomination, outcome in nominations:
            if outcome != "approved":
                continue

            try:
                entry = create_entry(
                    employee=nomination.employee,
                    position=nomination.target_position,
                    group=nomination.reserve_group,
                    actor=self.actor,
                    justification=nomination.justification,
                    review_months=DEFAULT_REVIEW_MONTHS,
                )
            except ReserveError:
                # Две одобренные заявки одного сотрудника на одну должность: случайный отбор
                # кандидатов способен породить такую пару.
                continue

            # Момент подачи заявки известен: он проставлен при её создании несколькими строками
            # выше.
            submitted_at = nomination.submitted_at or timezone.now()
            included = (submitted_at + dt.timedelta(days=14)).date()
            ReserveEntry.objects.filter(pk=entry.pk).update(
                inclusion_date=included,
                review_date=shift_months(included, DEFAULT_REVIEW_MONTHS),
            )
            entry.inclusion_date = included
            entry.review_date = shift_months(included, DEFAULT_REVIEW_MONTHS)

            nomination.status = NominationStatus.APPROVED
            nomination.hr_reviewer = self.actor
            nomination.hr_decision_at = submitted_at + dt.timedelta(days=12)
            nomination.hr_comment = "Кандидат соответствует требованиям, зачислен в резерв."
            nomination.entry = entry
            nomination.save()

            entries.append(entry)

        return entries

    def _age_entries(self, entries: list[ReserveEntry]) -> None:
        """Приводит записи в состояния, встречающиеся в работе."""
        for entry in entries:
            if self.random.random() > ACTIVATION_RATE:
                continue

            try:
                change_status(
                    entry,
                    ReserveStatus.ACTIVE,
                    actor=self.actor,
                    reason="Оценка завершена, кандидат переведён в состав резерва.",
                )
            except ReserveError:
                continue

            if self.random.random() < EXCLUSION_SHARE:
                change_status(
                    entry,
                    ReserveStatus.EXCLUDED,
                    actor=self.actor,
                    reason=self.random.choice(EXCLUSIONS),
                )
                continue

            if self.random.random() < OVERDUE_SHARE:
                overdue = self.today - dt.timedelta(days=self.random.randint(10, 120))
                ReserveEntry.objects.filter(pk=entry.pk).update(review_date=overdue)

            expected = shift_months(
                entry.inclusion_date,
                entry.reserve_group.horizon_months if entry.reserve_group else 24,
            )
            ReserveEntry.objects.filter(pk=entry.pk).update(expected_ready_date=expected)
