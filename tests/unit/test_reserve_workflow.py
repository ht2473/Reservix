"""Проверка правил кадрового резерва: пригодность, маршрут заявки, ранжирование."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from django.utils import timezone

from apps.reserve.models import (
    NominationKind,
    NominationStatus,
    ReserveEntry,
    ReserveStatus,
)
from apps.reserve.services import (
    MAX_ACTIVE_TARGETS,
    MIN_TENURE_MONTHS,
    ReserveError,
    change_status,
    check_eligibility,
    create_entry,
    decide_hr,
    decide_manager,
    pending_for_user,
    rerank_position,
    reserve_summary,
    submit_nomination,
    withdraw_nomination,
)
from core.constants import ReadinessLevel
from tests.factories import PositionFactory
from tests.factories.domain import (
    EmployeeFactory,
    ReserveEntryFactory,
    ReserveGroupFactory,
    ReserveNominationFactory,
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


def hired_months_ago(months: int) -> dt.date:
    """Дата приёма, дающая указанный стаж в месяцах."""
    return timezone.localdate() - dt.timedelta(days=31 * months)


# ---------------------------------------------------------------------------
# Пригодность кандидата
# ---------------------------------------------------------------------------
class TestEligibility:
    """Проверка пригодности к выдвижению."""

    def test_active_employee_is_eligible(self, department, position):
        """Работающий сотрудник с достаточным стажем препятствий не имеет."""
        employee = EmployeeFactory(department=department, hire_date=hired_months_ago(24))

        result = check_eligibility(employee, position, with_profile=False)

        assert result.is_eligible
        assert result.blockers == ()

    def test_short_tenure_blocks(self, department, position):
        """Стаж ниже порога — препятствие, а не предупреждение."""
        employee = EmployeeFactory(
            department=department, hire_date=hired_months_ago(MIN_TENURE_MONTHS - 3)
        )

        result = check_eligibility(employee, position, with_profile=False)

        assert not result.is_eligible
        assert {issue.code for issue in result.blockers} == {"tenure"}

    def test_excluded_employee_blocks(self, department, position):
        """Явное исключение из рассмотрения перекрывает любые показатели."""
        employee = EmployeeFactory(
            department=department,
            hire_date=hired_months_ago(60),
            is_excluded_from_reserve=True,
            exclusion_note="Дисциплинарное взыскание",
        )

        result = check_eligibility(employee, position, with_profile=False)

        assert "excluded" in {issue.code for issue in result.blockers}

    def test_existing_entry_on_same_position_blocks(self, department, position):
        """Повторное выдвижение на ту же должность не допускается."""
        employee = EmployeeFactory(department=department, hire_date=hired_months_ago(48))
        ReserveEntryFactory(employee=employee, target_position=position)

        result = check_eligibility(employee, position, with_profile=False)

        assert "already_in_reserve" in {issue.code for issue in result.blockers}

    def test_other_targets_only_warn(self, department, position, grade):
        """Резерв на другую должность — предупреждение, а не запрет."""
        employee = EmployeeFactory(department=department, hire_date=hired_months_ago(48))
        other = PositionFactory(name="Руководитель отдела продаж", grade=grade)
        ReserveEntryFactory(employee=employee, target_position=other)

        result = check_eligibility(employee, position, with_profile=False)

        assert result.is_eligible
        assert "other_targets" in {issue.code for issue in result.warnings}

    def test_target_limit_blocks(self, department, position, grade):
        """Больше трёх одновременных целей — подготовка ни к одной из них."""
        employee = EmployeeFactory(department=department, hire_date=hired_months_ago(60))
        for index in range(MAX_ACTIVE_TARGETS):
            ReserveEntryFactory(
                employee=employee,
                target_position=PositionFactory(name=f"Целевая роль {index}", grade=grade),
            )

        result = check_eligibility(employee, position, with_profile=False)

        assert "too_many_targets" in {issue.code for issue in result.blockers}

    def test_group_age_limit_blocks(self, department, position):
        """Правило программы подготовки проверяется наравне с общими."""
        employee = EmployeeFactory(
            department=department,
            hire_date=hired_months_ago(120),
            birth_date=timezone.localdate() - dt.timedelta(days=365 * 45),
        )
        youth = ReserveGroupFactory(code="youth-group", name="Молодёжный резерв", max_age=35)

        result = check_eligibility(employee, position, group=youth, with_profile=False)

        assert "group_age" in {issue.code for issue in result.blockers}


# ---------------------------------------------------------------------------
# Маршрут согласования
# ---------------------------------------------------------------------------
class TestNominationRoute:
    """Движение заявки по ступеням согласования."""

    def test_self_nomination_goes_to_manager(self, department, position, employee_user):
        """Самовыдвижение проходит обе ступени: согласия руководителя ещё нет."""
        employee = EmployeeFactory(department=department, hire_date=hired_months_ago(36))
        nomination = ReserveNominationFactory(
            employee=employee,
            target_position=position,
            kind=NominationKind.SELF,
            status=NominationStatus.DRAFT,
            submitted_at=None,
        )

        submit_nomination(nomination, actor=employee_user)

        assert nomination.status == NominationStatus.MANAGER_REVIEW
        assert nomination.submitted_at is not None

    def test_manager_nomination_skips_manager_stage(self, department, position, manager_user):
        """Заявка непосредственного руководителя минует его же согласование."""
        employee = EmployeeFactory(department=department, hire_date=hired_months_ago(36))
        nomination = ReserveNominationFactory(
            employee=employee,
            target_position=position,
            kind=NominationKind.MANAGER,
            status=NominationStatus.DRAFT,
            submitted_at=None,
        )

        submit_nomination(nomination, actor=manager_user)

        assert nomination.status == NominationStatus.HR_REVIEW
        # Решение зафиксировано явно: пустая ступень читалась бы как пропуск.
        assert nomination.manager_reviewer_id == manager_user.pk
        assert nomination.manager_decision_at is not None

    def test_ineligible_candidate_is_refused(self, department, position, manager_user):
        """Заявка на непригодного кандидата не подаётся вовсе."""
        employee = EmployeeFactory(department=department, hire_date=hired_months_ago(1))
        nomination = ReserveNominationFactory(
            employee=employee,
            target_position=position,
            status=NominationStatus.DRAFT,
            submitted_at=None,
        )

        with pytest.raises(ReserveError):
            submit_nomination(nomination, actor=manager_user)

    def test_manager_rejection_closes_route(self, department, position, manager_user):
        """Отказ руководителя завершает маршрут, не доводя заявку до HR."""
        nomination = ReserveNominationFactory(
            employee=EmployeeFactory(department=department, hire_date=hired_months_ago(24)),
            target_position=position,
            status=NominationStatus.MANAGER_REVIEW,
        )

        decide_manager(nomination, actor=manager_user, approve=False, comment="Некем заместить")

        assert nomination.status == NominationStatus.REJECTED
        assert nomination.rejection_reason == "Некем заместить"

    def test_hr_approval_creates_entry(self, department, position, hr_user):
        """Одобрение кадровой службы создаёт запись резерва."""
        employee = EmployeeFactory(department=department, hire_date=hired_months_ago(24))
        nomination = ReserveNominationFactory(
            employee=employee,
            target_position=position,
            status=NominationStatus.HR_REVIEW,
            justification="Замещал руководителя, справился с планом",
        )

        decide_hr(nomination, actor=hr_user, approve=True)

        assert nomination.status == NominationStatus.APPROVED
        assert nomination.entry is not None
        assert nomination.entry.employee_id == employee.pk
        assert nomination.entry.status == ReserveStatus.CANDIDATE
        # Обоснование переносится в запись: через год оно отвечает на вопрос,
        # почему человек в резерве.
        assert nomination.entry.justification == nomination.justification

    def test_hr_rejection_creates_no_entry(self, department, position, hr_user):
        """Отказ кадровой службы не оставляет записи резерва."""
        nomination = ReserveNominationFactory(
            employee=EmployeeFactory(department=department, hire_date=hired_months_ago(24)),
            target_position=position,
            status=NominationStatus.HR_REVIEW,
        )

        decide_hr(nomination, actor=hr_user, approve=False, comment="Оценка не завершена")

        assert nomination.status == NominationStatus.REJECTED
        assert nomination.entry is None
        assert not ReserveEntry.objects.filter(employee=nomination.employee).exists()

    def test_decision_out_of_order_is_refused(self, department, position, hr_user):
        """Кадровая служба не принимает решение до согласования руководителем."""
        nomination = ReserveNominationFactory(
            employee=EmployeeFactory(department=department, hire_date=hired_months_ago(24)),
            target_position=position,
            status=NominationStatus.MANAGER_REVIEW,
        )

        with pytest.raises(ReserveError):
            decide_hr(nomination, actor=hr_user, approve=True)

    def test_withdrawn_nomination_cannot_be_decided(self, department, position, hr_user):
        """Отозванная заявка выбывает из маршрута окончательно."""
        nomination = ReserveNominationFactory(
            employee=EmployeeFactory(department=department, hire_date=hired_months_ago(24)),
            target_position=position,
            status=NominationStatus.HR_REVIEW,
        )
        withdraw_nomination(nomination, actor=hr_user)

        with pytest.raises(ReserveError):
            decide_hr(nomination, actor=hr_user, approve=True)

    def test_pending_list_is_split_by_stage(
        self, department, position, grade, hr_user, manager_user
    ):
        """Каждой роли адресована своя ступень маршрута."""
        employee = EmployeeFactory(
            department=department, hire_date=hired_months_ago(24), manager=None
        )
        ReserveNominationFactory(
            employee=employee,
            target_position=position,
            status=NominationStatus.MANAGER_REVIEW,
        )
        ReserveNominationFactory(
            employee=EmployeeFactory(department=department, hire_date=hired_months_ago(24)),
            target_position=PositionFactory(name="Иная роль", grade=grade),
            status=NominationStatus.HR_REVIEW,
        )

        assert pending_for_user(hr_user).count() == 1
        assert pending_for_user(manager_user).count() == 1


# ---------------------------------------------------------------------------
# Состояния записи
# ---------------------------------------------------------------------------
class TestEntryLifecycle:
    """Движение записи резерва по состояниям."""

    def test_excluded_entry_cannot_return(self, department, position, hr_user):
        """Исключённый резервист не восстанавливается в обход процедуры."""
        entry = ReserveEntryFactory(
            employee=EmployeeFactory(department=department), target_position=position
        )
        change_status(entry, ReserveStatus.EXCLUDED, actor=hr_user, reason="Отказ кандидата")

        with pytest.raises(ReserveError):
            change_status(entry, ReserveStatus.ACTIVE, actor=hr_user, reason="Передумали")

    def test_status_change_is_recorded(self, department, position, hr_user):
        """Каждый переход сохраняется в истории вместе с основанием."""
        entry = ReserveEntryFactory(
            employee=EmployeeFactory(department=department), target_position=position
        )

        change_status(
            entry, ReserveStatus.INACTIVE, actor=hr_user, reason="Отпуск по уходу за ребёнком"
        )

        record = entry.status_history.first()
        assert record is not None
        assert record.old_status == ReserveStatus.ACTIVE
        assert record.new_status == ReserveStatus.INACTIVE
        assert record.reason == "Отпуск по уходу за ребёнком"
        assert record.changed_by_id == hr_user.pk

    def test_closing_status_releases_rank(self, department, position, hr_user):
        """Закрытая запись освобождает место в списке преемников."""
        entry = ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=position,
            rank=1,
        )

        change_status(entry, ReserveStatus.EXCLUDED, actor=hr_user, reason="Увольнение")

        entry.refresh_from_db()
        assert entry.rank is None
        assert entry.exclusion_date is not None

    def test_membership_closes_with_entry(self, department, position, hr_user):
        """Участие в программе подготовки завершается вместе с записью."""
        group = ReserveGroupFactory(code="closing-group")
        entry = create_entry(
            employee=EmployeeFactory(department=department, hire_date=hired_months_ago(36)),
            position=position,
            group=group,
            actor=hr_user,
            status=ReserveStatus.CANDIDATE,
        )
        assert entry.memberships.filter(left_at__isnull=True).count() == 1

        change_status(entry, ReserveStatus.EXCLUDED, actor=hr_user, reason="Отказ кандидата")

        assert entry.memberships.filter(left_at__isnull=True).count() == 0


# ---------------------------------------------------------------------------
# Ранжирование
# ---------------------------------------------------------------------------
class TestRanking:
    """Порядок кандидатов на одну должность."""

    def test_rank_follows_index(self, department, position):
        """Ранг расставляется по индексу готовности, старший — первый."""
        weak = ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=position,
            readiness_index=Decimal("48.00"),
        )
        strong = ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=position,
            readiness_index=Decimal("81.00"),
        )

        rerank_position(position.pk)

        strong.refresh_from_db()
        weak.refresh_from_db()
        assert strong.rank == 1
        assert weak.rank == 2

    def test_tie_resolved_by_time_in_reserve(self, department, position):
        """Равенство индексов разрешается сроком пребывания в резерве."""
        today = timezone.localdate()
        newer = ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=position,
            readiness_index=Decimal("70.00"),
            inclusion_date=today - dt.timedelta(days=30),
        )
        older = ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=position,
            readiness_index=Decimal("70.00"),
            inclusion_date=today - dt.timedelta(days=900),
        )

        rerank_position(position.pk)

        older.refresh_from_db()
        newer.refresh_from_db()
        assert older.rank == 1
        assert newer.rank == 2

    def test_unrated_entry_goes_last(self, department, position):
        """Запись без индекса не встаёт впереди оценённой."""
        rated = ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=position,
            readiness_index=Decimal("30.00"),
        )
        unrated = ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=position,
            readiness_index=None,
        )

        rerank_position(position.pk)

        rated.refresh_from_db()
        unrated.refresh_from_db()
        assert rated.rank == 1
        assert unrated.rank == 2

    def test_closed_entries_are_not_ranked(self, department, position, hr_user):
        """Назначенные и исключённые записи в списке преемников не участвуют."""
        active = ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=position,
            readiness_index=Decimal("50.00"),
        )
        closed = ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=position,
            readiness_index=Decimal("90.00"),
            status=ReserveStatus.EXCLUDED,
        )

        rerank_position(position.pk)

        active.refresh_from_db()
        closed.refresh_from_db()
        assert active.rank == 1
        assert closed.rank is None


# ---------------------------------------------------------------------------
# Сводка
# ---------------------------------------------------------------------------
class TestSummary:
    """Показатели состояния резерва."""

    def test_summary_counts_by_state(self, department, position, grade):
        """Сводка различает состав резерва, готовность и просрочку пересмотра."""
        ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=position,
            readiness_level=ReadinessLevel.HIGH,
        )
        ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=PositionFactory(name="Вторая роль", grade=grade),
            readiness_level=ReadinessLevel.LOW,
            review_date=timezone.localdate() - dt.timedelta(days=10),
        )

        summary = reserve_summary()

        assert summary.active == 2
        assert summary.ready_now == 1
        assert summary.ready_share == 50.0
        assert summary.positions_covered == 2
        assert summary.overdue_reviews == 1

    def test_empty_summary_does_not_divide_by_zero(self):
        """Пустой резерв даёт нули, а не отказ расчёта."""
        summary = reserve_summary()

        assert summary.total == 0
        assert summary.ready_share == 0.0
