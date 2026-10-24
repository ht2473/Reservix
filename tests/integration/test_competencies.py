"""Проверка раздела компетенций: страницы, права доступа, построение профиля."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from django.urls import reverse

from apps.assessment.models import AssessmentMethod, SessionStatus
from apps.competencies.models import Competency, CompetencyModel
from apps.competencies.services import (
    build_gap_matrix,
    build_profile,
    model_for_employee,
    resolve_competency_model,
)
from core.constants import ScopeLevel
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


@pytest.fixture
def model_with_requirements(db) -> CompetencyModel:
    """Модель из двух требований: одно критичное, веса в сумме дают единицу."""
    model = CompetencyModelFactory(code="model-profile", is_published=True)
    CompetencyRequirementFactory(
        competency_model=model,
        competency=CompetencyFactory(code="profile-critical"),
        target_level=4,
        weight=Decimal("0.600"),
        is_critical=True,
    )
    CompetencyRequirementFactory(
        competency_model=model,
        competency=CompetencyFactory(code="profile-regular"),
        target_level=3,
        weight=Decimal("0.400"),
    )
    return model


@pytest.fixture
def assessed_employee(branch, department, position, model_with_requirements):
    """Сотрудник с утверждённой оценкой по всем требованиям модели."""
    position.competency_models.add(model_with_requirements)
    employee = EmployeeFactory(branch=branch, department=department, position=position)

    session = AssessmentSessionFactory(
        employee=employee,
        competency_model=model_with_requirements,
        method=AssessmentMethod.MANAGER,
        status=SessionStatus.APPROVED,
    )
    for requirement in model_with_requirements.requirements.all():
        AssessmentScoreFactory(
            session=session,
            competency=requirement.competency,
            level=Decimal(str(requirement.target_level)),
        )
    return employee


# ---------------------------------------------------------------------------
# Выбор модели требований
# ---------------------------------------------------------------------------
class TestModelResolution:
    """Правило выбора модели: точное вытесняет общее."""

    def test_модель_должности_вытесняет_модель_грейда(self, position, grade):
        """Иначе настройка требований к конкретной должности не имела бы смысла."""
        by_grade = CompetencyModelFactory(code="by-grade", grade=grade, is_published=True)
        by_position = CompetencyModelFactory(code="by-position", is_published=True)
        by_position.positions.add(position)

        resolved = resolve_competency_model(position_id=position.pk, grade_id=grade.pk)

        assert resolved == by_position
        assert resolved != by_grade

    def test_модель_грейда_применяется_при_отсутствии_модели_должности(self, position, grade):
        by_grade = CompetencyModelFactory(code="fallback-grade", grade=grade, is_published=True)

        assert resolve_competency_model(position_id=position.pk, grade_id=grade.pk) == by_grade

    def test_неопубликованная_модель_не_применяется(self, position, grade):
        """Черновик не должен попадать в расчёты и кампании оценки."""
        draft = CompetencyModelFactory(code="draft-model", is_published=False)
        draft.positions.add(position)

        assert resolve_competency_model(position_id=position.pk, grade_id=grade.pk) is None

    def test_из_нескольких_версий_берётся_старшая(self, position):
        old = CompetencyModelFactory(code="versioned-1", version=1, is_published=True)
        new = CompetencyModelFactory(code="versioned-2", version=2, is_published=True)
        old.positions.add(position)
        new.positions.add(position)

        assert resolve_competency_model(position_id=position.pk) == new

    def test_сотрудник_без_должности_модели_не_имеет(self, branch, department):
        employee = EmployeeFactory(branch=branch, department=department, position=None)
        assert model_for_employee(employee) is None


# ---------------------------------------------------------------------------
# Построение профиля
# ---------------------------------------------------------------------------
class TestProfileBuilding:
    """Профиль строится по требованиям модели, а не по всем оценкам подряд."""

    def test_профиль_охватывает_все_требования_модели(
        self, assessed_employee, model_with_requirements
    ):
        profile = build_profile(assessed_employee)

        assert profile.competency_model == model_with_requirements
        assert len(profile.gaps) == model_with_requirements.requirements.count()

    def test_соответствие_целевым_уровням_даёт_сто_процентов(self, assessed_employee):
        assert build_profile(assessed_employee).match_score == 100.0
        assert build_profile(assessed_employee).coverage == 100.0

    def test_оценки_вне_модели_в_профиль_не_входят(
        self, assessed_employee, model_with_requirements
    ):
        """Компетенция, к должности не относящаяся, размывала бы показатель соответствия."""
        stranger = CompetencyFactory(code="outside-model")
        session = AssessmentSessionFactory(
            employee=assessed_employee,
            competency_model=model_with_requirements,
            status=SessionStatus.APPROVED,
        )
        AssessmentScoreFactory(session=session, competency=stranger, level=Decimal("1.0"))

        profile = build_profile(assessed_employee)

        assert stranger.pk not in {gap.requirement.competency_id for gap in profile.gaps}
        assert profile.match_score == 100.0

    def test_неутверждённые_оценки_в_расчёт_не_идут(
        self, branch, department, position, model_with_requirements
    ):
        """
        Оценка руководителя попадает в расчёт после проверки кадровой службой: иначе показатель
        меняется от каждого черновика.
        """
        position.competency_models.add(model_with_requirements)
        employee = EmployeeFactory(branch=branch, department=department, position=position)
        session = AssessmentSessionFactory(
            employee=employee,
            competency_model=model_with_requirements,
            status=SessionStatus.IN_PROGRESS,
        )
        for requirement in model_with_requirements.requirements.all():
            AssessmentScoreFactory(
                session=session, competency=requirement.competency, level=Decimal("5.0")
            )

        profile = build_profile(employee)

        assert profile.coverage == 0.0
        assert profile.match_score == 0.0

    def test_профиль_можно_построить_по_чужой_модели(self, assessed_employee):
        """
        Так оценивается готовность к назначению: сотрудник сопоставляется с требованиями целевой
        должности, а не собственной.
        """
        target = CompetencyModelFactory(code="target-position", is_published=True)
        CompetencyRequirementFactory(
            competency_model=target,
            competency=CompetencyFactory(code="target-only"),
            target_level=5,
            weight=Decimal("1.000"),
        )

        profile = build_profile(assessed_employee, target)

        assert profile.competency_model == target
        assert profile.coverage == 0.0, "по чужим требованиям сотрудник не оценивался"

    def test_без_модели_профиль_пуст_но_не_ломается(self, branch, department):
        employee = EmployeeFactory(branch=branch, department=department, position=None)
        profile = build_profile(employee)

        assert profile.competency_model is None
        assert profile.gaps == ()
        assert profile.match_score == 0.0


class TestGapMatrix:
    """Матрица разрывов: свод по команде."""

    def test_столбец_показывает_долю_несоответствующих(
        self, branch, department, position, model_with_requirements
    ):
        """
        Неоценённый сотрудник входит в знаменатель: прятать его значило бы приукрашивать картину.
        """
        assessed = EmployeeFactory(branch=branch, department=department, position=position)
        session = AssessmentSessionFactory(
            employee=assessed,
            competency_model=model_with_requirements,
            status=SessionStatus.APPROVED,
        )
        for requirement in model_with_requirements.requirements.all():
            AssessmentScoreFactory(
                session=session,
                competency=requirement.competency,
                level=Decimal(str(requirement.target_level)),
            )
        unassessed = EmployeeFactory(branch=branch, department=department, position=position)

        matrix = build_gap_matrix([assessed, unassessed], model_with_requirements)
        columns = matrix.columns()

        assert all(column["failing"] == 1 for column in columns)
        assert all(column["failing_share"] == 50.0 for column in columns)

    def test_строки_упорядочены_по_соответствию(
        self, branch, department, position, model_with_requirements
    ):
        strong = EmployeeFactory(branch=branch, department=department, position=position)
        weak = EmployeeFactory(branch=branch, department=department, position=position)

        for employee, level in ((strong, "5.0"), (weak, "1.0")):
            session = AssessmentSessionFactory(
                employee=employee,
                competency_model=model_with_requirements,
                status=SessionStatus.APPROVED,
            )
            for requirement in model_with_requirements.requirements.all():
                AssessmentScoreFactory(
                    session=session, competency=requirement.competency, level=Decimal(level)
                )

        rows = build_gap_matrix([weak, strong], model_with_requirements).rows()

        assert rows[0]["employee"] == strong


# ---------------------------------------------------------------------------
# Страницы раздела
# ---------------------------------------------------------------------------
class TestPages:
    """Открытие страниц раздела."""

    @pytest.mark.parametrize(
        "url_name",
        ["competencies:index", "competencies:models", "competencies:my_profile"],
    )
    def test_страницы_открываются_сотруднику(self, auth_client, url_name):
        """
        Справочник доступен и рядовому сотруднику: он должен понимать, по каким основаниям его
        оценивают, иначе оценка воспринимается как произвол.
        """
        response = auth_client.get(reverse(url_name))
        assert response.status_code == 200

    def test_анонимный_посетитель_перенаправляется_на_вход(self, client):
        response = client.get(reverse("competencies:index"))
        assert response.status_code == 302
        assert "/account/" in response["Location"]

    def test_карточка_компетенции_показывает_шкалу(self, hr_client):
        competency = CompetencyFactory(code="page-competency")
        response = hr_client.get(competency.get_absolute_url())

        assert response.status_code == 200
        assert len(response.context["scale"]) == Competency.expected_scale_size()

    def test_матрица_без_выбранной_модели_подсказывает_а_не_падает(self, hr_client):
        response = hr_client.get(reverse("competencies:matrix"))

        assert response.status_code == 200
        assert response.context.get("matrix") is None

    def test_матрица_строится_по_выбранной_модели(
        self, hr_client, assessed_employee, model_with_requirements
    ):
        response = hr_client.get(
            reverse("competencies:matrix"),
            {"competency_model": model_with_requirements.pk},
        )

        assert response.status_code == 200
        assert response.context["matrix"] is not None
        assert len(response.context["rows"]) == 1


class TestProfileAccess:
    """Разграничение доступа к профилю компетенций сотрудника."""

    def test_кадровая_служба_видит_профиль_любого_сотрудника(self, hr_client, assessed_employee):
        response = hr_client.get(
            reverse("competencies:employee_profile", args=[assessed_employee.public_id])
        )
        assert response.status_code == 200

    def test_рядовой_сотрудник_чужой_профиль_не_видит(self, auth_client, assessed_employee):
        response = auth_client.get(
            reverse("competencies:employee_profile", args=[assessed_employee.public_id])
        )
        assert response.status_code == 403

    def test_руководитель_чужого_подразделения_профиль_не_видит(
        self, client, assessed_employee, other_branch, other_department
    ):
        """
        Область видимости проверяется независимо от роли: наличие роли руководителя не открывает
        доступ к чужому подразделению.
        """
        from core.constants import Role

        outsider = UserFactory(
            role=Role.MANAGER,
            scope_level=ScopeLevel.DEPARTMENT,
            branch=other_branch,
            department=other_department,
        )
        client.force_login(outsider)

        response = client.get(
            reverse("competencies:employee_profile", args=[assessed_employee.public_id])
        )
        assert response.status_code == 403

    def test_профиль_можно_сравнить_с_другой_моделью(self, hr_client, assessed_employee):
        target = CompetencyModelFactory(code="compare-target", is_published=True)
        CompetencyRequirementFactory(
            competency_model=target,
            competency=CompetencyFactory(code="compare-only"),
            target_level=5,
            weight=Decimal("1.000"),
        )

        response = hr_client.get(
            reverse("competencies:employee_profile", args=[assessed_employee.public_id]),
            {"model": target.code},
        )

        assert response.status_code == 200
        assert response.context["competency_model"] == target


# ---------------------------------------------------------------------------
# Отрисовка разметки
# ---------------------------------------------------------------------------
class TestRendering:
    """Проверки разметки, которые нельзя выполнить на уровне контекста."""

    def test_числа_в_атрибутах_диаграммы_записаны_точкой(self, hr_client, assessed_employee):
        """
        Русская локаль разделяет целую и дробную части запятой, и координата вида x="160,0" для SVG
        недопустима: браузер отбрасывает дробную часть либо весь атрибут, и подписи осей
        разъезжаются по диаграмме.
        """
        response = hr_client.get(
            reverse("competencies:employee_profile", args=[assessed_employee.public_id])
        )
        html = response.content.decode()

        # Диаграмма отыскивается по своему классу, а не как первый элемент svg на странице: значки
        # интерфейса выводятся встроенным SVG и встречаются в разметке раньше.
        start = html.index('class="radar__svg"')
        svg = html[start : html.index("</svg>", start)]
        numeric = re.findall(r'\b(?:x|y|x1|y1|x2|y2|cx|cy|r)="([^"]+)"', svg)

        assert numeric, "в диаграмме должны быть числовые атрибуты"
        assert not [value for value in numeric if "," in value], (
            "координаты SVG выведены с десятичной запятой"
        )

    def test_доли_в_стилях_записаны_точкой(self, hr_client, assessed_employee):
        """Правило `left: 80,0%` браузер отбрасывает целиком."""
        response = hr_client.get(
            reverse("competencies:employee_profile", args=[assessed_employee.public_id])
        )
        html = response.content.decode()

        styles = re.findall(r'style="([^"]*(?:left|height|width)[^"]*)"', html)
        assert not [value for value in styles if "," in value], (
            "доли в атрибуте style выведены с десятичной запятой"
        )

    @pytest.mark.parametrize(
        "url_name", ["competencies:index", "competencies:models", "competencies:matrix"]
    )
    def test_комментарии_шаблонов_не_попадают_на_страницу(self, hr_client, url_name):
        """
        Django не поддерживает многострочный комментарий `{# … #}`: закрывающая последовательность
        ищется в той же строке, и текст комментария выводится читателю как содержимое страницы.
        """
        html = hr_client.get(reverse(url_name)).content.decode()

        assert "{#" not in html
        assert "#}" not in html

    def test_шкала_разрыва_согласована_с_числом(self, hr_client, assessed_employee):
        """
        Полоса уровня и число рядом с ней описывают одно и то же. Расхождение между ними хуже
        отсутствия полосы: читатель верит картинке.
        """
        response = hr_client.get(
            reverse("competencies:employee_profile", args=[assessed_employee.public_id])
        )
        profile = response.context["profile"]

        for gap in profile.gaps:
            filled = sum(1 for step in gap.steps if step["state"] == "filled")
            if gap.actual is not None:
                assert filled == int(gap.actual), gap.competency.name
