"""Проверка сценариев ролей: кнопки действий, область видимости, уведомления."""

from __future__ import annotations

from decimal import Decimal

import pytest

from django.urls import reverse

from apps.assessment.models import AssessmentMethod, CampaignStatus, SessionStatus
from apps.assessment.services import approve_session, launch_campaign
from apps.development.services import plans_for
from apps.notifications.models import Notification, NotificationKind
from apps.reserve.forms import NominationForm
from apps.reserve.models import NominationStatus
from apps.reserve.services import pending_for_user
from core.constants import Role, ScopeLevel
from tests.factories import UserFactory
from tests.factories.domain import (
    AssessmentCampaignFactory,
    AssessmentSessionFactory,
    CompetencyFactory,
    CompetencyModelFactory,
    CompetencyRequirementFactory,
    DevelopmentPlanFactory,
    EmployeeFactory,
    ReserveNominationFactory,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def competency_model(db):
    model = CompetencyModelFactory(code="scenario-model", is_published=True)
    CompetencyRequirementFactory(
        competency_model=model,
        competency=CompetencyFactory(code="scenario-competency"),
        target_level=3,
        weight=Decimal("1.000"),
    )
    return model


@pytest.fixture
def team(branch, department, position, manager_user, employee_user):
    """Руководитель подразделения и подчинённый с учётной записью."""
    head = EmployeeFactory(
        branch=branch, department=department, position=position, user=manager_user
    )
    member = EmployeeFactory(
        branch=branch, department=department, position=position, manager=head, user=employee_user
    )
    return {"head": head, "member": member}


@pytest.fixture
def branch_hr(other_branch):
    """Кадровик филиала: та же роль, что у головного офиса, но видимость — филиал."""
    return UserFactory(role=Role.HR, scope_level=ScopeLevel.BRANCH, branch=other_branch)


class TestPageActions:
    """Кнопки действий в заголовке рабочего места."""

    def test_кадровая_служба_видит_кнопку_утверждения(
        self, hr_client, team, competency_model, manager_user
    ):
        session = AssessmentSessionFactory(
            employee=team["member"],
            assessor=manager_user,
            competency_model=competency_model,
            method=AssessmentMethod.MANAGER,
            status=SessionStatus.SUBMITTED,
        )

        page = hr_client.get(session.get_absolute_url()).content.decode()

        assert reverse("assessment:session_approve", args=[session.public_id]) in page

    def test_черновик_кампании_можно_запустить(self, hr_client, competency_model):
        campaign = AssessmentCampaignFactory(
            competency_model=competency_model, status=CampaignStatus.DRAFT
        )

        page = hr_client.get(campaign.get_absolute_url()).content.decode()

        assert reverse("assessment:campaign_launch", args=[campaign.public_id]) in page

    def test_список_кампаний_ведёт_к_созданию(self, hr_client):
        page = hr_client.get(reverse("assessment:list")).content.decode()

        assert reverse("assessment:campaign_create") in page


class TestBranchScope:
    """Кадровая служба филиала ограничена своим филиалом."""

    def test_карточка_сотрудника_другого_филиала_закрыта(self, client, branch_hr, team):
        client.force_login(branch_hr)

        response = client.get(reverse("people:employee_detail", args=[team["member"].public_id]))

        assert response.status_code == 403

    def test_планы_другого_филиала_не_видны(self, branch_hr, team, other_branch, position):
        local = EmployeeFactory(branch=other_branch, position=position)
        own_plan = DevelopmentPlanFactory(employee=local)
        foreign_plan = DevelopmentPlanFactory(employee=team["member"])

        visible = set(plans_for(branch_hr))

        assert own_plan in visible
        assert foreign_plan not in visible

    def test_очередь_заявок_ограничена_филиалом(self, branch_hr, team, position):
        nomination = ReserveNominationFactory(
            employee=team["member"], target_position=position, status=NominationStatus.HR_REVIEW
        )

        assert nomination not in pending_for_user(branch_hr)


class TestNominationForm:
    """Список кандидатов в форме выдвижения."""

    def test_руководитель_видит_только_своих_сотрудников(
        self, manager_user, team, other_department, position
    ):
        outsider = EmployeeFactory(department=other_department, position=position)

        options = str(NominationForm(user=manager_user)["employee"])

        assert f'value="{team["member"].pk}"' in options
        assert f'value="{outsider.pk}"' not in options
        assert f'value="{team["head"].pk}"' not in options

    def test_собственная_заявка_не_попадает_к_себе_на_согласование(
        self, manager_user, team, position
    ):
        nomination = ReserveNominationFactory(
            employee=team["head"], target_position=position, status=NominationStatus.MANAGER_REVIEW
        )

        assert nomination not in pending_for_user(manager_user)


class TestAssessmentNotifications:
    """Уведомления процедуры оценки."""

    def test_запуск_кампании_извещает_оценивающих(self, team, competency_model, manager_user):
        campaign = AssessmentCampaignFactory(
            competency_model=competency_model,
            status=CampaignStatus.DRAFT,
            department=team["head"].department,
            is_360_enabled=False,
        )

        launch_campaign(campaign)

        assert Notification.objects.filter(
            recipient=manager_user, kind=NotificationKind.ASSESSMENT_REQUESTED
        ).exists()

    def test_утверждение_извещает_оцениваемого(
        self, team, competency_model, manager_user, hr_user, employee_user
    ):
        session = AssessmentSessionFactory(
            employee=team["member"],
            assessor=manager_user,
            competency_model=competency_model,
            method=AssessmentMethod.MANAGER,
            status=SessionStatus.SUBMITTED,
        )

        approve_session(session, actor=hr_user)

        assert Notification.objects.filter(
            recipient=employee_user, kind=NotificationKind.ASSESSMENT_COMPLETED
        ).exists()
