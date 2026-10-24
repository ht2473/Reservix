"""Проверка стартовых страниц рабочих мест."""

from __future__ import annotations

import datetime as dt

import pytest

from django.urls import reverse
from django.utils import timezone

from apps.development.models import ActivityStatus, PlanStatus
from core.navigation import ROLE_HOME
from tests.factories import PositionFactory
from tests.factories.domain import (
    DevelopmentActivityFactory,
    DevelopmentPlanFactory,
    EmployeeFactory,
    KeyPositionFactory,
    ReserveEntryFactory,
    ReserveNominationFactory,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


class TestRoleHome:
    """Соответствие роли и её стартовой страницы."""

    @pytest.mark.parametrize(
        ("role", "url_name"),
        [
            ("hr", "dashboard:hr"),
            ("manager", "dashboard:manager"),
            ("employee", "dashboard:employee"),
            ("admin", "administration:index"),
            ("mentor", "development:mentees"),
        ],
    )
    def test_role_home_declared(self, role, url_name):
        assert ROLE_HOME[role] == url_name

    @pytest.mark.parametrize(
        ("client_fixture", "expected"),
        [
            ("hr_client", "dashboard:hr"),
            ("manager_client", "dashboard:manager"),
            ("auth_client", "dashboard:employee"),
        ],
    )
    def test_role_home_opens(self, request, client_fixture, expected):
        client = request.getfixturevalue(client_fixture)

        response = client.get(reverse(expected))

        assert response.status_code == 200

    def test_login_lands_on_workspace_not_portal(self, client, hr_user, known_password):
        """Вход направляет на рабочее место, а не на публичную главную."""
        hr_user.set_password(known_password)
        hr_user.must_change_password = False
        hr_user.save(update_fields=["password", "must_change_password"])

        response = client.post(
            reverse("accounts:login"),
            {"username": hr_user.email, "password": known_password},
        )

        assert response.status_code == 302
        assert response.url == reverse("dashboard:hr")


class TestAccess:
    """Разграничение доступа к дашбордам."""

    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("dashboard:hr"))

        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_employee_denied_hr_dashboard(self, auth_client):
        assert auth_client.get(reverse("dashboard:hr")).status_code == 403

    def test_employee_denied_manager_dashboard(self, auth_client):
        assert auth_client.get(reverse("dashboard:manager")).status_code == 403

    def test_hr_denied_manager_dashboard(self, hr_client):
        """Дашборд руководителя закрыт и для кадровой службы."""
        assert hr_client.get(reverse("dashboard:manager")).status_code == 403


class TestHrDashboard:
    """Рабочее место кадровой службы."""

    def test_opens_on_empty_data(self, hr_client):
        """Страница открывается на пустой базе."""
        response = hr_client.get(reverse("dashboard:hr"))

        assert response.status_code == 200
        assert response.context["health"].entries == 0

    def test_queue_counts_match_lists(self, hr_client, branch, department, position):
        employee = EmployeeFactory(branch=branch, department=department, position=position)
        ReserveEntryFactory(
            employee=employee,
            target_position=position,
            review_date=timezone.localdate() - dt.timedelta(days=10),
        )
        ReserveNominationFactory(employee=employee, target_position=PositionFactory())

        response = hr_client.get(reverse("dashboard:hr"))
        queue = {str(item["label"]): item["count"] for item in response.context["queue"]}

        assert queue["Просрочен пересмотр резерва"] == 1

    def test_funnel_counts_ready_inside_reserve(self, hr_client, branch, department, position):
        """Стадия «готовы» вложена в стадию «в резерве», а не следует за ней."""
        employee = EmployeeFactory(branch=branch, department=department, position=position)
        ReserveEntryFactory(
            employee=employee,
            target_position=position,
            status="active",
            readiness_level="high",
        )

        funnel = {
            item["stage"]: item["count"]
            for item in hr_client.get(reverse("dashboard:hr")).context["funnel"]
        }

        assert funnel["active"] == 1
        assert funnel["ready"] == 1

    def test_uncovered_key_positions_listed(self, hr_client, position):
        KeyPositionFactory(position=position)

        response = hr_client.get(reverse("dashboard:hr"))

        assert len(response.context["uncovered"]) == 1

    def test_admin_opens_hr_dashboard(self, admin_client):
        """Администратору дашборд кадровой службы доступен: у него полная область видимости."""
        assert admin_client.get(reverse("dashboard:hr")).status_code == 200

    def test_query_count_does_not_grow_with_data(
        self, hr_client, branch, department, position, django_assert_max_num_queries
    ):
        """Число запросов не зависит от объёма данных."""
        for index in range(20):
            employee = EmployeeFactory(branch=branch, department=department, position=position)
            entry = ReserveEntryFactory(employee=employee, target_position=PositionFactory())
            plan = DevelopmentPlanFactory(employee=employee, entry=entry)
            DevelopmentActivityFactory(plan=plan)
            if index % 4 == 0:
                KeyPositionFactory(position=PositionFactory())

        with django_assert_max_num_queries(45):
            response = hr_client.get(reverse("dashboard:hr"))

        assert response.status_code == 200

    def test_coverage_is_not_computed_twice_per_render(self, hr_client, position):
        """Отчёт об обеспеченности не пересчитывается внутри расчёта показателей."""
        from unittest.mock import patch

        import apps.succession.services as succession

        KeyPositionFactory(position=position)

        with patch.object(succession, "coverage_report", wraps=succession.coverage_report) as spy:
            response = hr_client.get(reverse("dashboard:hr"))

        assert response.status_code == 200
        assert spy.call_count == 0, "Показатели здоровья резерва пересчитали обеспеченность"

    def test_reserve_health_computes_coverage_when_not_given(self, position):
        """
        Обратная сторона предыдущей проверки: без готового отчёта расчёт показателей считает его
        сам.
        """
        from unittest.mock import patch

        import apps.succession.services as succession
        from apps.analytics.services import reserve_health

        KeyPositionFactory(position=position)

        with patch.object(succession, "coverage_report", wraps=succession.coverage_report) as spy:
            reserve_health()

        assert spy.call_count == 1


class TestManagerDashboard:
    """Рабочее место руководителя подразделения."""

    def test_opens_on_empty_data(self, manager_client):
        response = manager_client.get(reverse("dashboard:manager"))

        assert response.status_code == 200
        assert response.context["team_size"] == 0

    def test_counts_only_own_department(
        self, manager_client, branch, department, other_department, position
    ):
        EmployeeFactory(branch=branch, department=department, position=position)
        EmployeeFactory(branch=branch, department=other_department, position=position)

        response = manager_client.get(reverse("dashboard:manager"))

        assert response.context["team_size"] == 1

    def test_without_plan_counts_employees_lacking_active_plan(
        self, manager_client, branch, department, position
    ):
        with_plan = EmployeeFactory(branch=branch, department=department, position=position)
        EmployeeFactory(branch=branch, department=department, position=position)
        DevelopmentPlanFactory(employee=with_plan, status=PlanStatus.ACTIVE)

        response = manager_client.get(reverse("dashboard:manager"))

        assert response.context["without_plan"] == 1

    def test_overdue_activities_counted(self, manager_client, branch, department, position):
        employee = EmployeeFactory(branch=branch, department=department, position=position)
        plan = DevelopmentPlanFactory(employee=employee)
        DevelopmentActivityFactory(
            plan=plan,
            due_date=timezone.localdate() - dt.timedelta(days=5),
            status=ActivityStatus.PENDING,
        )

        response = manager_client.get(reverse("dashboard:manager"))
        queue = {str(item["label"]): item["count"] for item in response.context["queue"]}

        assert queue["Просроченные мероприятия"] == 1


class TestEmployeeDashboard:
    """Личный кабинет сотрудника."""

    def test_account_without_card_explains_itself(self, auth_client):
        """Учётная запись без кадровой карточки — рабочий случай, а не сбой."""
        response = auth_client.get(reverse("dashboard:employee"))

        assert response.status_code == 200
        assert response.context["employee"] is None
        assert "Кадровая карточка не заведена" in response.content.decode()

    def test_shows_own_plan_and_reserve(self, client, employee_user, branch, department, position):
        employee = EmployeeFactory(
            user=employee_user, branch=branch, department=department, position=position
        )
        DevelopmentPlanFactory(employee=employee, title="Подготовка к роли руководителя")
        ReserveEntryFactory(employee=employee, target_position=position)
        client.force_login(employee_user)

        response = client.get(reverse("dashboard:employee"))
        body = response.content.decode()

        assert "Подготовка к роли руководителя" in body
        assert response.context["entries"].count() == 1

    def test_readiness_index_not_shown_as_number(
        self, client, employee_user, branch, department, position
    ):
        """Сотруднику показывается уровень готовности, но не значение индекса."""
        employee = EmployeeFactory(
            user=employee_user, branch=branch, department=department, position=position
        )
        ReserveEntryFactory(employee=employee, target_position=position)
        client.force_login(employee_user)

        body = client.get(reverse("dashboard:employee")).content.decode()

        assert "62,5" not in body
        assert "62.5" not in body

    def test_sees_only_own_records(self, client, employee_user, branch, department, position):
        mine = EmployeeFactory(
            user=employee_user, branch=branch, department=department, position=position
        )
        colleague = EmployeeFactory(
            last_name="Чужаков", branch=branch, department=department, position=position
        )
        DevelopmentPlanFactory(employee=mine, title="Мой план")
        DevelopmentPlanFactory(employee=colleague, title="Чужой план")
        client.force_login(employee_user)

        body = client.get(reverse("dashboard:employee")).content.decode()

        assert "Мой план" in body
        assert "Чужой план" not in body
