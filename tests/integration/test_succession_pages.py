"""Проверка страниц преемственности и сквозного пути до назначения."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from django.urls import reverse
from django.utils import timezone

from apps.reserve.models import ReserveStatus
from apps.succession.models import (
    Appointment,
    AppointmentKind,
    PlanStatus,
    SuccessionPlan,
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

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def key_position(position):
    """Критичная должность с действующим сотрудником."""
    return KeyPositionFactory(position=position, required_successors=2)


@pytest.fixture
def plan(key_position, department):
    """Действующий план с одним преемником."""
    plan = SuccessionPlanFactory(key_position=key_position, status=PlanStatus.ACTIVE)
    SuccessionSlateFactory(
        plan=plan,
        employee=EmployeeFactory(department=department),
        rank=1,
        readiness_level=ReadinessLevel.HIGH,
    )
    return plan


# ---------------------------------------------------------------------------
# Доступность страниц
# ---------------------------------------------------------------------------
class TestPageAccess:
    """Разграничение доступа к разделу."""

    @pytest.mark.parametrize(
        "url_name",
        [
            "succession:list",
            "succession:coverage",
            "succession:key_positions",
            "succession:plans",
            "succession:appointments",
        ],
    )
    def test_hr_opens_all_pages(self, hr_client, url_name, plan):
        """Кадровая служба ведёт планирование целиком."""
        assert hr_client.get(reverse(url_name)).status_code == 200

    def test_manager_reads_but_does_not_plan(self, manager_client, key_position):
        """Руководитель видит перечень, но не заводит критичные должности."""
        assert manager_client.get(reverse("succession:key_positions")).status_code == 200
        assert manager_client.get(reverse("succession:key_position_create")).status_code == 403

    def test_employee_denied(self, auth_client):
        """Рядовому сотруднику раздел недоступен целиком."""
        assert auth_client.get(reverse("succession:list")).status_code == 403
        assert auth_client.get(reverse("succession:plans")).status_code == 403

    def test_anonymous_is_redirected(self, client):
        """Раздел закрыт от неавторизованного посетителя."""
        response = client.get(reverse("succession:list"))

        assert response.status_code == 302
        assert "/account/login" in response["Location"]

    def test_detail_pages_open(self, hr_client, key_position, plan):
        """Карточки должности и плана отображаются."""
        assert hr_client.get(key_position.get_absolute_url()).status_code == 200
        assert hr_client.get(plan.get_absolute_url()).status_code == 200


# ---------------------------------------------------------------------------
# Показатели
# ---------------------------------------------------------------------------
class TestCoverage:
    """Обеспеченность и её отображение."""

    def test_overview_reports_coverage(self, hr_client, plan, grade):
        """Обзор различает покрытые и непокрытые должности."""
        KeyPositionFactory(
            position=PositionFactory(name="Незакрытая роль", grade=grade, is_key_position=True)
        )

        response = hr_client.get(reverse("succession:list"))
        report = response.context["report"]

        assert response.status_code == 200
        assert report.total == 2
        assert report.covered == 1
        assert report.coverage == 50.0

    def test_list_counts_successors_by_query(self, hr_client, plan, key_position):
        """Обеспеченность считается агрегатами, а не свойствами модели."""
        response = hr_client.get(reverse("succession:key_positions"))
        row = response.context["positions"][0]

        assert response.status_code == 200
        assert row.successors_total == 1
        assert row.successors_ready == 1

    def test_uncovered_filter_uses_annotation(self, hr_client, plan, grade):
        """Отбор «без действующего плана» опирается на аннотацию запроса."""
        KeyPositionFactory(
            position=PositionFactory(name="Пустая роль", grade=grade, is_key_position=True)
        )

        response = hr_client.get(reverse("succession:key_positions"), {"coverage": "uncovered"})

        assert response.status_code == 200
        assert len(response.context["positions"]) == 1

    def test_sync_requires_post(self, hr_client, grade):
        """Сверка со штатным расписанием по переходу по ссылке не выполняется."""
        PositionFactory(name="Новая критичная роль", grade=grade, is_key_position=True)

        response = hr_client.get(reverse("succession:key_positions_sync"))

        assert response.status_code == 302
        assert not SuccessionPlan.objects.exists()


# ---------------------------------------------------------------------------
# Ведение плана
# ---------------------------------------------------------------------------
class TestPlanFlow:
    """Полный путь: план — преемники — ввод в действие."""

    def test_plan_created_from_key_position(self, hr_client, key_position):
        """План создаётся из карточки критичной должности."""
        response = hr_client.post(reverse("succession:plan_create", args=[key_position.pk]))

        assert response.status_code == 302
        assert key_position.plans.count() == 1
        assert key_position.plans.get().status == PlanStatus.DRAFT

    def test_successor_added_and_plan_activated(self, hr_client, key_position, department):
        """Черновик с преемником вводится в действие."""
        plan = SuccessionPlanFactory(key_position=key_position, status=PlanStatus.DRAFT)
        employee = EmployeeFactory(department=department)

        hr_client.post(
            reverse("succession:slate_add", args=[plan.public_id]),
            {"employee": employee.pk, "development_focus": "Управление бюджетом"},
        )
        response = hr_client.post(
            reverse("succession:plan_action", args=[plan.public_id, "activate"])
        )

        plan.refresh_from_db()
        assert response.status_code == 302
        assert plan.status == PlanStatus.ACTIVE
        assert plan.slate.count() == 1

    def test_empty_plan_activation_is_refused(self, hr_client, key_position):
        """План без преемников в действие не вводится."""
        plan = SuccessionPlanFactory(key_position=key_position, status=PlanStatus.DRAFT)

        response = hr_client.post(
            reverse("succession:plan_action", args=[plan.public_id, "activate"]), follow=True
        )

        plan.refresh_from_db()
        assert response.status_code == 200
        assert plan.status == PlanStatus.DRAFT

    def test_successor_removal_requires_reason(self, hr_client, plan):
        """Исключение преемника без основания не выполняется."""
        slate = plan.slate.get()

        response = hr_client.post(
            reverse("succession:slate_remove", args=[slate.pk]), {"reason": ""}
        )

        slate.refresh_from_db()
        assert response.status_code == 200
        assert slate.removed_on is None

    def test_successor_removed_with_reason(self, hr_client, plan):
        """С основанием преемник исключается, строка остаётся в истории."""
        slate = plan.slate.get()

        response = hr_client.post(
            reverse("succession:slate_remove", args=[slate.pk]),
            {"reason": "Отказ кандидата от подготовки"},
        )

        slate.refresh_from_db()
        assert response.status_code == 302
        assert slate.removed_on is not None
        assert plan.slate.count() == 1

    def test_manager_cannot_edit_plan(self, manager_client, plan, department):
        """Планирование ведёт кадровая служба."""
        response = manager_client.post(
            reverse("succession:slate_add", args=[plan.public_id]),
            {"employee": EmployeeFactory(department=department).pk},
        )

        assert response.status_code == 403
        assert plan.slate.count() == 1

    def test_reorder_changes_ranks(self, hr_client, plan, department):
        """Порядок преемников сохраняется одной операцией."""
        first = plan.slate.get()
        second = SuccessionSlateFactory(
            plan=plan, employee=EmployeeFactory(department=department), rank=2
        )

        response = hr_client.post(
            reverse("succession:slate_order", args=[plan.public_id]),
            {"order": f"{second.pk},{first.pk}"},
        )

        first.refresh_from_db()
        second.refresh_from_db()
        assert response.status_code == 302
        assert second.rank == 1
        assert first.rank == 2


# ---------------------------------------------------------------------------
# Назначение
# ---------------------------------------------------------------------------
class TestAppointmentFlow:
    """Регистрация назначения замыкает цикл системы."""

    def test_appointment_closes_cycle(self, hr_client, key_position, department, position):
        """Одно действие меняет пять связанных записей."""
        employee = EmployeeFactory(department=department)
        entry = ReserveEntryFactory(
            employee=employee,
            target_position=position,
            readiness_index=Decimal("82.00"),
            inclusion_date=timezone.localdate() - dt.timedelta(days=400),
        )
        plan = SuccessionPlanFactory(key_position=key_position, status=PlanStatus.ACTIVE)
        slate = SuccessionSlateFactory(plan=plan, employee=employee, rank=1)

        response = hr_client.post(
            reverse("succession:appointment_create"),
            {
                "employee": employee.pk,
                "position": position.pk,
                "kind": AppointmentKind.FROM_RESERVE,
                "appointment_date": timezone.localdate().isoformat(),
                "order_number": "412-к",
                "comment": "Назначение по действующему плану преемственности",
            },
        )

        entry.refresh_from_db()
        slate.refresh_from_db()
        employee.refresh_from_db()
        key_position.refresh_from_db()
        appointment = Appointment.objects.get()

        assert response.status_code == 302
        assert entry.status == ReserveStatus.APPOINTED
        assert slate.removed_on is not None
        assert employee.position_id == position.pk
        assert key_position.incumbent_id == employee.pk
        assert appointment.readiness_index_at_appointment == Decimal("82.00")
        assert appointment.months_in_reserve == 13

    def test_appointment_to_current_position_is_refused(self, hr_client, department, position):
        """Назначение на уже занимаемую должность отклоняется формой."""
        employee = EmployeeFactory(department=department, position=position)

        response = hr_client.post(
            reverse("succession:appointment_create"),
            {
                "employee": employee.pk,
                "position": position.pk,
                "kind": AppointmentKind.INTERNAL,
                "appointment_date": timezone.localdate().isoformat(),
            },
        )

        assert response.status_code == 200
        assert not Appointment.objects.exists()

    def test_statistics_shown_on_list(self, hr_client, department, position):
        """Доля назначений из резерва выводится в шапке списка."""
        Appointment.objects.create(
            employee=EmployeeFactory(department=department),
            position=position,
            kind=AppointmentKind.FROM_RESERVE,
            appointment_date=timezone.localdate(),
        )
        Appointment.objects.create(
            employee=EmployeeFactory(department=department),
            position=position,
            kind=AppointmentKind.EXTERNAL,
            appointment_date=timezone.localdate(),
        )

        response = hr_client.get(reverse("succession:appointments"))

        assert response.status_code == 200
        assert response.context["statistics"].reserve_share == 50.0

    def test_manager_cannot_register_appointment(self, manager_client, department, position):
        """Назначения регистрирует кадровая служба."""
        response = manager_client.post(
            reverse("succession:appointment_create"),
            {
                "employee": EmployeeFactory(department=department).pk,
                "position": position.pk,
                "kind": AppointmentKind.INTERNAL,
                "appointment_date": timezone.localdate().isoformat(),
            },
        )

        assert response.status_code == 403
        assert not Appointment.objects.exists()
