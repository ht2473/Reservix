"""Проверка раздела развития: планы, мероприятия, подтверждения, наставничество."""

from __future__ import annotations

import datetime as dt

import pytest

from django.urls import reverse
from django.utils import timezone

from apps.development.models import (
    ActivityStatus,
    ActivityType,
    EvidenceKind,
    MentorshipStatus,
    PlanStatus,
)
from apps.development.services import (
    DevelopmentError,
    add_evidence,
    approve_plan,
    complete_activity,
    complete_plan,
    create_plan,
    development_summary,
    plans_for,
    start_mentorship,
    submit_plan,
    verify_evidence,
)
from tests.factories.domain import (
    DevelopmentActivityFactory,
    DevelopmentPlanFactory,
    EmployeeFactory,
    LearningResourceFactory,
    MentorshipFactory,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def employee(department):
    """Сотрудник подразделения, доступного руководителю."""
    return EmployeeFactory(department=department)


@pytest.fixture
def plan(employee):
    """Действующий план развития с двумя мероприятиями."""
    plan = DevelopmentPlanFactory(employee=employee, status=PlanStatus.ACTIVE)
    DevelopmentActivityFactory(plan=plan, weight=3, activity_type=ActivityType.TASK)
    DevelopmentActivityFactory(plan=plan, weight=1, activity_type=ActivityType.COURSE)
    return plan


# ---------------------------------------------------------------------------
# Доступность страниц
# ---------------------------------------------------------------------------
class TestPageAccess:
    """Разграничение доступа к разделу."""

    @pytest.mark.parametrize(
        "url_name",
        [
            "development:list",
            "development:activities",
            "development:mentorship",
            "development:resources",
        ],
    )
    def test_страницы_раздела_открываются_кадровой_службе(self, hr_client, url_name):
        response = hr_client.get(reverse(url_name))
        assert response.status_code == 200

    def test_рядовой_сотрудник_не_видит_общий_список_планов(self, auth_client):
        """
        Список планов организации — рабочий инструмент кадровой службы, а не сотрудника: он
        раскрывает состав резерва целиком.
        """
        response = auth_client.get(reverse("development:list"))
        assert response.status_code == 403

    def test_библиотека_открыта_любому_вошедшему(self, auth_client):
        """
        Каталог обучения сведений о людях не содержит, и закрывать его от сотрудника, который
        подбирает материал сам, бессмысленно.
        """
        response = auth_client.get(reverse("development:resources"))
        assert response.status_code == 200

    def test_страницы_наставника_доступны_наставнику(self, mentor_client):
        for url_name in (
            "development:mentees",
            "development:mentee_plans",
            "development:mentor_feedback",
        ):
            response = mentor_client.get(reverse(url_name))
            assert response.status_code == 200, url_name

    def test_личный_план_открывается_сотрудником(self, auth_client):
        response = auth_client.get(reverse("development:my_plan"))
        assert response.status_code == 200

    def test_анонимный_посетитель_перенаправляется_на_вход(self, client):
        response = client.get(reverse("development:list"))
        assert response.status_code == 302
        assert reverse("accounts:login") in response.url


class TestScope:
    """Область видимости определяет состав планов."""

    def test_руководитель_видит_планы_своего_подразделения(
        self, manager_user, plan, other_department
    ):
        DevelopmentPlanFactory(employee=EmployeeFactory(department=other_department))

        visible = list(plans_for(manager_user))
        assert plan in visible
        assert all(item.employee.department_id == plan.employee.department_id for item in visible)

    def test_кадровая_служба_видит_все_планы(self, hr_user, plan, other_department):
        DevelopmentPlanFactory(employee=EmployeeFactory(department=other_department))
        assert plans_for(hr_user).count() == 2

    def test_наставник_видит_планы_подопечных(self, mentor_user, plan):
        """
        Пара «наставник — подопечный» пересекает организационные границы: наставник закреплён за
        человеком, а не за подразделением.
        """
        assert plans_for(mentor_user).count() == 0

        MentorshipFactory(mentor=mentor_user, mentee=plan.employee)
        assert list(plans_for(mentor_user)) == [plan]


# ---------------------------------------------------------------------------
# Жизненный цикл плана
# ---------------------------------------------------------------------------
class TestPlanLifecycle:
    """Черновик — согласование — действие — завершение."""

    def test_второй_незакрытый_план_не_создаётся(self, employee, hr_user, plan):
        """
        Два действующих плана означают два перечня приоритетов, и сотрудник обоснованно выполняет
        тот, который легче.
        """
        with pytest.raises(DevelopmentError, match="уже есть незакрытый план"):
            create_plan(employee, actor=hr_user)

    def test_план_без_мероприятий_не_уходит_на_утверждение(self, employee, hr_user):
        draft = create_plan(employee, actor=hr_user)
        with pytest.raises(DevelopmentError, match="нет мероприятий"):
            submit_plan(draft, actor=hr_user)

    def test_утверждение_вводит_план_в_действие(self, employee, hr_user, manager_user):
        draft = create_plan(employee, actor=hr_user, supervisor=manager_user)
        DevelopmentActivityFactory(plan=draft)

        submit_plan(draft, actor=hr_user)
        assert draft.status == PlanStatus.ON_APPROVAL

        approve_plan(draft, actor=manager_user)
        draft.refresh_from_db()
        assert draft.status == PlanStatus.ACTIVE
        assert draft.approved_by == manager_user
        assert draft.approved_at is not None

    def test_завершение_требует_итога(self, plan, hr_user):
        """
        План, закрытый без вывода о достигнутом, не отвечает на вопрос, ради которого составлялся.
        """
        with pytest.raises(DevelopmentError, match="итог"):
            complete_plan(plan, actor=hr_user, summary="   ")

    def test_действия_над_планом_доступны_через_интерфейс(self, hr_client, employee, hr_user):
        draft = create_plan(employee, actor=hr_user)
        DevelopmentActivityFactory(plan=draft)

        response = hr_client.post(
            reverse("development:plan_action", args=[draft.public_id, "submit"])
        )
        assert response.status_code == 302
        draft.refresh_from_db()
        assert draft.status == PlanStatus.ON_APPROVAL

    def test_действие_запрещено_постороннему_руководителю(
        self, manager_client, other_department, hr_user
    ):
        foreign = DevelopmentPlanFactory(employee=EmployeeFactory(department=other_department))
        response = manager_client.post(
            reverse("development:plan_action", args=[foreign.public_id, "approve"])
        )
        assert response.status_code == 404


class TestCompletionRate:
    """Выполнение считается по весам мероприятий."""

    def test_вес_мероприятия_влияет_на_процент(self, plan):
        """
        Пять пройденных вебинаров не закрывают план, в котором есть незавершённая стажировка: вклад
        видов развития различен.
        """
        heavy, light = plan.activities.order_by("-weight")
        light.status = ActivityStatus.COMPLETED
        light.completion_date = timezone.localdate()
        light.save()

        assert plan.recalculate_completion() == 25

        heavy.status = ActivityStatus.COMPLETED
        heavy.completion_date = timezone.localdate()
        heavy.save()
        assert plan.recalculate_completion() == 100


# ---------------------------------------------------------------------------
# Мероприятия и подтверждения
# ---------------------------------------------------------------------------
class TestActivityCompletion:
    """Мероприятие закрывается документом, а не сменой состояния."""

    def test_обучение_без_подтверждения_не_закрывается(self, plan, hr_user):
        activity = plan.activities.get(activity_type=ActivityType.COURSE)
        with pytest.raises(DevelopmentError, match="подтверждение"):
            complete_activity(activity, actor=hr_user)

    def test_обучение_с_подтверждением_закрывается(self, plan, hr_user):
        activity = plan.activities.get(activity_type=ActivityType.COURSE)
        add_evidence(
            activity,
            actor=hr_user,
            kind=EvidenceKind.CERTIFICATE,
            title="Сертификат",
            external_url="https://learning.example.org/1",
        )
        complete_activity(activity, actor=hr_user, result="Применил в работе")

        activity.refresh_from_db()
        assert activity.status == ActivityStatus.COMPLETED
        assert activity.completion_date is not None

    def test_развивающая_задача_закрывается_без_документа(self, plan, hr_user):
        """
        Естественная форма подтверждения работы на месте — отзыв руководителя, и требовать
        сертификат здесь было бы формализмом.
        """
        activity = plan.activities.get(activity_type=ActivityType.TASK)
        complete_activity(activity, actor=hr_user, result="Взял направление на сопровождение")
        assert activity.status == ActivityStatus.COMPLETED

    def test_подтверждение_без_содержимого_отклоняется(self, plan, hr_user):
        activity = plan.activities.get(activity_type=ActivityType.TASK)
        with pytest.raises(DevelopmentError, match="файл или укажите ссылку"):
            add_evidence(activity, actor=hr_user, title="Пустое подтверждение")

    def test_приложивший_не_проверяет_собственное_подтверждение(self, plan, hr_user):
        """
        Без этого правила проверка вырождается в самоподтверждение.
        """
        activity = plan.activities.get(activity_type=ActivityType.TASK)
        evidence = add_evidence(
            activity, actor=hr_user, title="Отчёт", external_url="https://example.org/report"
        )
        with pytest.raises(DevelopmentError, match="не тот, кто его приложил"):
            verify_evidence(evidence, actor=hr_user)

    def test_наставник_проверяет_подтверждение(self, plan, hr_user, mentor_user):
        activity = plan.activities.get(activity_type=ActivityType.TASK)
        evidence = add_evidence(
            activity, actor=hr_user, title="Отчёт", external_url="https://example.org/report"
        )
        verify_evidence(evidence, actor=mentor_user)

        evidence.refresh_from_db()
        assert evidence.is_verified is True
        assert evidence.verified_by == mentor_user


class TestOverdue:
    """Просрочка считается по дате, а не по хранимому состоянию."""

    def test_сводка_учитывает_просроченное_до_запуска_фоновой_задачи(self, hr_user, plan):
        """
        Состояние `overdue` проставляется задачей раз в сутки; до её запуска просроченное
        мероприятие числилось бы выполняемым.
        """
        activity = plan.activities.first()
        activity.due_date = timezone.localdate() - dt.timedelta(days=5)
        activity.status = ActivityStatus.PENDING
        activity.save()

        summary = development_summary(user=hr_user)
        assert summary.overdue_activities == 1


# ---------------------------------------------------------------------------
# Наставничество
# ---------------------------------------------------------------------------
class TestMentorship:
    """Пара с целью, сроком и результатом."""

    def test_пара_создаётся_и_видна_наставнику(self, mentor_client, mentor_user, employee, hr_user):
        pair = start_mentorship(
            mentor=mentor_user,
            mentee=employee,
            goal="Подготовка к руководству командой",
            actor=hr_user,
        )
        assert pair.status == MentorshipStatus.ACTIVE

        response = mentor_client.get(reverse("development:mentees"))
        assert response.status_code == 200
        assert employee.last_name in response.content.decode()

    def test_повторная_пара_не_создаётся(self, mentor_user, employee, hr_user):
        start_mentorship(mentor=mentor_user, mentee=employee, goal="Цель", actor=hr_user)
        with pytest.raises(DevelopmentError, match="уже существует"):
            start_mentorship(mentor=mentor_user, mentee=employee, goal="Цель", actor=hr_user)

    def test_человек_не_становится_наставником_самому_себе(self, employee_user, hr_user):
        employee = EmployeeFactory(user=employee_user)
        with pytest.raises(DevelopmentError, match="одним человеком"):
            start_mentorship(mentor=employee_user, mentee=employee, goal="Цель", actor=hr_user)

    def test_встреча_отмечается_наставником(self, mentor_client, mentor_user, employee, hr_user):
        pair = start_mentorship(
            mentor=mentor_user, mentee=employee, goal="Цель пары", actor=hr_user
        )
        response = mentor_client.post(
            reverse("development:mentorship_meeting", args=[pair.public_id]),
            {"meeting_date": timezone.localdate().isoformat(), "note": "Разобрали делегирование"},
        )
        assert response.status_code == 302

        pair.refresh_from_db()
        assert pair.meetings_count == 1
        assert pair.last_meeting_at == timezone.localdate()

    def test_встреча_будущей_датой_не_принимается(
        self, mentor_client, mentor_user, employee, hr_user
    ):
        """
        Отметка о том, чего ещё не было, сбрасывает счётчик напоминаний и скрывает пару, с которой
        на деле никто не работал.
        """
        pair = start_mentorship(
            mentor=mentor_user, mentee=employee, goal="Цель пары", actor=hr_user
        )
        tomorrow = timezone.localdate() + dt.timedelta(days=1)
        mentor_client.post(
            reverse("development:mentorship_meeting", args=[pair.public_id]),
            {"meeting_date": tomorrow.isoformat()},
        )

        pair.refresh_from_db()
        assert pair.meetings_count == 0


# ---------------------------------------------------------------------------
# Библиотека развития
# ---------------------------------------------------------------------------
class TestLibrary:
    """Материалы связаны с компетенциями и уровнями."""

    def test_карточка_ресурса_открывается(self, auth_client):
        resource = LearningResourceFactory()
        response = auth_client.get(reverse("development:resource_detail", args=[resource.pk]))
        assert response.status_code == 200
        assert resource.name in response.content.decode()

    def test_отбор_по_формату_сужает_список(self, hr_client):
        LearningResourceFactory(resource_format="course", name="Онлайн-курс по управлению")
        LearningResourceFactory(resource_format="book", name="Книга о делегировании")

        response = hr_client.get(reverse("development:resources"), {"resource_format": "book"})
        body = response.content.decode()
        assert "Книга о делегировании" in body
        assert "Онлайн-курс по управлению" not in body
