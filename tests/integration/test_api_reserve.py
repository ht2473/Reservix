"""Проверка методов резерва и преемственности, добавленных на этапе резерва."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from django.utils import timezone

from apps.reserve.models import NominationStatus, ReserveStatus
from apps.succession.models import Appointment, AppointmentKind, PlanStatus
from core.constants import ReadinessLevel, Role, ScopeLevel
from tests.factories import PositionFactory, UserFactory
from tests.factories.domain import (
    EmployeeFactory,
    KeyPositionFactory,
    ReserveEntryFactory,
    ReserveGroupFactory,
    ReserveNominationFactory,
    SuccessionPlanFactory,
    SuccessionSlateFactory,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

API = "/api/v1"


# ---------------------------------------------------------------------------
# Заявки
# ---------------------------------------------------------------------------
class TestNominationDecision:
    """Решение по заявке через программный интерфейс."""

    @pytest.fixture
    def nomination(self, department, position, manager_user):
        return ReserveNominationFactory(
            employee=EmployeeFactory(department=department, hire_date=dt.date(2019, 4, 1)),
            target_position=position,
            nominated_by=manager_user,
            status=NominationStatus.MANAGER_REVIEW,
        )

    def test_manager_stage_moves_to_hr(self, manager_client, nomination):
        """Согласие руководителя переводит заявку на ступень кадровой службы."""
        response = manager_client.post(
            f"{API}/reserve/nominations/{nomination.public_id}/decision",
            data={"approve": True, "comment": "Согласовано"},
            content_type="application/json",
        )

        nomination.refresh_from_db()
        assert response.status_code == 200
        assert nomination.status == NominationStatus.HR_REVIEW

    def test_hr_stage_creates_entry(self, hr_client, nomination):
        """Одобрение кадровой службой создаёт запись резерва в той же операции."""
        nomination.status = NominationStatus.HR_REVIEW
        nomination.save(update_fields=["status"])
        group = ReserveGroupFactory(code="api-group")

        response = hr_client.post(
            f"{API}/reserve/nominations/{nomination.public_id}/decision",
            data={"approve": True, "comment": "Зачислен", "reserve_group_id": group.pk},
            content_type="application/json",
        )

        nomination.refresh_from_db()
        assert response.status_code == 200
        assert nomination.status == NominationStatus.APPROVED
        assert nomination.entry is not None
        assert nomination.entry.reserve_group_id == group.pk

    def test_closed_nomination_is_refused(self, hr_client, nomination):
        """По решённой заявке решение повторно не принимается."""
        nomination.status = NominationStatus.REJECTED
        nomination.save(update_fields=["status"])

        response = hr_client.post(
            f"{API}/reserve/nominations/{nomination.public_id}/decision",
            data={"approve": True},
            content_type="application/json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "wrong_stage"

    def test_outsider_is_refused(self, client, nomination, other_department):
        """Руководитель чужого подразделения решения не принимает."""
        outsider = UserFactory(
            role=Role.MANAGER,
            scope_level=ScopeLevel.SUBORDINATES,
            department=other_department,
            is_department_head=True,
        )
        client.force_login(outsider)

        response = client.post(
            f"{API}/reserve/nominations/{nomination.public_id}/decision",
            data={"approve": True, "comment": "Согласовано"},
            content_type="application/json",
        )

        nomination.refresh_from_db()
        assert response.status_code == 403
        assert nomination.status == NominationStatus.MANAGER_REVIEW

    def test_missing_nomination_returns_404(self, hr_client):
        """Несуществующая заявка даёт отказ, а не ошибку сервера."""
        response = hr_client.post(
            f"{API}/reserve/nominations/00000000-0000-0000-0000-000000000000/decision",
            data={"approve": True},
            content_type="application/json",
        )

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Преемственность
# ---------------------------------------------------------------------------
class TestSuccessionEndpoints:
    """Показатели преемственности."""

    def test_coverage_reports_three_measures(self, hr_client, position, department, grade):
        """Покрытие, глубина скамейки и немедленная готовность считаются раздельно."""
        covered = KeyPositionFactory(position=position)
        KeyPositionFactory(
            position=PositionFactory(name="Незакрытая роль", grade=grade, is_key_position=True)
        )
        plan = SuccessionPlanFactory(key_position=covered, status=PlanStatus.ACTIVE)
        SuccessionSlateFactory(
            plan=plan,
            employee=EmployeeFactory(department=department),
            readiness_level=ReadinessLevel.HIGH,
        )

        response = hr_client.get(f"{API}/succession/coverage")
        body = response.json()

        assert response.status_code == 200
        assert body["total"] == 2
        assert body["covered"] == 1
        assert body["coverage"] == 50.0
        assert body["ready_coverage"] == 50.0
        assert body["bench_depth"] == 0.5

    def test_uncovered_filter_ignores_cancelled_plan(
        self, hr_client, position, department, hr_user
    ):
        """Должность с отменённым планом остаётся незакрытой."""
        key_position = KeyPositionFactory(position=position)
        plan = SuccessionPlanFactory(key_position=key_position, status=PlanStatus.CANCELLED)
        SuccessionSlateFactory(plan=plan, employee=EmployeeFactory(department=department))

        response = hr_client.get(f"{API}/succession/key-positions", {"uncovered": "true"})
        body = response.json()

        assert response.status_code == 200
        assert body["count"] == 1

    def test_statistics_measure_reserve_share(self, hr_client, position, department, grade):
        """Доля назначений из резерва считается за указанный год."""
        today = timezone.localdate()
        Appointment.objects.create(
            employee=EmployeeFactory(department=department),
            position=position,
            kind=AppointmentKind.FROM_RESERVE,
            appointment_date=today,
            months_in_reserve=24,
        )
        Appointment.objects.create(
            employee=EmployeeFactory(department=department),
            position=PositionFactory(name="Иная роль", grade=grade),
            kind=AppointmentKind.EXTERNAL,
            appointment_date=today,
        )

        response = hr_client.get(f"{API}/succession/statistics", {"year": today.year})
        body = response.json()

        assert response.status_code == 200
        assert body["total"] == 2
        assert body["reserve_share"] == 50.0
        assert body["average_months"] == 24.0

    def test_employee_denied(self, auth_client):
        """Показатели преемственности рядовому сотруднику недоступны."""
        assert auth_client.get(f"{API}/succession/coverage").status_code == 403
        assert auth_client.get(f"{API}/succession/statistics").status_code == 403


# ---------------------------------------------------------------------------
# Записи резерва
# ---------------------------------------------------------------------------
class TestEntryTransition:
    """Смена состояния записи через программный интерфейс."""

    def test_invalid_transition_is_refused(self, hr_client, department, position):
        """Исключённая запись не восстанавливается и через интерфейс."""
        entry = ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=position,
            status=ReserveStatus.EXCLUDED,
            readiness_index=Decimal("60.00"),
        )

        response = hr_client.post(
            f"{API}/reserve/entries/{entry.public_id}/transition",
            data={"status": ReserveStatus.ACTIVE, "reason": "Вернуть в резерв"},
            content_type="application/json",
        )

        entry.refresh_from_db()
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_transition"
        assert entry.status == ReserveStatus.EXCLUDED

    def test_manager_cannot_transition(self, manager_client, department, position):
        """Состав резерва ведёт кадровая служба."""
        entry = ReserveEntryFactory(
            employee=EmployeeFactory(department=department), target_position=position
        )

        response = manager_client.post(
            f"{API}/reserve/entries/{entry.public_id}/transition",
            data={"status": ReserveStatus.INACTIVE, "reason": "Приостановить"},
            content_type="application/json",
        )

        entry.refresh_from_db()
        assert response.status_code == 403
        assert entry.status == ReserveStatus.ACTIVE
