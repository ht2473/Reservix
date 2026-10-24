"""Проверка панели администрирования."""

from __future__ import annotations

import pytest

from django.urls import reverse

from apps.accounts.models import User
from apps.portal.models import Feedback
from tests.factories import FeedbackFactory
from tests.factories.domain import (
    CompetencyFactory,
    CompetencyModelFactory,
    EmployeeFactory,
    ReserveEntryFactory,
    ScoringSchemeFactory,
)

pytestmark = pytest.mark.django_db

#: Девять разделов панели администрирования.
ADMIN_PAGES = [
    "administration:index",
    "administration:users",
    "administration:org",
    "administration:references",
    "administration:scoring",
    "administration:audit",
    "administration:data",
    "administration:content",
    "administration:feedback",
]


class TestAccessControl:
    """Разграничение доступа к разделу администрирования."""

    @pytest.mark.parametrize("route", ADMIN_PAGES)
    def test_admin_opens_every_page(self, admin_client, route):
        """Администратор открывает все страницы панели."""
        assert admin_client.get(reverse(route)).status_code == 200

    @pytest.mark.parametrize("route", ADMIN_PAGES)
    def test_hr_is_refused(self, hr_client, route):
        """HR-специалист в панель администрирования не допускается."""
        assert hr_client.get(reverse(route)).status_code == 403

    @pytest.mark.parametrize("route", ADMIN_PAGES)
    def test_employee_is_refused(self, auth_client, route):
        """Рядовой сотрудник в панель администрирования не допускается."""
        assert auth_client.get(reverse(route)).status_code == 403

    @pytest.mark.parametrize("route", ADMIN_PAGES)
    def test_anonymous_is_redirected_to_login(self, client, route):
        """Неавторизованный посетитель перенаправляется на форму входа."""
        response = client.get(reverse(route))
        assert response.status_code == 302
        assert "/account/" in response.url


class TestAdminIndex:
    """Сводка состояния системы."""

    def test_attention_list_counts_incomplete_references(self, admin_client, position):
        """Сводка показывает справочники с пробелами."""
        CompetencyFactory(scale=False)
        response = admin_client.get(reverse("administration:index"))

        labels = {str(item["label"]): item["count"] for item in response.context["attention"]}
        assert labels["Компетенции с неполной шкалой"] == 1
        assert labels["Должности без профиля требований"] >= 1

    def test_counters_reflect_stored_data(self, admin_client):
        """Показатели сводки соответствуют содержимому базы."""
        EmployeeFactory()
        ReserveEntryFactory()

        response = admin_client.get(reverse("administration:index"))

        assert response.context["employee_total"] >= 1
        assert response.context["reserve_total"] >= 1


class TestUserManagement:
    """Работа с учётными записями."""

    def test_list_shows_role_and_scope(self, admin_client, manager_user):
        """В списке видны и роль, и область видимости: это разные измерения."""
        response = admin_client.get(reverse("administration:users"))
        content = response.content.decode()

        assert manager_user.email in content
        assert str(manager_user.get_scope_level_display()) in content

    def test_search_filters_list(self, admin_client):
        """Поиск отбирает записи по фамилии."""
        from tests.factories import UserFactory

        UserFactory(last_name="Одинцов", email="odintsov@meridian-soft.ru")
        response = admin_client.get(reverse("administration:users"), {"q": "Одинц"})

        assert len(response.context["users"]) == 1

    def test_role_filter(self, admin_client, hr_user, manager_user):
        """Отбор по роли оставляет только записи выбранной роли."""
        response = admin_client.get(reverse("administration:users"), {"role": "manager"})
        roles = {item.role for item in response.context["users"]}
        assert roles == {"manager"}

    def test_created_account_cannot_log_in_until_password_is_set(
        self, admin_client, branch, department
    ):
        """Созданная запись получает непригодный для входа пароль."""
        response = admin_client.post(
            reverse("administration:user_create"),
            {
                "email": "novikov@meridian-soft.ru",
                "last_name": "Новиков",
                "first_name": "Артём",
                "middle_name": "Сергеевич",
                "phone": "",
                "role": "employee",
                "scope_level": "self",
                "branch": branch.pk,
                "department": department.pk,
                "is_active": "on",
            },
        )

        assert response.status_code == 302
        created = User.objects.get(email="novikov@meridian-soft.ru")
        assert created.has_usable_password() is False
        assert created.must_change_password is True

    def test_form_rejects_head_without_department(self, admin_client, branch):
        """Признак руководителя без подразделения не принимается формой."""
        response = admin_client.post(
            reverse("administration:user_create"),
            {
                "email": "head@meridian-soft.ru",
                "last_name": "Гордеев",
                "first_name": "Илья",
                "role": "manager",
                "scope_level": "subordinates",
                "branch": branch.pk,
                "department": "",
                "is_department_head": "on",
                "is_active": "on",
            },
        )

        assert response.status_code == 200
        assert "department" in response.context["form"].errors

    def test_toggle_blocks_account(self, admin_client, employee_user):
        """Действие блокировки снимает признак активности."""
        admin_client.post(reverse("administration:user_toggle", args=[employee_user.pk]))
        employee_user.refresh_from_db()
        assert employee_user.is_active is False

    def test_admin_cannot_block_own_account(self, admin_client, admin_user):
        """Собственную запись заблокировать нельзя."""
        admin_client.post(reverse("administration:user_toggle", args=[admin_user.pk]))
        admin_user.refresh_from_db()
        assert admin_user.is_active is True


class TestOrgStructure:
    """Страница организационной структуры."""

    def test_shows_plan_and_fact(self, admin_client, position):
        """Штатное расписание показано рядом с фактической численностью."""
        EmployeeFactory(position=position, department=position.department)
        response = admin_client.get(reverse("administration:org"))

        assert response.context["headcount_plan"] >= position.headcount_plan
        assert response.context["headcount_fact"] >= 1

    def test_department_detail_lists_staff(self, admin_client, department):
        """Карточка подразделения показывает его сотрудников."""
        employee = EmployeeFactory(department=department, branch=department.branch)
        response = admin_client.get(
            reverse("administration:department_detail", args=[department.pk])
        )

        assert employee.short_name in response.content.decode()

    def test_position_detail_shows_reserve(self, admin_client, position):
        """Карточка должности показывает резервистов на неё."""
        entry = ReserveEntryFactory(target_position=position)
        response = admin_client.get(reverse("administration:position_detail", args=[position.pk]))

        assert entry.employee.short_name in response.content.decode()


class TestReferencesAndScoring:
    """Справочники и модель скоринга."""

    def test_incomplete_competency_is_highlighted(self, admin_client):
        """Компетенция с неполной шкалой попадает в перечень пробелов."""
        CompetencyFactory(scale=False)
        response = admin_client.get(reverse("administration:references"))

        assert len(response.context["competencies_incomplete"]) == 1

    def test_unbalanced_model_is_highlighted(self, admin_client):
        """Модель с суммой весов, отличной от единицы, отмечается."""
        from decimal import Decimal

        from tests.factories.domain import CompetencyRequirementFactory

        model = CompetencyModelFactory()
        CompetencyRequirementFactory(competency_model=model, weight=Decimal("0.300"))

        response = admin_client.get(reverse("administration:references"))
        assert model in response.context["models_unbalanced"]

    def test_scoring_page_shows_thresholds(self, admin_client):
        """Страница расчёта показывает пороговые значения."""
        ScoringSchemeFactory()
        response = admin_client.get(reverse("administration:scoring"))

        assert len(response.context["settings_values"]) >= 5
        assert len(response.context["schemes"]) == 1


class TestSystemData:
    """Раздел накопленных данных."""

    def test_separates_imported_records(self, admin_client):
        """Перенесённые записи учитываются отдельно."""
        EmployeeFactory(legacy_id=1)
        EmployeeFactory(legacy_id=None)

        response = admin_client.get(reverse("administration:data"))

        assert response.context["total_records"] >= 2
        assert response.context["legacy_records"] >= 1


class TestFeedbackModeration:
    """Обработка обращений с публичной части."""

    def test_list_filters_by_status(self, admin_client):
        """Отбор по состоянию оставляет только новые обращения."""
        FeedbackFactory(status=Feedback.Status.NEW)
        FeedbackFactory(status=Feedback.Status.CLOSED)

        response = admin_client.get(reverse("administration:feedback"), {"status": "new"})
        assert len(response.context["items"]) == 1

    def test_answer_requires_text(self, admin_client):
        """Перевод обращения в состояние «отвечено» требует текста ответа."""
        feedback = FeedbackFactory()
        response = admin_client.post(
            reverse("administration:feedback_detail", args=[feedback.pk]),
            {"status": Feedback.Status.ANSWERED, "answer": "", "internal_note": ""},
        )

        assert response.status_code == 200
        assert "answer" in response.context["form"].errors

    def test_answer_records_author_and_time(self, admin_client, admin_user):
        """Ответ фиксирует автора и момент — след решения сохраняется."""
        feedback = FeedbackFactory()
        admin_client.post(
            reverse("administration:feedback_detail", args=[feedback.pk]),
            {
                "status": Feedback.Status.ANSWERED,
                "answer": "Раздел плана развития доступен в личном кабинете сотрудника.",
                "internal_note": "",
            },
        )

        feedback.refresh_from_db()
        assert feedback.status == Feedback.Status.ANSWERED
        assert feedback.answered_by == admin_user
        assert feedback.answered_at is not None
