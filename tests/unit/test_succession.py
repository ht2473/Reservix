"""Проверка правил преемственности: обеспеченность, список преемников, назначения."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from django.utils import timezone

from apps.reserve.models import ReserveStatus
from apps.succession.models import (
    Appointment,
    AppointmentKind,
    CriticalityLevel,
    PlanStatus,
    SuccessionSlate,
    VacancyRisk,
)
from apps.succession.services import (
    SuccessionError,
    activate_plan,
    add_successor,
    appointment_statistics,
    close_plan,
    compact_ranks,
    coverage_report,
    create_plan,
    key_positions,
    register_appointment,
    remove_successor,
    reorder_slate,
    risk_matrix,
    suggest_successors,
    sync_key_positions,
    sync_slate_readiness,
    uncovered_positions,
)
from core.constants import ReadinessLevel
from tests.factories import PositionFactory
from tests.factories.domain import (
    EmployeeFactory,
    KeyPositionFactory,
    ReserveEntryFactory,
    SuccessionPlanFactory,
    SuccessionSlateFactory,
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


# ---------------------------------------------------------------------------
# Критичные должности
# ---------------------------------------------------------------------------
class TestKeyPositions:
    """Перечень критичных должностей и его синхронизация."""

    def test_sync_creates_missing(self, grade, department):
        """Сверка добавляет должности, отмеченные критичными в справочнике."""
        PositionFactory(name="Главный инженер", grade=grade, is_key_position=True)
        PositionFactory(name="Кладовщик", grade=grade, is_key_position=False)

        result = sync_key_positions()

        assert result["created"] == 1
        assert key_positions().count() == 1

    def test_sync_retires_without_deleting(self, grade):
        """Снятие признака критичности выводит запись из планирования."""
        position = PositionFactory(name="Главный инженер", grade=grade, is_key_position=True)
        key_position = KeyPositionFactory(position=position)
        SuccessionPlanFactory(key_position=key_position)

        position.is_key_position = False
        position.save(update_fields=["is_key_position"])
        result = sync_key_positions()

        key_position.refresh_from_db()
        assert result["retired"] == 1
        assert key_position.is_active is False
        assert key_position.plans.count() == 1

    def test_sync_is_idempotent(self, grade):
        """Повторная сверка не создаёт дубликатов."""
        PositionFactory(name="Главный инженер", grade=grade, is_key_position=True)

        sync_key_positions()
        second = sync_key_positions()

        assert second["created"] == 0
        assert key_positions().count() == 1

    def test_priority_is_product(self, position):
        """Приоритет планирования — произведение критичности на риск."""
        key_position = KeyPositionFactory(
            position=position,
            criticality=CriticalityLevel.SIGNIFICANT,
            vacancy_risk=VacancyRisk.HIGH,
        )

        assert key_position.priority_score == 9

    def test_cancelled_plan_leaves_position_uncovered(self, position, hr_user):
        """Должность с отменённым планом остаётся в списке незакрытых."""
        key_position = KeyPositionFactory(position=position)
        plan = SuccessionPlanFactory(key_position=key_position)
        SuccessionSlateFactory(plan=plan, employee=EmployeeFactory())
        close_plan(plan, actor=hr_user, reason="Кандидат уволился", cancel=True)

        assert uncovered_positions().filter(pk=key_position.pk).exists()

    def test_coverage_counts_only_active_plans(self, grade, department, hr_user):
        """Обеспеченность считается по действующим планам."""
        covered = KeyPositionFactory(
            position=PositionFactory(name="Роль А", grade=grade, is_key_position=True)
        )
        stale = KeyPositionFactory(
            position=PositionFactory(name="Роль Б", grade=grade, is_key_position=True)
        )
        active_plan = SuccessionPlanFactory(key_position=covered)
        SuccessionSlateFactory(
            plan=active_plan,
            employee=EmployeeFactory(department=department),
            readiness_level=ReadinessLevel.HIGH,
        )
        closed_plan = SuccessionPlanFactory(key_position=stale)
        SuccessionSlateFactory(plan=closed_plan, employee=EmployeeFactory(department=department))
        close_plan(closed_plan, actor=hr_user, reason="Плановое закрытие")

        report = coverage_report()

        assert report.total == 2
        assert report.covered == 1
        assert report.coverage == 50.0
        assert report.with_ready == 1

    def test_risk_matrix_has_all_cells(self, position):
        """Матрица покрывает все сочетания критичности и риска."""
        KeyPositionFactory(position=position)

        matrix = risk_matrix()

        assert len(matrix) == len(VacancyRisk.values)
        assert all(len(row) == len(CriticalityLevel.values) for row in matrix)
        # Верхняя строка — самый высокий риск: таблицу читают сверху.
        assert matrix[0][0].risk == max(VacancyRisk.values)


# ---------------------------------------------------------------------------
# Планы и преемники
# ---------------------------------------------------------------------------
class TestPlans:
    """Ведение плана преемственности."""

    def test_empty_plan_cannot_be_activated(self, position, hr_user):
        """План без преемников заявлял бы обеспеченность, которой нет."""
        plan = create_plan(KeyPositionFactory(position=position), actor=hr_user)

        with pytest.raises(SuccessionError):
            activate_plan(plan, actor=hr_user)

    def test_plan_with_successors_activates(self, position, hr_user, department):
        """План с преемником вводится в действие."""
        plan = create_plan(KeyPositionFactory(position=position), actor=hr_user)
        add_successor(plan, EmployeeFactory(department=department))

        activate_plan(plan, actor=hr_user)

        assert plan.status == PlanStatus.ACTIVE

    def test_second_active_plan_is_refused(self, position, hr_user, department):
        """Два действующих плана — два несогласованных списка преемников."""
        key_position = KeyPositionFactory(position=position)
        SuccessionPlanFactory(key_position=key_position, status=PlanStatus.ACTIVE)

        with pytest.raises(SuccessionError):
            create_plan(key_position, actor=hr_user, activate=True)

    def test_successor_readiness_comes_from_reserve(self, position, department):
        """Готовность преемника переносится из записи резерва."""
        employee = EmployeeFactory(department=department)
        ReserveEntryFactory(
            employee=employee,
            target_position=position,
            readiness_level=ReadinessLevel.HIGH,
            readiness_index=Decimal("83.00"),
        )
        plan = SuccessionPlanFactory(key_position=KeyPositionFactory(position=position))

        slate = add_successor(plan, employee)

        assert slate.readiness_level == ReadinessLevel.HIGH
        assert slate.readiness_index == Decimal("83.00")
        assert slate.entry is not None

    def test_external_successor_gets_lowest_readiness(self, position, department):
        """Кандидат вне резерва получает низший уровень, а не пустое значение."""
        plan = SuccessionPlanFactory(key_position=KeyPositionFactory(position=position))

        slate = add_successor(plan, EmployeeFactory(department=department))

        assert slate.readiness_level == ReadinessLevel.LOW
        assert slate.readiness_index is None

    def test_duplicate_successor_is_refused(self, position, department):
        """Дважды включить одного человека в план нельзя."""
        plan = SuccessionPlanFactory(key_position=KeyPositionFactory(position=position))
        employee = EmployeeFactory(department=department)
        add_successor(plan, employee)

        with pytest.raises(SuccessionError):
            add_successor(plan, employee)

    def test_removed_successor_can_return(self, position, department, hr_user):
        """Исключённого преемника можно вернуть в план."""
        plan = SuccessionPlanFactory(key_position=KeyPositionFactory(position=position))
        employee = EmployeeFactory(department=department)
        slate = add_successor(plan, employee)
        remove_successor(slate, reason="Отказ кандидата", actor=hr_user)

        restored = add_successor(plan, employee)

        assert restored.pk == slate.pk
        assert restored.removed_on is None
        assert SuccessionSlate.objects.filter(plan=plan, employee=employee).count() == 1

    def test_reorder_keeps_ranks_unique(self, position, department):
        """Перестановка не нарушает уникальность рангов."""
        plan = SuccessionPlanFactory(key_position=KeyPositionFactory(position=position))
        first = add_successor(plan, EmployeeFactory(department=department))
        second = add_successor(plan, EmployeeFactory(department=department))
        third = add_successor(plan, EmployeeFactory(department=department))

        reorder_slate(plan, [third.pk, first.pk, second.pk])

        ranks = dict(plan.slate.values_list("pk", "rank"))
        assert ranks[third.pk] == 1
        assert ranks[first.pk] == 2
        assert ranks[second.pk] == 3

    def test_reorder_rejects_incomplete_order(self, position, department):
        """Перечень, не совпадающий с составом плана, отвергается."""
        plan = SuccessionPlanFactory(key_position=KeyPositionFactory(position=position))
        first = add_successor(plan, EmployeeFactory(department=department))
        add_successor(plan, EmployeeFactory(department=department))

        with pytest.raises(SuccessionError):
            reorder_slate(plan, [first.pk])

    def test_removal_compacts_ranks(self, position, department, hr_user):
        """Нумерация уплотняется после исключения преемника."""
        plan = SuccessionPlanFactory(key_position=KeyPositionFactory(position=position))
        first = add_successor(plan, EmployeeFactory(department=department))
        second = add_successor(plan, EmployeeFactory(department=department))
        third = add_successor(plan, EmployeeFactory(department=department))

        remove_successor(second, reason="Перевод в другой филиал", actor=hr_user)

        first.refresh_from_db()
        third.refresh_from_db()
        assert [first.rank, third.rank] == [1, 2]
        assert compact_ranks(plan) == 2

    def test_sync_readiness_updates_stale_snapshot(self, position, department):
        """Готовность в плане обновляется по текущему состоянию резерва."""
        employee = EmployeeFactory(department=department)
        entry = ReserveEntryFactory(
            employee=employee,
            target_position=position,
            readiness_level=ReadinessLevel.LOW,
            readiness_index=Decimal("40.00"),
        )
        plan = SuccessionPlanFactory(key_position=KeyPositionFactory(position=position))
        slate = add_successor(plan, employee, entry=entry)

        entry.readiness_level = ReadinessLevel.HIGH
        entry.readiness_index = Decimal("88.00")
        entry.save(update_fields=["readiness_level", "readiness_index"])
        updated = sync_slate_readiness(plan)

        slate.refresh_from_db()
        assert updated == 1
        assert slate.readiness_level == ReadinessLevel.HIGH

    def test_suggestions_prefer_reserve(self, position, department):
        """Резервисты на эту должность предлагаются раньше прочих кандидатов."""
        employee = EmployeeFactory(department=department)
        ReserveEntryFactory(
            employee=employee,
            target_position=position,
            readiness_index=Decimal("77.00"),
        )
        key_position = KeyPositionFactory(position=position)

        suggestions = suggest_successors(key_position)

        assert suggestions
        assert suggestions[0]["employee"].pk == employee.pk
        assert suggestions[0]["from_reserve"] is True


# ---------------------------------------------------------------------------
# Назначения
# ---------------------------------------------------------------------------
class TestAppointments:
    """Регистрация назначения — операция, замыкающая цикл."""

    def test_appointment_closes_reserve_entry(self, position, department, hr_user):
        """Назначенный резервист покидает скамейку."""
        employee = EmployeeFactory(department=department)
        entry = ReserveEntryFactory(
            employee=employee,
            target_position=position,
            readiness_index=Decimal("79.00"),
            inclusion_date=timezone.localdate() - dt.timedelta(days=400),
        )

        appointment = register_appointment(
            employee=employee,
            position=position,
            appointment_date=timezone.localdate(),
            actor=hr_user,
        )

        entry.refresh_from_db()
        assert entry.status == ReserveStatus.APPOINTED
        assert appointment.entry_id == entry.pk
        # Индекс фиксируется, чтобы позже проверить прогностическую силу оценки.
        assert appointment.readiness_index_at_appointment == Decimal("79.00")
        assert appointment.months_in_reserve == 13

    def test_appointment_removes_from_slate(self, position, department, hr_user):
        """Назначенный больше не преемник: он занял должность."""
        employee = EmployeeFactory(department=department)
        key_position = KeyPositionFactory(position=position)
        plan = SuccessionPlanFactory(key_position=key_position)
        slate = add_successor(plan, employee)

        register_appointment(
            employee=employee,
            position=position,
            appointment_date=timezone.localdate(),
            actor=hr_user,
        )

        slate.refresh_from_db()
        key_position.refresh_from_db()
        assert slate.removed_on is not None
        assert key_position.incumbent_id == employee.pk

    def test_appointment_moves_employee(self, position, department, hr_user):
        """Карточка сотрудника переезжает на новую должность."""
        employee = EmployeeFactory(department=department)
        today = timezone.localdate()

        register_appointment(
            employee=employee,
            position=position,
            appointment_date=today,
            actor=hr_user,
        )

        employee.refresh_from_db()
        assert employee.position_id == position.pk
        assert employee.position_since == today

    def test_external_hire_does_not_touch_reserve(self, position, department, hr_user):
        """Внешний наём регистрируется, не затрагивая записи резерва."""
        employee = EmployeeFactory(department=department)
        other = EmployeeFactory(department=department)
        entry = ReserveEntryFactory(employee=other, target_position=position)

        register_appointment(
            employee=employee,
            position=position,
            appointment_date=timezone.localdate(),
            kind=AppointmentKind.EXTERNAL,
            actor=hr_user,
        )

        entry.refresh_from_db()
        assert entry.status == ReserveStatus.ACTIVE

    def test_statistics_measure_reserve_share(self, position, grade, department, hr_user):
        """Доля назначений из резерва — главный показатель системы."""
        today = timezone.localdate()
        Appointment.objects.create(
            employee=EmployeeFactory(department=department),
            position=position,
            kind=AppointmentKind.FROM_RESERVE,
            appointment_date=today,
            months_in_reserve=18,
        )
        Appointment.objects.create(
            employee=EmployeeFactory(department=department),
            position=PositionFactory(name="Иная роль", grade=grade),
            kind=AppointmentKind.EXTERNAL,
            appointment_date=today,
        )

        stats = appointment_statistics(year=today.year)

        assert stats.total == 2
        assert stats.from_reserve == 1
        assert stats.reserve_share == 50.0
        assert stats.average_months == 18.0

    def test_statistics_on_empty_history(self):
        """Пустая история даёт нули, а не отказ расчёта."""
        stats = appointment_statistics()

        assert stats.total == 0
        assert stats.reserve_share == 0.0
        assert stats.average_years is None
