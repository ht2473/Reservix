"""Проверка методов программного интерфейса, добавленных вместе с оценкой."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.assessment.models import SessionStatus
from apps.competencies.models import Competency
from core.constants import Role, ScopeLevel
from tests.factories import UserFactory
from tests.factories.domain import (
    AssessmentScoreFactory,
    AssessmentSessionFactory,
    CompetencyFactory,
    CompetencyModelFactory,
    CompetencyRequirementFactory,
    EmployeeFactory,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

API = "/api/v1"


def payload(response):
    """Тело ответа, разобранное из JSON."""
    return response.json()


@pytest.fixture
def competency_model(db):
    """Модель из двух требований, одно из которых критичное."""
    model = CompetencyModelFactory(code="api-model", is_published=True)
    CompetencyRequirementFactory(
        competency_model=model,
        competency=CompetencyFactory(code="api-critical"),
        target_level=4,
        weight=Decimal("0.700"),
        is_critical=True,
    )
    CompetencyRequirementFactory(
        competency_model=model,
        competency=CompetencyFactory(code="api-regular"),
        target_level=3,
        weight=Decimal("0.300"),
    )
    return model


@pytest.fixture
def employee(branch, department, position, competency_model):
    """Сотрудник с утверждённой оценкой по всем требованиям."""
    position.competency_models.add(competency_model)
    person = EmployeeFactory(branch=branch, department=department, position=position)
    session = AssessmentSessionFactory(
        employee=person, competency_model=competency_model, status=SessionStatus.APPROVED
    )
    for requirement in competency_model.requirements.all():
        AssessmentScoreFactory(
            session=session,
            competency=requirement.competency,
            level=Decimal(str(requirement.target_level)),
        )
    return person


class TestScaleEndpoint:
    """Метод выдачи шкалы компетенции."""

    def test_шкала_содержит_индикаторы_всех_уровней(self, hr_client):
        competency = CompetencyFactory(code="api-scale")

        response = hr_client.get(f"{API}/competencies/competencies/{competency.pk}/scale")
        levels = payload(response)

        assert response.status_code == 200
        assert len(levels) == Competency.expected_scale_size()
        assert all(level["indicators"] for level in levels)

    def test_уровни_идут_по_возрастанию(self, hr_client):
        competency = CompetencyFactory(code="api-scale-order")
        levels = payload(hr_client.get(f"{API}/competencies/competencies/{competency.pk}/scale"))

        assert [item["level"] for item in levels] == sorted(item["level"] for item in levels)


class TestProfileEndpoint:
    """Метод выдачи профиля компетенций."""

    def test_профиль_содержит_соответствие_и_покрытие(self, hr_client, employee):
        """
        Покрытие передаётся рядом с соответствием: без него потребитель не отличит слабого
        кандидата от незавершённой оценки.
        """
        response = hr_client.get(f"{API}/competencies/profiles/{employee.public_id}")
        data = payload(response)

        assert response.status_code == 200
        assert data["match_score"] == 100.0
        assert data["coverage"] == 100.0
        assert data["competency_model_name"]

    def test_разрывы_передаются_с_происхождением_уровня(self, hr_client, employee):
        data = payload(hr_client.get(f"{API}/competencies/profiles/{employee.public_id}"))
        first = data["gaps"][0]

        assert {"confidence", "methods", "attainment", "is_blocking"} <= set(first)
        assert first["methods"] >= 1

    def test_профиль_можно_запросить_по_другой_модели(self, hr_client, employee):
        """Так внешняя система оценивает готовность к назначению."""
        target = CompetencyModelFactory(code="api-target", is_published=True)
        CompetencyRequirementFactory(
            competency_model=target,
            competency=CompetencyFactory(code="api-target-only"),
            target_level=5,
            weight=Decimal("1.000"),
        )

        data = payload(
            hr_client.get(
                f"{API}/competencies/profiles/{employee.public_id}", {"model": target.code}
            )
        )

        assert data["competency_model_id"] == target.pk
        assert data["coverage"] == 0.0

    def test_сотрудник_вне_области_видимости_не_раскрывается(
        self, client, employee, other_branch, other_department
    ):
        """
        Ответ «нет прав» сам по себе подтверждал бы, что такой сотрудник существует, поэтому
        отсутствие права неотличимо от отсутствия записи.
        """
        outsider = UserFactory(
            role=Role.MANAGER,
            scope_level=ScopeLevel.DEPARTMENT,
            branch=other_branch,
            department=other_department,
        )
        client.force_login(outsider)

        response = client.get(f"{API}/competencies/profiles/{employee.public_id}")

        assert response.status_code == 404

    def test_несуществующий_сотрудник_даёт_тот_же_ответ(self, hr_client):
        import uuid

        response = hr_client.get(f"{API}/competencies/profiles/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_сотрудник_без_модели_возвращает_пустой_профиль(self, hr_client, branch, department):
        """Отсутствие требований — не ошибка, а состояние справочника."""
        person = EmployeeFactory(branch=branch, department=department, position=None)

        data = payload(hr_client.get(f"{API}/competencies/profiles/{person.public_id}"))

        assert data["competency_model_id"] is None
        assert data["gaps"] == []
        assert data["match_score"] == 0.0


class TestSchemaComposition:
    """Состав описания интерфейса."""

    def test_новые_методы_попали_в_openapi(self, hr_client):
        schema = payload(hr_client.get(f"{API}/openapi.json"))
        paths = set(schema["paths"])

        assert "/api/v1/competencies/profiles/{employee_id}" in paths
        assert "/api/v1/competencies/competencies/{competency_id}/scale" in paths

    def test_профиль_не_раскрывает_личных_данных(self, hr_client):
        """
        Интерфейс предназначен для отчётности и интеграции: передача личных сведений потребовала бы
        отдельного основания обработки персональных данных.
        """
        schema = payload(hr_client.get(f"{API}/openapi.json"))
        properties = schema["components"]["schemas"]["CompetencyProfileSchema"]["properties"]

        assert "birth_date" not in properties
        assert "phone" not in properties
        assert "employee_name" not in properties
