"""Проверка программного интерфейса."""

from __future__ import annotations

import json

import pytest

from apps.people.models import Employee
from tests.factories.domain import (
    AssessmentScoreFactory,
    AssessmentSessionFactory,
    CompetencyFactory,
    CompetencyModelFactory,
    CompetencyRequirementFactory,
    EmployeeFactory,
    KeyPositionFactory,
    ReserveEntryFactory,
    ReserveGroupFactory,
)

pytestmark = pytest.mark.django_db

API = "/api/v1"


def payload(response) -> dict:
    """Разбирает тело ответа."""
    return json.loads(response.content)


# ---------------------------------------------------------------------------
# Аутентификация
# ---------------------------------------------------------------------------
class TestAuthentication:
    """Доступ к методам интерфейса."""

    def test_health_is_public(self, client):
        """Проверка состояния открыта без аутентификации."""
        response = client.get(f"{API}/system/health")

        assert response.status_code == 200
        assert payload(response)["status"] == "ok"
        assert payload(response)["database"] is True

    def test_data_requires_authentication(self, client):
        """Метод с данными без аутентификации отвечает отказом установленной формы."""
        response = client.get(f"{API}/people/employees")

        assert response.status_code == 401
        assert payload(response)["code"] == "unauthorized"

    def test_basic_auth_is_accepted(self, client, hr_user, known_password):
        """Базовая аутентификация допускается для внешних потребителей."""
        import base64

        credentials = base64.b64encode(f"{hr_user.email}:{known_password}".encode()).decode()
        response = client.get(f"{API}/system/me", headers={"authorization": f"Basic {credentials}"})

        assert response.status_code == 200
        assert payload(response)["email"] == hr_user.email

    def test_me_reports_role_and_scope(self, manager_client, manager_user):
        """Метод сведений о себе сообщает границы прав."""
        response = manager_client.get(f"{API}/system/me")
        body = payload(response)

        assert body["role"] == "manager"
        assert body["scope_level"] == manager_user.scope_level
        assert body["department_id"] == manager_user.department_id


# ---------------------------------------------------------------------------
# Область видимости
# ---------------------------------------------------------------------------
class TestScope:
    """Ограничение выборок областью видимости вызывающего."""

    def test_hr_sees_whole_organization(self, hr_client, department, other_department, branch):
        """Кадровая служба видит сотрудников всех подразделений."""
        EmployeeFactory(department=department, branch=branch)
        EmployeeFactory(department=other_department, branch=branch)

        response = hr_client.get(f"{API}/people/employees")
        assert payload(response)["count"] == 2

    def test_manager_sees_own_subtree_only(
        self, manager_client, department, child_department, other_department, branch
    ):
        """Руководитель видит своё подразделение и вложенные, но не соседние."""
        EmployeeFactory(department=department, branch=branch)
        EmployeeFactory(department=child_department, branch=branch)
        EmployeeFactory(department=other_department, branch=branch)

        response = manager_client.get(f"{API}/people/employees")
        assert payload(response)["count"] == 2

    def test_employee_sees_own_card_only(self, auth_client, employee_user, department, branch):
        """Рядовой сотрудник видит только собственную карточку."""
        EmployeeFactory(user=employee_user, department=department, branch=branch)
        EmployeeFactory(department=department, branch=branch)

        response = auth_client.get(f"{API}/people/employees")
        assert payload(response)["count"] == 1

    def test_foreign_card_is_refused(self, auth_client, other_department, branch):
        """Обращение к чужой карточке по прямому адресу отклоняется."""
        stranger = EmployeeFactory(department=other_department, branch=branch)

        response = auth_client.get(f"{API}/people/employees/{stranger.public_id}")

        assert response.status_code == 403
        assert payload(response)["code"] == "forbidden"

    def test_reserve_entries_follow_the_same_scope(
        self, manager_client, department, other_department, branch
    ):
        """Записи резерва ограничиваются областью видимости так же, как карточки."""
        ReserveEntryFactory(employee=EmployeeFactory(department=department, branch=branch))
        ReserveEntryFactory(employee=EmployeeFactory(department=other_department, branch=branch))

        response = manager_client.get(f"{API}/reserve/entries")
        assert payload(response)["count"] == 1


# ---------------------------------------------------------------------------
# Выборки и отбор
# ---------------------------------------------------------------------------
class TestListings:
    """Списочные методы: отбор, поиск, постраничная навигация."""

    def test_employee_filter_by_department(self, hr_client, department, other_department, branch):
        """Отбор по подразделению оставляет только его сотрудников."""
        EmployeeFactory(department=department, branch=branch)
        EmployeeFactory(department=other_department, branch=branch)

        response = hr_client.get(f"{API}/people/employees", {"department": department.pk})
        assert payload(response)["count"] == 1

    def test_employee_search(self, hr_client, department, branch):
        """Поиск находит сотрудника по фрагменту фамилии."""
        EmployeeFactory(last_name="Одинцов", department=department, branch=branch)
        EmployeeFactory(last_name="Кузнецов", department=department, branch=branch)

        response = hr_client.get(f"{API}/people/employees", {"search": "динц"})
        assert payload(response)["count"] == 1

    def test_pagination_limits_result(self, hr_client, department, branch):
        """Постраничная навигация ограничивает объём ответа."""
        EmployeeFactory.create_batch(5, department=department, branch=branch)

        response = hr_client.get(f"{API}/people/employees", {"limit": 2})
        body = payload(response)

        assert len(body["items"]) == 2
        assert body["count"] == 5

    def test_department_tree_is_nested(self, hr_client, department, child_department):
        """Дерево подразделений возвращается вложенным, а не плоским списком."""
        response = hr_client.get(f"{API}/org/departments/tree")
        tree = payload(response)

        root = next(node for node in tree if node["id"] == department.pk)
        assert [child["id"] for child in root["children"]] == [child_department.pk]

    def test_department_tree_counts_staff(self, hr_client, department, branch):
        """Каждый узел дерева содержит численность сотрудников."""
        EmployeeFactory(department=department, branch=branch)

        response = hr_client.get(f"{API}/org/departments/tree")
        root = next(node for node in payload(response) if node["id"] == department.pk)

        assert root["headcount"] == 1

    def test_reserve_groups_report_membership(self, hr_client):
        """Группа резерва сообщает численность участников."""
        ReserveGroupFactory()
        response = hr_client.get(f"{API}/reserve/groups")

        assert payload(response)[0]["member_count"] == 0


# ---------------------------------------------------------------------------
# Профиль компетенций
# ---------------------------------------------------------------------------
class TestCompetencyProfile:
    """Сопоставление оценок с требованиями модели компетенций."""

    def test_gap_is_computed_against_model(self, hr_client, position, department, branch):
        """Разрыв считается относительно целевого уровня модели."""
        employee = EmployeeFactory(position=position, department=department, branch=branch)
        competency = CompetencyFactory()
        model = CompetencyModelFactory()
        model.positions.add(position)
        CompetencyRequirementFactory(competency_model=model, competency=competency, target_level=5)

        session = AssessmentSessionFactory(employee=employee, competency_model=model)
        AssessmentScoreFactory(session=session, competency=competency, level="3.0")

        response = hr_client.get(f"{API}/people/employees/{employee.public_id}/competencies")
        item = payload(response)[0]

        assert item["actual_level"] == 3.0
        assert item["target_level"] == 5
        assert item["gap"] == 2.0

    def test_unassessed_requirement_is_full_gap(self, hr_client, position, department, branch):
        """Неоценённая компетенция даёт полный разрыв, а не нулевой."""
        employee = EmployeeFactory(position=position, department=department, branch=branch)
        model = CompetencyModelFactory()
        model.positions.add(position)
        CompetencyRequirementFactory(competency_model=model, target_level=4)

        response = hr_client.get(f"{API}/people/employees/{employee.public_id}/competencies")
        item = payload(response)[0]

        assert item["actual_level"] is None
        assert item["gap"] == 4.0


# ---------------------------------------------------------------------------
# Изменяющие операции
# ---------------------------------------------------------------------------
class TestReserveTransitions:
    """Смена статуса записи резерва через программный интерфейс."""

    def test_hr_may_change_status(self, hr_client):
        """Кадровая служба переводит запись в допустимый статус."""
        entry = ReserveEntryFactory(status="candidate")

        response = hr_client.post(
            f"{API}/reserve/entries/{entry.public_id}/transition",
            data=json.dumps({"status": "active", "reason": "Оценка подтвердила готовность"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        entry.refresh_from_db()
        assert entry.status == "active"
        assert entry.status_history.count() == 1

    def test_invalid_transition_is_rejected(self, hr_client):
        """Недопустимый переход отклоняется с пояснением."""
        entry = ReserveEntryFactory(status="active")
        entry.transition_to("excluded", reason="Перевод в другой филиал")

        response = hr_client.post(
            f"{API}/reserve/entries/{entry.public_id}/transition",
            data=json.dumps({"status": "active", "reason": "Возврат"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert payload(response)["code"] == "invalid_transition"

    def test_employee_may_not_change_status(self, auth_client):
        """Рядовой сотрудник решений о составе резерва не принимает."""
        entry = ReserveEntryFactory(status="candidate")

        response = auth_client.post(
            f"{API}/reserve/entries/{entry.public_id}/transition",
            data=json.dumps({"status": "active", "reason": "Прошу включить"}),
            content_type="application/json",
        )

        assert response.status_code == 403


class TestNominations:
    """Подача заявок на выдвижение."""

    def test_manager_submits_nomination(self, manager_client, position, department, branch):
        """Руководитель выдвигает сотрудника с обоснованием."""
        employee = EmployeeFactory(department=department, branch=branch)

        response = manager_client.post(
            f"{API}/reserve/nominations",
            data=json.dumps(
                {
                    "employee_id": employee.pk,
                    "target_position_id": position.pk,
                    "justification": (
                        "Два года руководит проектной группой, закрыл план развития "
                        "и прошёл оценку по управленческим компетенциям"
                    ),
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 201
        assert payload(response)["status"] == "submitted"

    def test_short_justification_is_rejected(self, manager_client, position, department, branch):
        """Обоснование короче тридцати символов не принимается."""
        employee = EmployeeFactory(department=department, branch=branch)

        response = manager_client.post(
            f"{API}/reserve/nominations",
            data=json.dumps(
                {
                    "employee_id": employee.pk,
                    "target_position_id": position.pk,
                    "justification": "Хороший",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 422
        assert payload(response)["code"] == "validation_error"
        assert "justification" in payload(response)["fields"]

    def test_duplicate_pending_nomination_is_rejected(
        self, manager_client, position, department, branch
    ):
        """Повторная заявка при нерассмотренной предыдущей отклоняется."""
        employee = EmployeeFactory(department=department, branch=branch)
        body = json.dumps(
            {
                "employee_id": employee.pk,
                "target_position_id": position.pk,
                "justification": "Кандидат подготовлен и рекомендован руководителем отдела",
            }
        )

        manager_client.post(
            f"{API}/reserve/nominations", data=body, content_type="application/json"
        )
        response = manager_client.post(
            f"{API}/reserve/nominations", data=body, content_type="application/json"
        )

        assert response.status_code == 400
        assert payload(response)["code"] == "duplicate_nomination"

    def test_dismissed_employee_may_not_be_nominated(
        self, manager_client, position, department, branch
    ):
        """Уволенный сотрудник не может быть выдвинут в резерв."""
        from django.utils import timezone

        employee = EmployeeFactory(
            department=department,
            branch=branch,
            status="dismissed",
            dismissal_date=timezone.localdate(),
        )

        response = manager_client.post(
            f"{API}/reserve/nominations",
            data=json.dumps(
                {
                    "employee_id": employee.pk,
                    "target_position_id": position.pk,
                    "justification": "Кандидат подготовлен и рекомендован руководителем отдела",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert payload(response)["code"] == "not_eligible"


# ---------------------------------------------------------------------------
# Аналитика
# ---------------------------------------------------------------------------
class TestAnalytics:
    """Показатели и распределения."""

    def test_metrics_report_reserve_share(self, hr_client, department, branch):
        """Доля сотрудников в резерве считается от численности штата."""
        employees = EmployeeFactory.create_batch(4, department=department, branch=branch)
        ReserveEntryFactory(employee=employees[0])

        body = payload(hr_client.get(f"{API}/analytics/metrics"))

        assert body["employees_total"] == 4
        assert body["reserve_entries"] == 1
        assert body["reserve_share"] == 25.0

    def test_metrics_exclude_external_candidates_from_headcount(
        self, hr_client, department, branch
    ):
        """Внешние кандидаты не входят в численность организации."""
        EmployeeFactory(department=department, branch=branch)
        EmployeeFactory(is_external=True, branch=None, department=None, position=None)

        assert payload(hr_client.get(f"{API}/analytics/metrics"))["employees_total"] == 1

    def test_key_position_coverage(self, hr_client, position):
        """Обеспеченность критичных должностей входит в показатели."""
        KeyPositionFactory(position=position, required_successors=1)

        body = payload(hr_client.get(f"{API}/analytics/metrics"))
        assert body["key_positions"] == 1
        assert body["covered_key_positions"] == 0

    def test_nine_box_distribution_has_all_cells(self, hr_client, department, branch):
        """Распределение возвращает все девять ячеек, включая пустые."""
        from tests.factories.domain import NineBoxPlacementFactory

        NineBoxPlacementFactory(employee=EmployeeFactory(department=department, branch=branch))

        body = payload(hr_client.get(f"{API}/analytics/nine-box"))

        assert len(body["cells"]) == 9
        assert body["total"] == 1
        assert sum(cell["count"] for cell in body["cells"]) == 1

    def test_reserve_funnel_reports_stages(self, hr_client):
        """Воронка резерва показывает численность на каждой стадии."""
        ReserveEntryFactory(status="candidate")
        ReserveEntryFactory(status="active", readiness_level="high")

        stages = {
            item["stage"]: item["count"]
            for item in payload(hr_client.get(f"{API}/analytics/reserve-funnel"))
        }

        assert stages["candidate"] == 1
        assert stages["active"] == 1
        assert stages["ready"] == 1


# ---------------------------------------------------------------------------
# Документация интерфейса
# ---------------------------------------------------------------------------
class TestOpenApi:
    """Описание интерфейса в формате OpenAPI."""

    def test_schema_is_published(self, hr_client):
        """Схема доступна и содержит описанные разделы."""
        response = hr_client.get(f"{API}/openapi.json")
        schema = payload(response)

        assert response.status_code == 200
        assert schema["info"]["title"] == "Reservix API"

        tags = {
            tag
            for path in schema["paths"].values()
            for method in path.values()
            for tag in method.get("tags", [])
        }
        assert {"Сотрудники", "Кадровый резерв", "Аналитика"} <= tags

    def test_employee_schema_hides_personal_data(self, hr_client):
        """Схема сотрудника не раскрывает дату рождения и личные контакты."""
        schema = payload(hr_client.get(f"{API}/openapi.json"))
        properties = schema["components"]["schemas"]["EmployeeDetailSchema"]["properties"]

        assert "birth_date" not in properties
        assert "phone" not in properties
        assert "education" not in properties


def test_employee_manager_is_not_leaked_across_scopes(hr_client, department, branch):
    """Служебная проверка целостности данных теста."""
    EmployeeFactory(department=department, branch=branch)
    assert Employee.objects.count() == 1
