"""Проверка страниц кадрового резерва, разграничения доступа и сквозного пути."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from django.urls import reverse
from django.utils import timezone

from apps.reserve.models import (
    NominationKind,
    NominationStatus,
    ReserveEntry,
    ReserveStatus,
)
from core.constants import Role, ScopeLevel
from tests.factories import PositionFactory, UserFactory
from tests.factories.domain import (
    EmployeeFactory,
    ReserveEntryFactory,
    ReserveGroupFactory,
    ReserveNominationFactory,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def reserve_entry(department, position):
    """Действующая запись резерва в подразделении по умолчанию."""
    return ReserveEntryFactory(
        employee=EmployeeFactory(department=department, hire_date=dt.date(2020, 3, 1)),
        target_position=position,
        readiness_index=Decimal("71.00"),
        rank=1,
    )


@pytest.fixture
def nomination(department, position, manager_user):
    """Заявка, ожидающая решения руководителя."""
    return ReserveNominationFactory(
        employee=EmployeeFactory(department=department, hire_date=dt.date(2019, 5, 1)),
        target_position=position,
        nominated_by=manager_user,
        status=NominationStatus.MANAGER_REVIEW,
    )


# ---------------------------------------------------------------------------
# Доступность страниц
# ---------------------------------------------------------------------------
class TestPageAccess:
    """Кто какие страницы раздела открывает."""

    @pytest.mark.parametrize(
        "url_name",
        ["reserve:list", "reserve:reviews", "reserve:candidates", "reserve:nominations"],
    )
    def test_hr_opens_curator_pages(self, hr_client, url_name):
        """Кадровая служба ведёт резерв целиком."""
        response = hr_client.get(reverse(url_name))

        assert response.status_code == 200

    @pytest.mark.parametrize("url_name", ["reserve:list", "reserve:reviews", "reserve:candidates"])
    def test_manager_denied_curator_pages(self, manager_client, url_name):
        """Руководителю закрыт список резерва организации."""
        response = manager_client.get(reverse(url_name))

        assert response.status_code == 403

    def test_manager_opens_team_pages(self, manager_client):
        """Рабочее место руководителя открыто ему самому."""
        assert manager_client.get(reverse("reserve:team")).status_code == 200
        assert manager_client.get(reverse("reserve:approvals")).status_code == 200

    def test_employee_denied_reserve_section(self, auth_client):
        """Рядовому сотруднику раздел ведения резерва недоступен."""
        assert auth_client.get(reverse("reserve:list")).status_code == 403
        assert auth_client.get(reverse("reserve:team")).status_code == 403

    def test_employee_opens_own_application(self, client, department):
        """Страница самовыдвижения открыта сотруднику с карточкой."""
        user = UserFactory(role=Role.EMPLOYEE, scope_level=ScopeLevel.SELF, department=department)
        EmployeeFactory(user=user, department=department)
        client.force_login(user)

        assert client.get(reverse("reserve:apply")).status_code == 200

    def test_application_needs_employee_card(self, auth_client):
        """Учётная запись без карточки сотрудника заявку не подаёт."""
        assert auth_client.get(reverse("reserve:apply")).status_code == 403

    def test_anonymous_is_redirected(self, client):
        """Раздел закрыт от неавторизованного посетителя."""
        response = client.get(reverse("reserve:list"))

        assert response.status_code == 302
        assert "/account/login" in response["Location"]

    def test_entry_detail_shows_bench(self, hr_client, reserve_entry, department, position):
        """Карточка показывает конкурентов на ту же должность."""
        ReserveEntryFactory(
            employee=EmployeeFactory(department=department, last_name="Конкурентов"),
            target_position=position,
            readiness_index=Decimal("83.00"),
        )

        response = hr_client.get(reserve_entry.get_absolute_url())

        assert response.status_code == 200
        assert len(response.context["bench"]) == 2

    def test_entry_hidden_outside_scope(self, manager_client, other_branch, grade):
        """Запись сотрудника вне области видимости не открывается."""
        outsider = EmployeeFactory(branch=other_branch, department=None)
        entry = ReserveEntryFactory(
            employee=outsider,
            target_position=PositionFactory(name="Роль другого филиала", grade=grade),
        )

        response = manager_client.get(entry.get_absolute_url())

        assert response.status_code == 403

    def test_group_pages_open(self, hr_client):
        """Программы подготовки доступны кадровой службе."""
        group = ReserveGroupFactory(code="pages-group", name="Оперативный резерв")

        assert hr_client.get(reverse("reserve:groups")).status_code == 200
        assert hr_client.get(group.get_absolute_url()).status_code == 200


# ---------------------------------------------------------------------------
# Отбор и подбор
# ---------------------------------------------------------------------------
class TestFiltering:
    """Отбор записей и подбор кандидатов."""

    def test_scope_filter_matches_status(self, hr_client, department, position, grade):
        """Отбор «состоящие в резерве» исключает закрытые записи."""
        ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=position,
        )
        ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=PositionFactory(name="Закрытая роль", grade=grade),
            status=ReserveStatus.EXCLUDED,
        )

        response = hr_client.get(reverse("reserve:list"), {"scope": "active"})

        assert response.status_code == 200
        assert len(response.context["entries"]) == 1

    def test_review_queue_lists_overdue(self, hr_client, department, position):
        """В очередь пересмотра попадают записи с прошедшим сроком."""
        ReserveEntryFactory(
            employee=EmployeeFactory(department=department),
            target_position=position,
            review_date=timezone.localdate() - dt.timedelta(days=30),
        )

        response = hr_client.get(reverse("reserve:reviews"))

        assert response.status_code == 200
        assert len(response.context["entries"]) == 1

    def test_candidate_search_requires_position(self, hr_client):
        """Подбор без должности не выполняется."""
        response = hr_client.get(reverse("reserve:candidates"))

        assert response.status_code == 200
        assert "candidates" not in response.context


# ---------------------------------------------------------------------------
# Защита действий
# ---------------------------------------------------------------------------
class TestActionSafety:
    """Действия, изменяющие состояние, защищены от перехода по ссылке."""

    def test_status_change_needs_post(self, hr_client, reserve_entry):
        """GET открывает форму, но состояния не меняет."""
        response = hr_client.get(reverse("reserve:entry_status", args=[reserve_entry.public_id]))

        reserve_entry.refresh_from_db()
        assert response.status_code == 200
        assert reserve_entry.status == ReserveStatus.ACTIVE

    def test_readiness_refresh_ignores_get(self, hr_client, reserve_entry):
        """Пересчёт индекса по переходу по ссылке не выполняется."""
        response = hr_client.get(reverse("reserve:entry_readiness", args=[reserve_entry.public_id]))

        assert response.status_code == 302

    def test_status_change_requires_reason(self, hr_client, reserve_entry):
        """Переход без основания не принимается: история решений обязательна."""
        response = hr_client.post(
            reverse("reserve:entry_status", args=[reserve_entry.public_id]),
            {"status": ReserveStatus.EXCLUDED, "reason": "нет"},
        )

        reserve_entry.refresh_from_db()
        assert response.status_code == 200
        assert reserve_entry.status == ReserveStatus.ACTIVE

    def test_status_change_records_history(self, hr_client, reserve_entry):
        """Переход с основанием применяется и сохраняется в истории."""
        response = hr_client.post(
            reverse("reserve:entry_status", args=[reserve_entry.public_id]),
            {
                "status": ReserveStatus.EXCLUDED,
                "reason": "Сотрудник отказался от подготовки к роли",
            },
        )

        reserve_entry.refresh_from_db()
        assert response.status_code == 302
        assert reserve_entry.status == ReserveStatus.EXCLUDED
        assert reserve_entry.status_history.count() == 1

    def test_manager_cannot_change_status(self, manager_client, reserve_entry):
        """Состав резерва ведёт кадровая служба, а не руководитель."""
        response = manager_client.post(
            reverse("reserve:entry_status", args=[reserve_entry.public_id]),
            {"status": ReserveStatus.INACTIVE, "reason": "Приостановить подготовку"},
        )

        reserve_entry.refresh_from_db()
        assert response.status_code == 403
        assert reserve_entry.status == ReserveStatus.ACTIVE

    def test_outside_manager_cannot_decide(self, client, nomination, other_department):
        """Руководитель чужого подразделения решения по заявке не принимает."""
        outsider = UserFactory(
            role=Role.MANAGER,
            scope_level=ScopeLevel.SUBORDINATES,
            department=other_department,
            is_department_head=True,
        )
        client.force_login(outsider)

        response = client.post(
            reverse("reserve:nomination_decide", args=[nomination.public_id]),
            {"decision": "approve", "comment": "Согласовано"},
        )

        nomination.refresh_from_db()
        assert response.status_code == 403
        assert nomination.status == NominationStatus.MANAGER_REVIEW

    def test_hr_may_unblock_manager_stage(self, hr_client, nomination, hr_user):
        """Кадровая служба вправе закрыть застрявшую ступень руководителя."""
        response = hr_client.post(
            reverse("reserve:nomination_decide", args=[nomination.public_id]),
            {"decision": "approve", "comment": "Руководитель в отпуске, согласовано"},
        )

        nomination.refresh_from_db()
        assert response.status_code == 302
        assert nomination.status == NominationStatus.HR_REVIEW
        assert nomination.manager_reviewer_id == hr_user.pk


# ---------------------------------------------------------------------------
# Сквозной путь
# ---------------------------------------------------------------------------
class TestNominationFlow:
    """Полный маршрут: подача, согласование, зачисление."""

    def test_manager_approval_moves_to_hr(self, manager_client, nomination):
        """Согласие руководителя переводит заявку к кадровой службе."""
        response = manager_client.post(
            reverse("reserve:nomination_decide", args=[nomination.public_id]),
            {"decision": "approve", "comment": "Готов отпустить, замена есть"},
        )

        nomination.refresh_from_db()
        assert response.status_code == 302
        assert nomination.status == NominationStatus.HR_REVIEW

    def test_rejection_requires_comment(self, manager_client, nomination):
        """Отказ без объяснения кандидату непонятен и не принимается."""
        response = manager_client.post(
            reverse("reserve:nomination_decide", args=[nomination.public_id]),
            {"decision": "reject", "comment": ""},
            follow=True,
        )

        nomination.refresh_from_db()
        assert response.status_code == 200
        assert nomination.status == NominationStatus.MANAGER_REVIEW

    def test_full_route_creates_entry(self, manager_client, hr_client, nomination):
        """Заявка проходит обе ступени и превращается в запись резерва."""
        manager_client.post(
            reverse("reserve:nomination_decide", args=[nomination.public_id]),
            {"decision": "approve", "comment": "Кандидатура согласована"},
        )
        group = ReserveGroupFactory(code="flow-group")

        response = hr_client.post(
            reverse("reserve:nomination_decide", args=[nomination.public_id]),
            {
                "decision": "approve",
                "comment": "Зачислен в оперативный резерв",
                "reserve_group": group.pk,
                "review_months": 24,
            },
        )

        nomination.refresh_from_db()
        assert response.status_code == 302
        assert nomination.status == NominationStatus.APPROVED
        assert nomination.entry is not None

        entry = nomination.entry
        assert entry.status == ReserveStatus.CANDIDATE
        assert entry.reserve_group_id == group.pk
        assert entry.memberships.filter(left_at__isnull=True).count() == 1
        assert entry.status_history.count() == 1

    def test_self_application_creates_nomination(self, client, department, position):
        """Сотрудник подаёт заявку на себя со страницы самовыдвижения."""
        user = UserFactory(role=Role.EMPLOYEE, scope_level=ScopeLevel.SELF, department=department)
        EmployeeFactory(user=user, department=department, hire_date=dt.date(2018, 1, 10))
        client.force_login(user)

        response = client.post(
            reverse("reserve:apply"),
            {
                "target_position": position.pk,
                "justification": (
                    "Полтора года веду направление самостоятельно, дважды замещал "
                    "руководителя на время отпуска и закрыл план развития"
                ),
                "achievements": "Запустил регламент приёмки заявок",
            },
        )

        assert response.status_code == 302
        nomination = user.employee_card.nominations.get()
        assert nomination.kind == NominationKind.SELF
        assert nomination.status == NominationStatus.MANAGER_REVIEW

    def test_short_justification_is_refused(self, client, department, position):
        """Формальная отписка вместо обоснования не принимается."""
        user = UserFactory(role=Role.EMPLOYEE, scope_level=ScopeLevel.SELF, department=department)
        EmployeeFactory(user=user, department=department, hire_date=dt.date(2018, 1, 10))
        client.force_login(user)

        response = client.post(
            reverse("reserve:apply"),
            {"target_position": position.pk, "justification": "Хочу"},
        )

        assert response.status_code == 200
        assert not ReserveEntry.objects.exists()
        assert not user.employee_card.nominations.exists()
