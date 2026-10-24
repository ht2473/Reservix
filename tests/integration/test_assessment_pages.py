"""Проверка страниц раздела оценки и разграничения доступа."""

from __future__ import annotations

from decimal import Decimal

import pytest

from django.urls import reverse

from apps.assessment.models import (
    Assessment360Respondent,
    AssessmentMethod,
    CampaignStatus,
    RespondentRelation,
    RespondentStatus,
    SessionStatus,
)
from apps.assessment.services import save_scores, submit_session
from core.constants import Role, ScopeLevel
from tests.factories import UserFactory
from tests.factories.domain import (
    AssessmentCampaignFactory,
    AssessmentScoreFactory,
    AssessmentSessionFactory,
    CompetencyFactory,
    CompetencyModelFactory,
    CompetencyRequirementFactory,
    EmployeeFactory,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def competency_model(db):
    """Модель из двух требований."""
    model = CompetencyModelFactory(code="pages-model", is_published=True)
    for index, weight in enumerate(("0.600", "0.400"), start=1):
        CompetencyRequirementFactory(
            competency_model=model,
            competency=CompetencyFactory(code=f"pages-competency-{index}"),
            target_level=4,
            weight=Decimal(weight),
        )
    return model


@pytest.fixture
def campaign(competency_model):
    return AssessmentCampaignFactory(
        code="pages-campaign", competency_model=competency_model, status=CampaignStatus.DRAFT
    )


@pytest.fixture
def session(branch, department, position, competency_model, manager_user):
    """Запланированная сессия, назначенная руководителю."""
    employee = EmployeeFactory(branch=branch, department=department, position=position)
    return AssessmentSessionFactory(
        employee=employee,
        assessor=manager_user,
        competency_model=competency_model,
        status=SessionStatus.PLANNED,
        method=AssessmentMethod.MANAGER,
    )


def _payload(competency_model, level: str = "4.0", comment: str = "") -> dict:
    """Данные формы выставления баллов."""
    data: dict[str, str] = {"summary": ""}
    for requirement in competency_model.requirements.all():
        data[f"level_{requirement.competency_id}"] = level
        data[f"comment_{requirement.competency_id}"] = comment
    return data


# ---------------------------------------------------------------------------
# Доступ к разделу
# ---------------------------------------------------------------------------
class TestAccess:
    """Разграничение доступа по ролям."""

    @pytest.mark.parametrize(
        "url_name", ["assessment:list", "assessment:sessions", "assessment:campaign_create"]
    )
    def test_разделы_организатора_закрыты_от_сотрудника(self, auth_client, url_name):
        assert auth_client.get(reverse(url_name)).status_code == 403

    @pytest.mark.parametrize(
        "url_name", ["assessment:inbox", "assessment:my_results", "assessment:tests"]
    )
    def test_рабочие_страницы_доступны_сотруднику(self, auth_client, url_name):
        assert auth_client.get(reverse(url_name)).status_code == 200

    def test_анонимный_посетитель_перенаправляется_на_вход(self, client):
        response = client.get(reverse("assessment:inbox"))
        assert response.status_code == 302
        assert "/account/" in response["Location"]

    def test_чужая_сессия_недоступна_постороннему(self, auth_client, session):
        assert auth_client.get(session.get_absolute_url()).status_code == 403

    def test_оценивающий_видит_собственную_сессию(self, manager_client, session):
        assert manager_client.get(session.get_absolute_url()).status_code == 200

    def test_сессия_триста_шестьдесят_скрыта_от_оцениваемого(
        self, client, branch, department, position, competency_model, employee_user
    ):
        """
        Персональный ответ респондента раскрыл бы его авторство, и обещанная анонимность
        превратилась бы в формальность.
        """
        employee = EmployeeFactory(
            branch=branch, department=department, position=position, user=employee_user
        )
        session = AssessmentSessionFactory(
            employee=employee,
            assessor=UserFactory(branch=branch, department=department),
            competency_model=competency_model,
            method=AssessmentMethod.ASSESSMENT_360,
            status=SessionStatus.APPROVED,
        )
        client.force_login(employee_user)

        assert client.get(session.get_absolute_url()).status_code == 403

    def test_обычная_сессия_видна_оцениваемому(
        self, client, branch, department, position, competency_model, employee_user
    ):
        """Свою оценку руководителя сотрудник видеть должен."""
        employee = EmployeeFactory(
            branch=branch, department=department, position=position, user=employee_user
        )
        session = AssessmentSessionFactory(
            employee=employee,
            assessor=UserFactory(branch=branch, department=department),
            competency_model=competency_model,
            method=AssessmentMethod.MANAGER,
            status=SessionStatus.APPROVED,
        )
        client.force_login(employee_user)

        assert client.get(session.get_absolute_url()).status_code == 200


# ---------------------------------------------------------------------------
# Действия, изменяющие состояние
# ---------------------------------------------------------------------------
class TestStateChangingActions:
    """Изменение состояния выполняется только методом POST."""

    def test_переход_по_ссылке_кампанию_не_запускает(self, hr_client, campaign):
        """
        Иначе предпросмотр ссылки почтовым клиентом запускал бы процедуру на четыреста человек.
        """
        response = hr_client.get(reverse("assessment:campaign_launch", args=[campaign.public_id]))
        campaign.refresh_from_db()

        assert response.status_code == 302
        assert campaign.status == CampaignStatus.DRAFT

    def test_запуск_методом_post_создаёт_сессии(
        self, hr_client, campaign, branch, department, position
    ):
        EmployeeFactory(
            branch=branch,
            department=department,
            position=position,
            user=UserFactory(branch=branch, department=department),
        )
        response = hr_client.post(reverse("assessment:campaign_launch", args=[campaign.public_id]))
        campaign.refresh_from_db()

        assert response.status_code == 302
        assert campaign.status == CampaignStatus.ACTIVE
        assert campaign.sessions.exists()

    def test_сотрудник_кампанию_не_запускает(self, auth_client, campaign):
        assert (
            auth_client.post(
                reverse("assessment:campaign_launch", args=[campaign.public_id])
            ).status_code
            == 403
        )

    def test_завершение_кампании_доступно_кадровой_службе(self, hr_client, campaign):
        campaign.status = CampaignStatus.ACTIVE
        campaign.save(update_fields=["status"])

        hr_client.post(reverse("assessment:campaign_close", args=[campaign.public_id]))
        campaign.refresh_from_db()

        assert campaign.status == CampaignStatus.CLOSED


# ---------------------------------------------------------------------------
# Форма выставления баллов
# ---------------------------------------------------------------------------
class TestScoreForm:
    """Заполнение сессии оценки через интерфейс."""

    def test_форма_содержит_поле_на_каждое_требование(
        self, manager_client, session, competency_model
    ):
        response = manager_client.get(reverse("assessment:session_fill", args=[session.public_id]))

        assert response.status_code == 200
        assert len(response.context["rows"]) == competency_model.requirements.count()

    def test_форма_показывает_поведенческие_индикаторы(self, manager_client, session):
        """
        Оценивающий, которому нужно открыть другую страницу, чтобы вспомнить разницу между уровнями
        3 и 4, её не откроет.
        """
        response = manager_client.get(reverse("assessment:session_fill", args=[session.public_id]))
        first = response.context["rows"][0]

        assert len(first["scale"]) > 0
        assert all(level.indicators for level in first["scale"])

    def test_сохранение_черновика_не_вносит_сессию(self, manager_client, session, competency_model):
        manager_client.post(
            reverse("assessment:session_fill", args=[session.public_id]),
            {**_payload(competency_model), "save": "1"},
        )
        session.refresh_from_db()

        assert session.status == SessionStatus.IN_PROGRESS
        assert session.scores.count() == competency_model.requirements.count()

    def test_внесение_закрывает_сессию_для_правки(self, manager_client, session, competency_model):
        manager_client.post(
            reverse("assessment:session_fill", args=[session.public_id]),
            {**_payload(competency_model), "submit": "1"},
        )
        session.refresh_from_db()

        assert session.status == SessionStatus.SUBMITTED
        assert not session.is_editable

    def test_существенное_расхождение_требует_обоснования(
        self, manager_client, session, competency_model
    ):
        """
        Обязательное обоснование каждой оценки приводит к двадцати «соответствует» подряд, поэтому
        оно требуется избирательно — там, где оценка расходится с целевым уровнем.
        """
        response = manager_client.post(
            reverse("assessment:session_fill", args=[session.public_id]),
            {**_payload(competency_model, level="1.0"), "submit": "1"},
        )
        session.refresh_from_db()

        assert response.status_code == 200
        assert response.context["form"].errors
        assert session.status == SessionStatus.PLANNED

    def test_расхождение_с_обоснованием_принимается(
        self, manager_client, session, competency_model
    ):
        response = manager_client.post(
            reverse("assessment:session_fill", args=[session.public_id]),
            {
                **_payload(
                    competency_model,
                    level="1.0",
                    comment="Задачи уровня должности самостоятельно не выполняет",
                ),
                "submit": "1",
            },
        )
        session.refresh_from_db()

        assert response.status_code == 302
        assert session.status == SessionStatus.SUBMITTED

    def test_чужую_сессию_заполнить_нельзя(self, auth_client, session):
        assert (
            auth_client.get(
                reverse("assessment:session_fill", args=[session.public_id])
            ).status_code
            == 403
        )


class TestApproval:
    """Утверждение и возврат сессии кадровой службой."""

    @pytest.fixture
    def submitted(self, session, competency_model):
        save_scores(
            session,
            {
                requirement.competency_id: Decimal("4.0")
                for requirement in competency_model.requirements.all()
            },
        )
        submit_session(session)
        return session

    def test_утверждение_переводит_сессию_в_расчёт(self, hr_client, submitted):
        hr_client.post(reverse("assessment:session_approve", args=[submitted.public_id]))
        submitted.refresh_from_db()

        assert submitted.status == SessionStatus.APPROVED
        assert submitted.is_counted

    def test_возврат_требует_содержательной_причины(self, hr_client, submitted):
        hr_client.post(
            reverse("assessment:session_reject", args=[submitted.public_id]),
            {"reason": "нет"},
        )
        submitted.refresh_from_db()

        assert submitted.status == SessionStatus.SUBMITTED, "короткая причина отклоняется"

    def test_возврат_с_причиной_открывает_сессию_для_правки(self, hr_client, submitted):
        hr_client.post(
            reverse("assessment:session_reject", args=[submitted.public_id]),
            {"reason": "Оценки не подтверждены наблюдаемыми результатами работы"},
        )
        submitted.refresh_from_db()

        assert submitted.status == SessionStatus.REJECTED
        assert submitted.rejection_reason

    def test_руководитель_собственную_оценку_не_утверждает(self, manager_client, submitted):
        """Иначе проверка кадровой службой теряет смысл."""
        assert (
            manager_client.post(
                reverse("assessment:session_approve", args=[submitted.public_id])
            ).status_code
            == 403
        )


# ---------------------------------------------------------------------------
# Опрос 360°
# ---------------------------------------------------------------------------
class TestSurveyAccess:
    """Доступ к приглашениям и сводным результатам опроса 360°."""

    @pytest.fixture
    def invitation(self, campaign, branch, department, position, employee_user):
        campaign.status = CampaignStatus.ACTIVE
        campaign.is_360_enabled = True
        campaign.save(update_fields=["status", "is_360_enabled"])
        subject = EmployeeFactory(branch=branch, department=department, position=position)
        return Assessment360Respondent.objects.create(
            campaign=campaign,
            employee=subject,
            respondent=employee_user,
            relation=RespondentRelation.PEER,
        )

    def test_приглашение_открывает_форму_респондента(self, auth_client, invitation):
        response = auth_client.get(
            reverse("assessment:respond_360", args=[invitation.access_token])
        )
        invitation.refresh_from_db()

        assert response.status_code == 302
        assert invitation.session_id is not None
        assert invitation.status == RespondentStatus.IN_PROGRESS

    def test_чужое_приглашение_недоступно(self, manager_client, invitation):
        """
        Код приглашения не заменяет проверку личности: иначе анкету можно было бы заполнить от
        чужого имени.
        """
        assert (
            manager_client.get(
                reverse("assessment:respond_360", args=[invitation.access_token])
            ).status_code
            == 403
        )

    def test_отказ_фиксируется_явно(self, auth_client, invitation):
        """
        Организатору важно отличать «не успел» от «не считаю возможным оценивать»: во втором случае
        нужен другой респондент.
        """
        auth_client.post(reverse("assessment:decline_360", args=[invitation.access_token]))
        invitation.refresh_from_db()

        assert invitation.status == RespondentStatus.DECLINED

    def test_сводные_результаты_доступны_кадровой_службе(self, hr_client, invitation):
        response = hr_client.get(
            reverse(
                "assessment:survey_360",
                args=[invitation.campaign.public_id, invitation.employee.public_id],
            )
        )
        assert response.status_code == 200

    def test_сводные_результаты_закрыты_от_рядового_сотрудника(self, auth_client, invitation):
        response = auth_client.get(
            reverse(
                "assessment:survey_360",
                args=[invitation.campaign.public_id, invitation.employee.public_id],
            )
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Результаты оцениваемого
# ---------------------------------------------------------------------------
class TestMyResults:
    """Страница собственных результатов."""

    def test_сведённые_уровни_показываются_с_достоверностью(
        self, client, branch, department, position, competency_model, employee_user
    ):
        employee = EmployeeFactory(
            branch=branch, department=department, position=position, user=employee_user
        )
        session = AssessmentSessionFactory(
            employee=employee,
            competency_model=competency_model,
            status=SessionStatus.APPROVED,
        )
        for requirement in competency_model.requirements.all():
            AssessmentScoreFactory(
                session=session, competency=requirement.competency, level=Decimal("4.0")
            )
        client.force_login(employee_user)

        response = client.get(reverse("assessment:my_results"))
        rows = response.context["rows"]

        assert response.status_code == 200
        assert len(rows) == competency_model.requirements.count()
        assert all("confidence" in row for row in rows)

    def test_без_карточки_страница_объясняет_причину(self, client):
        """Пустой экран без пояснения выглядит как неисправность."""
        user = UserFactory(role=Role.EMPLOYEE, scope_level=ScopeLevel.SELF)
        client.force_login(user)

        response = client.get(reverse("assessment:my_results"))

        assert response.status_code == 200
        assert response.context["employee"] is None
