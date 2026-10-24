"""Проверка раздела сотрудников."""

from __future__ import annotations

import pytest

from django.urls import reverse

from apps.people.models import AttritionRisk, EmployeeStatus, Mobility
from tests.factories import PositionFactory
from tests.factories.domain import (
    AssessmentSessionFactory,
    DevelopmentPlanFactory,
    EmployeeFactory,
    ReserveEntryFactory,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def staff(branch, department, child_department, other_department, position):
    """Три сотрудника: в подразделении, во вложенном отделе и вне поддерева."""
    return {
        "own": EmployeeFactory(
            last_name="Кузнецов",
            branch=branch,
            department=department,
            position=position,
        ),
        "nested": EmployeeFactory(
            last_name="Лебедев",
            branch=branch,
            department=child_department,
            position=position,
        ),
        "outside": EmployeeFactory(
            last_name="Соколов",
            branch=branch,
            department=other_department,
            position=position,
        ),
    }


class TestAccess:
    """Доступ к разделу по ролям."""

    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("people:list"))

        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_hr_opens_directory(self, hr_client, staff):
        response = hr_client.get(reverse("people:list"))

        assert response.status_code == 200
        assert "Кузнецов" in response.content.decode()

    def test_employee_denied_directory(self, auth_client, staff):
        """Рядовой сотрудник не получает доступ к кадровому справочнику."""
        response = auth_client.get(reverse("people:list"))

        assert response.status_code == 403

    def test_manager_opens_team(self, manager_client, staff):
        response = manager_client.get(reverse("people:team"))

        assert response.status_code == 200


class TestScope:
    """Область видимости."""

    def test_manager_sees_subtree_only(self, manager_client, staff):
        response = manager_client.get(reverse("people:list"))
        body = response.content.decode()

        assert "Кузнецов" in body
        assert "Лебедев" in body
        assert "Соколов" not in body

    def test_hr_sees_whole_organization(self, hr_client, staff):
        body = hr_client.get(reverse("people:list")).content.decode()

        assert "Соколов" in body

    def test_card_of_foreign_employee_denied(self, manager_client, staff):
        """Подстановка идентификатора чужого сотрудника отклоняется."""
        response = manager_client.get(staff["outside"].get_absolute_url())

        assert response.status_code == 403

    def test_card_of_own_employee_opens(self, manager_client, staff):
        response = manager_client.get(staff["own"].get_absolute_url())

        assert response.status_code == 200
        assert "Кузнецов" in response.content.decode()

    def test_employee_opens_own_card(self, client, employee_user, branch, department, position):
        """Сотрудник видит собственную карточку, но не карточку коллеги."""
        mine = EmployeeFactory(
            user=employee_user, branch=branch, department=department, position=position
        )
        colleague = EmployeeFactory(branch=branch, department=department, position=position)
        client.force_login(employee_user)

        assert client.get(mine.get_absolute_url()).status_code == 200
        assert client.get(colleague.get_absolute_url()).status_code == 403


class TestFilters:
    """Отбор в кадровом справочнике."""

    def test_search_finds_by_surname(self, hr_client, staff):
        body = hr_client.get(reverse("people:list"), {"q": "Кузн"}).content.decode()

        assert "Кузнецов" in body
        assert "Соколов" not in body

    def test_search_finds_by_personnel_number(self, hr_client, staff):
        number = staff["own"].personnel_number
        body = hr_client.get(reverse("people:list"), {"q": number}).content.decode()

        assert number in body

    def test_department_filter_covers_nested(self, hr_client, staff, department):
        """Отбор по департаменту захватывает вложенные отделы."""
        body = hr_client.get(reverse("people:list"), {"department": department.pk}).content.decode()

        assert "Кузнецов" in body
        assert "Лебедев" in body

    def test_scope_reserve_keeps_only_reservists(self, hr_client, staff, position):
        ReserveEntryFactory(employee=staff["own"], target_position=position)

        body = hr_client.get(reverse("people:list"), {"scope": "reserve"}).content.decode()

        assert "Кузнецов" in body
        assert "Соколов" not in body

    def test_branch_field_hidden_from_manager(self, manager_client, staff):
        """Руководителю не показывается отбор по филиалу."""
        response = manager_client.get(reverse("people:list"))

        assert "branch" not in response.context["filter_form"].fields


class TestSummary:
    """Сводка в шапке справочника."""

    def test_counts_reflect_scope(self, manager_client, hr_client, staff):
        manager_total = manager_client.get(reverse("people:list")).context["summary"]["total"]
        hr_total = hr_client.get(reverse("people:list")).context["summary"]["total"]

        assert manager_total == 2
        assert hr_total == 3

    def test_reserve_participation_counts_people_not_entries(self, hr_client, staff, position):
        """Показатель «состоят в резерве» считает людей, а не записи."""
        ReserveEntryFactory(employee=staff["own"], target_position=position)
        ReserveEntryFactory(employee=staff["own"], target_position=PositionFactory())

        summary = hr_client.get(reverse("people:list")).context["summary"]

        assert summary["in_reserve"] == 1


class TestCard:
    """Карточка сотрудника."""

    def test_card_shows_related_records(self, hr_client, staff, position):
        employee = staff["own"]
        ReserveEntryFactory(employee=employee, target_position=position)
        DevelopmentPlanFactory(employee=employee, title="План на руководителя отдела")
        AssessmentSessionFactory(employee=employee)

        response = hr_client.get(employee.get_absolute_url())
        body = response.content.decode()

        assert response.status_code == 200
        assert "План на руководителя отдела" in body
        assert response.context["entries"].count() == 1
        assert len(response.context["sessions"]) == 1

    def test_card_lists_subordinates(self, hr_client, staff, branch, department, position):
        chief = staff["own"]
        EmployeeFactory(
            last_name="Подчинённый",
            manager=chief,
            branch=branch,
            department=department,
            position=position,
        )

        body = hr_client.get(chief.get_absolute_url()).content.decode()

        assert "Подчинённый" in body


class TestCardEditing:
    """Правка характеристик, влияющих на расчёт готовности."""

    def test_hr_updates_card(self, hr_client, staff):
        employee = staff["own"]
        url = reverse("people:employee_edit", args=[employee.public_id])

        response = hr_client.post(
            url,
            {
                "mobility": Mobility.COUNTRY,
                "attrition_risk": AttritionRisk.LOW,
                "is_key_talent": "on",
                "notes": "Готов к переезду в новосибирский филиал",
            },
        )

        employee.refresh_from_db()
        assert response.status_code == 302
        assert employee.is_key_talent is True
        assert employee.mobility == Mobility.COUNTRY

    def test_exclusion_requires_reason(self, hr_client, staff):
        """Исключение из резерва без основания отклоняется."""
        employee = staff["own"]
        url = reverse("people:employee_edit", args=[employee.public_id])

        response = hr_client.post(
            url,
            {
                "mobility": Mobility.CITY,
                "attrition_risk": AttritionRisk.LOW,
                "is_excluded_from_reserve": "on",
                "exclusion_note": "",
            },
        )

        employee.refresh_from_db()
        assert response.status_code == 200
        assert employee.is_excluded_from_reserve is False
        assert "exclusion_note" in response.context["form"].errors

    def test_key_talent_at_risk_saves_with_warning(self, hr_client, staff):
        """Сочетание «ключевой сотрудник — высокий риск» сохраняется, но с предупреждением."""
        employee = staff["own"]
        url = reverse("people:employee_edit", args=[employee.public_id])

        response = hr_client.post(
            url,
            {
                "mobility": Mobility.CITY,
                "attrition_risk": AttritionRisk.HIGH,
                "is_key_talent": "on",
            },
            follow=True,
        )

        employee.refresh_from_db()
        assert employee.is_key_talent is True
        assert employee.attrition_risk == AttritionRisk.HIGH
        assert any("преемственности" in str(m) for m in response.context["messages"])

    def test_manager_cannot_edit_card(self, manager_client, staff):
        url = reverse("people:employee_edit", args=[staff["own"].public_id])

        assert manager_client.get(url).status_code == 403


class TestTeamPage:
    """Состав подразделения глазами руководителя."""

    def test_stale_assessment_marked(self, manager_client, staff):
        """Отсутствие свежей оценки помечается явно, а не пустой ячейкой."""
        response = manager_client.get(reverse("people:team"))

        assert response.status_code == 200
        assert "устарела" in response.content.decode()
        assert response.context["assessed_ids"] == set()

    def test_dismissed_excluded_from_team(self, manager_client, staff, branch, department):
        """Уволенный не показывается в составе подразделения."""
        EmployeeFactory(
            last_name="Уволенный",
            branch=branch,
            department=department,
            status=EmployeeStatus.DISMISSED,
            dismissal_date="2025-01-31",
        )

        body = manager_client.get(reverse("people:team")).content.decode()

        assert "Уволенный" not in body
