"""
Наполнение преемственности: критичные должности, планы, преемники, назначения.

Запуск::

    uv run python manage.py seed_succession
    uv run python manage.py seed_succession --seed 42
    uv run python manage.py seed_succession --flush
"""

from __future__ import annotations

import datetime as dt
import random

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.people.models import AttritionRisk, Employee
from apps.reserve.models import ReserveEntry, ReserveStatus
from apps.succession.models import (
    Appointment,
    AppointmentKind,
    CriticalityLevel,
    KeyPosition,
    PlanStatus,
    SuccessionPlan,
    SuccessionSlate,
    VacancyRisk,
)
from apps.succession.services import (
    SuccessionError,
    add_successor,
    create_plan,
    register_appointment,
    sync_key_positions,
)
from core.utils import shift_months

#: Значение генератора по умолчанию — общее для всех команд наполнения.
DEFAULT_SEED = 20260828

#: Доля критичных должностей, обеспеченных действующим планом.
PLAN_COVERAGE = 0.72

#: Доля планов, оставшихся черновиками. Черновик — нормальное состояние: план
#: готовится, преемники подбираются, в действие он ещё не введён.
DRAFT_SHARE = 0.15

#: Доля планов, закрытых после назначения. Нужна, чтобы отбор по состоянию
#: плана и разбор истории решений имели материал.
CLOSED_SHARE = 0.08

#: Сколько назначений порождается для демонстрации результативности.
APPOINTMENTS = 26

#: Доля назначений, сделанных из резерва.
FROM_RESERVE_SHARE = 0.46

#: Описания последствий ухода.
IMPACT_TEMPLATES: tuple[str, ...] = (
    "Останавливается согласование договоров с ключевыми контрагентами: "
    "полномочия не делегированы, замещение потребует доверенности и месяца "
    "на ввод в контекст.",
    "Прерывается планирование загрузки подразделения. Восстановление "
    "потребует не менее двух месяцев: методика планирования нигде не описана "
    "и держится на личном опыте.",
    "Теряется единственный носитель договорённостей с регуляторами и "
    "смежными организациями. Формальные процедуры сохранятся, скорость "
    "прохождения вопросов упадёт кратно.",
    "Приостанавливается сопровождение учётной системы: детали настройки не "
    "документированы, восстановление по журналам займёт около квартала.",
    "Останавливается работа с крупнейшими клиентами направления. Риск потери "
    "части клиентской базы в течение полугода оценивается как высокий.",
)

#: Основания критичности.
JUSTIFICATION_TEMPLATES: tuple[str, ...] = (
    "Должность замыкает на себя решения, влияющие на выполнение обязательств "
    "перед клиентами, и не имеет полноценного дублирования.",
    "Единственная должность в организации с данным сочетанием полномочий и "
    "квалификации: подбор на внешнем рынке занимает не менее четырёх месяцев.",
    "Ключевое звено в цепочке принятия решений: задержка на этом уровне "
    "останавливает работу трёх смежных подразделений.",
)

#: Риски исполнения планов преемственности.
RISK_TEMPLATES: tuple[str, ...] = (
    "Первый преемник рассматривает предложения вне организации; вероятность "
    "выбытия из плана в течение года оценивается как средняя.",
    "Подготовка требует ротации, свободных ролей для неё в горизонте года не запланировано.",
    "Бюджет на внешнее обучение по направлению не подтверждён; закрытие "
    "разрывов возможно только внутренними средствами.",
    "Второй и третий преемники отстают от требований на полтора уровня по "
    "критичным компетенциям: скамейка формально есть, реально — один кандидат.",
)

#: Формулировки фокуса подготовки преемника.
FOCUS_TEMPLATES: tuple[str, ...] = (
    "Управление бюджетом подразделения и защита плановых показателей.",
    "Ведение переговоров с внешними контрагентами без сопровождения.",
    "Работа с конфликтами в команде и принятие непопулярных решений.",
    "Планирование на горизонте года и распределение ресурсов между задачами.",
    "Публичное представление результатов подразделения руководству.",
)


class Command(BaseCommand):
    """Наполняет планирование преемственности и историю назначений."""

    help = "Создаёт критичные должности, планы преемственности, преемников и назначения"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
        parser.add_argument(
            "--flush",
            action="store_true",
            help="удалить планы, преемников и назначения перед наполнением",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        """Наполняет блок преемственности целиком в одной транзакции."""
        self.random = random.Random(options["seed"])  # noqa: S311  # nosec B311
        self.today = timezone.localdate()
        self.actor = self._resolve_actor()

        if options["flush"]:
            self._flush()

        synced = sync_key_positions(actor=self.actor)
        self._describe_positions()
        plans = self._create_plans()
        successors = self._fill_slates(plans)
        appointments = self._create_appointments()

        self.stdout.write(self.style.SUCCESS("\nПреемственность наполнена."))
        self.stdout.write(
            f"  Критичных должностей: {KeyPosition.objects.count()}"
            f" (создано сверкой — {synced['created']})\n"
            f"  Планов преемственности: {len(plans)}"
            f" (действующих — {SuccessionPlan.objects.active().count()})\n"
            f"  Преемников: {successors}\n"
            f"  Назначений: {appointments}"
            f" (из резерва — {Appointment.objects.from_reserve().count()})"
        )

    # -- Подготовка ----------------------------------------------------------
    def _resolve_actor(self):
        """Пользователь кадровой службы, от имени которого ведётся план."""
        from apps.accounts.models import User
        from core.constants import Role

        return (
            User.objects.filter(role=Role.HR, is_active=True).first()
            or User.objects.filter(role=Role.ADMIN, is_active=True).first()
        )

    def _flush(self) -> None:
        """Удаляет порождённые планы, преемников и назначения."""
        Appointment.objects.all().delete()
        SuccessionSlate.objects.all().delete()
        SuccessionPlan.objects.all().delete()
        self.stdout.write(self.style.WARNING("Планы, преемники и назначения удалены"))

    def _describe_positions(self) -> None:
        """Расставляет критичность, риск и последствия ухода."""
        for key_position in KeyPosition.objects.select_related(
            "position", "position__grade", "incumbent"
        ):
            level = key_position.position.grade.level if key_position.position.grade_id else 0
            criticality = (
                CriticalityLevel.CRITICAL
                if level >= 90
                else CriticalityLevel.HIGH
                if level >= 70
                else CriticalityLevel.SIGNIFICANT
                if level >= 50
                else CriticalityLevel.MODERATE
            )

            key_position.criticality = criticality
            key_position.vacancy_risk = self._risk_for(key_position.incumbent)
            key_position.impact_of_loss = self.random.choice(IMPACT_TEMPLATES)
            key_position.justification = self.random.choice(JUSTIFICATION_TEMPLATES)
            key_position.required_successors = 2 if criticality < CriticalityLevel.HIGH else 3
            key_position.reviewed_at = self.today - dt.timedelta(days=self.random.randint(10, 200))
            if key_position.vacancy_risk == VacancyRisk.HIGH:
                key_position.vacancy_horizon_months = self.random.choice([6, 12, 18])
            key_position.save()

    def _risk_for(self, incumbent: Employee | None) -> int:
        """Риск освобождения должности по признакам действующего сотрудника."""
        if incumbent is None:
            return VacancyRisk.HIGH

        score = 0
        age = incumbent.age
        if age is not None and age >= 58:
            score += 2
        elif age is not None and age >= 50:
            score += 1

        if incumbent.position_tenure_months >= 84:
            score += 1
        if incumbent.attrition_risk == AttritionRisk.HIGH:
            score += 2
        elif incumbent.attrition_risk == AttritionRisk.MEDIUM:
            score += 1

        if score >= 3:
            return VacancyRisk.HIGH
        if score >= 1:
            return VacancyRisk.MEDIUM
        return VacancyRisk.LOW

    # -- Планы ----------------------------------------------------------------
    def _create_plans(self) -> list[SuccessionPlan]:
        """Создаёт планы преемственности, начиная с наиболее приоритетных."""
        positions = sorted(
            KeyPosition.objects.filter(is_active=True).select_related("position"),
            key=lambda item: -item.priority_score,
        )
        wanted = int(len(positions) * PLAN_COVERAGE)
        plans: list[SuccessionPlan] = []

        for key_position in positions[:wanted]:
            plan = create_plan(
                key_position,
                actor=self.actor,
                horizon_months=self.random.choice([24, 36, 36, 48]),
            )
            started = self.today - dt.timedelta(days=self.random.randint(30, 700))
            SuccessionPlan.objects.filter(pk=plan.pk).update(
                start_date=started,
                review_date=shift_months(started, 12),
                risks=self.random.choice(RISK_TEMPLATES),
            )
            plan.start_date = started
            plan.review_date = shift_months(started, 12)
            plans.append(plan)

        return plans

    # -- Преемники --------------------------------------------------------------
    def _fill_slates(self, plans: list[SuccessionPlan]) -> int:
        """Наполняет списки преемников и вводит планы в действие."""
        total = 0

        for plan in plans:
            entries = list(
                ReserveEntry.objects.for_position(plan.key_position.position_id)
                .in_reserve()
                .with_related()
                .order_by("rank", "-readiness_index")[: plan.key_position.required_successors]
            )
            if not entries:
                continue

            for index, entry in enumerate(entries):
                try:
                    add_successor(
                        plan,
                        entry.employee,
                        entry=entry,
                        # Экстренная замена — только первый преемник и только при подтверждённой
                        # готовности: признак означает способность принять должность немедленно, и
                        # раздавать его всем значит обесценить показатель.
                        is_emergency=(index == 0 and entry.is_ready),
                        development_focus=self.random.choice(FOCUS_TEMPLATES),
                    )
                except SuccessionError:
                    continue
                total += 1

            self._set_plan_state(plan)

        return total

    def _set_plan_state(self, plan: SuccessionPlan) -> None:
        """Разводит планы по состояниям: действующий, черновик, закрытый."""
        roll = self.random.random()
        if roll < DRAFT_SHARE:
            return
        if roll < DRAFT_SHARE + CLOSED_SHARE:
            SuccessionPlan.objects.filter(pk=plan.pk).update(
                status=PlanStatus.CLOSED,
                closed_at=self.today - dt.timedelta(days=self.random.randint(10, 200)),
                notes="План закрыт после состоявшегося назначения.",
            )
            return
        SuccessionPlan.objects.filter(pk=plan.pk).update(status=PlanStatus.ACTIVE)

    # -- Назначения ---------------------------------------------------------------
    def _create_appointments(self) -> int:
        """Порождает историю назначений."""
        created = 0
        candidates = list(
            ReserveEntry.objects.filter(status=ReserveStatus.ACTIVE)
            .with_related()
            .order_by("-readiness_index")[:60]
        )
        self.random.shuffle(candidates)

        for entry in candidates:
            if created >= int(APPOINTMENTS * FROM_RESERVE_SHARE):
                break
            appointed_on = self.today - dt.timedelta(days=self.random.randint(20, 900))
            if appointed_on < entry.inclusion_date:
                continue

            months = (appointed_on.year - entry.inclusion_date.year) * 12 + (
                appointed_on.month - entry.inclusion_date.month
            )
            Appointment.objects.create(
                employee=entry.employee,
                position=entry.target_position,
                plan=SuccessionPlan.objects.filter(
                    key_position__position=entry.target_position
                ).first(),
                entry=entry,
                kind=AppointmentKind.FROM_RESERVE,
                appointment_date=appointed_on,
                order_number=f"{self.random.randint(100, 999)}-к",
                readiness_index_at_appointment=entry.readiness_index,
                months_in_reserve=max(months, 0),
                created_by=self.actor,
            )
            created += 1

        # Назначения вне резерва: внутренние перемещения и внешний наём.
        others = list(Employee.objects.active().exclude(position__isnull=True).with_org()[:200])
        self.random.shuffle(others)
        positions = list(KeyPosition.objects.select_related("position"))

        for employee in others:
            if created >= APPOINTMENTS or not positions:
                break
            key_position = self.random.choice(positions)
            if employee.position_id == key_position.position_id:
                continue

            Appointment.objects.create(
                employee=employee,
                position=key_position.position,
                kind=self.random.choices(
                    [AppointmentKind.INTERNAL, AppointmentKind.EXTERNAL, AppointmentKind.ACTING],
                    weights=[0.5, 0.35, 0.15],
                )[0],
                appointment_date=self.today - dt.timedelta(days=self.random.randint(20, 900)),
                order_number=f"{self.random.randint(100, 999)}-к",
                created_by=self.actor,
                comment="Должность закрыта вне процедуры кадрового резерва.",
            )
            created += 1

        self._appoint_through_service()
        return Appointment.objects.count()

    def _appoint_through_service(self) -> None:
        """Выполняет одно назначение полным сервисным путём."""
        entry = (
            ReserveEntry.objects.filter(status=ReserveStatus.ACTIVE)
            .with_related()
            .order_by("-readiness_index")
            .first()
        )
        if entry is None:
            return

        register_appointment(
            employee=entry.employee,
            position=entry.target_position,
            appointment_date=self.today - dt.timedelta(days=3),
            actor=self.actor,
            entry=entry,
            order_number="001-к",
            comment="Назначение из кадрового резерва по действующему плану преемственности.",
        )
