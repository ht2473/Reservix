"""Проверка процедур оценки: кампании, сессии, опрос 360°, тестирование."""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

import pytest

from django.utils import timezone

from apps.assessment.models import (
    Assessment360Respondent,
    AssessmentCampaign,
    AssessmentMethod,
    AssessmentScore,
    AssessmentSession,
    AttemptStatus,
    CampaignStatus,
    QuestionKind,
    RespondentRelation,
    RespondentStatus,
    SessionStatus,
)

# Модели встроенного тестирования переименованы при импорте.
from apps.assessment.models import Test as Quiz
from apps.assessment.models import TestOption as QuizOption
from apps.assessment.models import TestQuestion as QuizQuestion
from apps.assessment.services import (
    AssessmentError,
    aggregate_360,
    attempt_questions,
    blind_spots,
    campaign_participants,
    campaign_progress,
    check_open_answer,
    close_campaign,
    finish_attempt,
    invite_360,
    launch_campaign,
    open_360_session,
    pending_manual_checks,
    publish_attempt,
    save_answer,
    save_scores,
    start_attempt,
    submit_session,
)
from apps.competencies.models import CompetencyModel
from tests.factories import UserFactory
from tests.factories.domain import (
    AssessmentCampaignFactory,
    AssessmentSessionFactory,
    CompetencyFactory,
    CompetencyModelFactory,
    CompetencyRequirementFactory,
    EmployeeFactory,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


# ---------------------------------------------------------------------------
# Фикстуры раздела
# ---------------------------------------------------------------------------
@pytest.fixture
def competency_model(db) -> CompetencyModel:
    """Модель из трёх требований с суммой весов, равной единице."""
    model = CompetencyModelFactory(code="model-under-test", is_published=True)
    for index, weight in enumerate(("0.500", "0.300", "0.200"), start=1):
        CompetencyRequirementFactory(
            competency_model=model,
            competency=CompetencyFactory(code=f"under-test-{index}"),
            target_level=4,
            weight=Decimal(weight),
            is_critical=index == 1,
        )
    return model


@pytest.fixture
def campaign(competency_model) -> AssessmentCampaign:
    """Кампания-черновик, готовая к запуску."""
    return AssessmentCampaignFactory(
        code="campaign-under-test",
        competency_model=competency_model,
        status=CampaignStatus.DRAFT,
    )


@pytest.fixture
def team(branch, department, position, employee_user, manager_user):
    """Руководитель и двое подчинённых в одном подразделении."""
    head = EmployeeFactory(
        branch=branch, department=department, position=position, user=manager_user
    )
    first = EmployeeFactory(
        branch=branch,
        department=department,
        position=position,
        manager=head,
        user=employee_user,
    )
    second = EmployeeFactory(
        branch=branch,
        department=department,
        position=position,
        manager=head,
        user=UserFactory(branch=branch, department=department),
    )
    return {"head": head, "first": first, "second": second}


# ---------------------------------------------------------------------------
# Состав участников
# ---------------------------------------------------------------------------
class TestParticipants:
    """Отбор участников кампании."""

    def test_кампания_подразделения_охватывает_вложенные(self, campaign, team, child_department):
        """Оценка отдела без входящих в него групп смысла не имеет."""
        nested = EmployeeFactory(department=child_department, branch=child_department.branch)
        campaign.department = child_department.parent
        campaign.save(update_fields=["department"])

        assert nested in campaign_participants(campaign)

    def test_сотрудник_в_длительном_отпуске_не_участвует(self, campaign, team):
        """
        Оценивать сотрудника, которого полгода не было на работе, не по чему, а незаполненная
        сессия по нему создаёт ложную тревогу в отчёте.
        """
        from apps.people.models import EmployeeStatus

        absent = team["second"]
        absent.status = EmployeeStatus.LEAVE
        absent.save(update_fields=["status"])

        assert absent not in campaign_participants(campaign)

    def test_внешний_кандидат_не_участвует(self, campaign, team, branch, department, position):
        """Внешнего кандидата некому оценивать: он не работает в организации."""
        external = EmployeeFactory(
            branch=branch, department=department, position=position, is_external=True
        )
        assert external not in campaign_participants(campaign)


# ---------------------------------------------------------------------------
# Запуск и завершение кампании
# ---------------------------------------------------------------------------
class TestCampaignLifecycle:
    """Жизненный цикл кампании оценки."""

    def test_запуск_создаёт_сессии_и_переводит_в_работу(self, campaign, team):
        result = launch_campaign(campaign)
        campaign.refresh_from_db()

        assert campaign.status == CampaignStatus.ACTIVE
        assert result["sessions"] > 0
        assert AssessmentSession.objects.filter(campaign=campaign).exists()

    def test_запуск_создаёт_самооценку_и_оценку_руководителя(self, campaign, team):
        launch_campaign(campaign)
        methods = set(
            AssessmentSession.objects.filter(campaign=campaign, employee=team["first"]).values_list(
                "method", flat=True
            )
        )
        assert AssessmentMethod.SELF in methods
        assert AssessmentMethod.MANAGER in methods

    def test_повторный_запуск_не_создаёт_дубликатов(self, campaign, team):
        """
        Сотрудника могли принять после старта кампании, и организатор должен иметь возможность
        включить его, не отменяя процедуру.
        """
        launch_campaign(campaign)
        before = AssessmentSession.objects.filter(campaign=campaign).count()

        second = launch_campaign(campaign)

        assert second["sessions"] == 0
        assert AssessmentSession.objects.filter(campaign=campaign).count() == before

    def test_повторный_запуск_добирает_новых_сотрудников(
        self, campaign, team, branch, department, position
    ):
        launch_campaign(campaign)
        before = AssessmentSession.objects.filter(campaign=campaign).count()

        newcomer = EmployeeFactory(
            branch=branch,
            department=department,
            position=position,
            manager=team["head"],
            user=UserFactory(branch=branch, department=department),
        )
        launch_campaign(campaign)

        assert AssessmentSession.objects.filter(campaign=campaign).count() > before
        assert AssessmentSession.objects.filter(campaign=campaign, employee=newcomer).exists()

    def test_завершённую_кампанию_нельзя_запустить(self, campaign, team):
        launch_campaign(campaign)
        close_campaign(campaign)

        with pytest.raises(AssessmentError):
            launch_campaign(campaign)

    def test_завершение_отменяет_незаполненные_сессии(self, campaign, team):
        """
        Кампания с вечно незакрытыми сессиями искажает показатель завершённости следующих процедур.
        """
        launch_campaign(campaign)
        result = close_campaign(campaign)
        campaign.refresh_from_db()

        assert campaign.status == CampaignStatus.CLOSED
        assert campaign.closed_at is not None
        assert result["cancelled_sessions"] > 0
        assert not AssessmentSession.objects.filter(
            campaign=campaign, status=SessionStatus.PLANNED
        ).exists()

    def test_ход_кампании_считается_по_состояниям(self, campaign, team, competency_model):
        launch_campaign(campaign)
        session = AssessmentSession.objects.filter(campaign=campaign).first()
        _fill(session, competency_model)
        submit_session(session)

        progress = campaign_progress(campaign)

        assert progress.total > 0
        assert progress.submitted == 1
        assert progress.done == 1
        assert 0 < progress.completion_rate <= 100


# ---------------------------------------------------------------------------
# Заполнение сессии
# ---------------------------------------------------------------------------
def _fill(session: AssessmentSession, model: CompetencyModel, level: str = "4.0") -> None:
    """Заполняет все требования модели одним уровнем."""
    save_scores(
        session,
        {requirement.competency_id: Decimal(level) for requirement in model.requirements.all()},
    )


class TestScoring:
    """Выставление баллов и внесение сессии."""

    def test_сохранение_переводит_сессию_в_заполнение(self, competency_model):
        session = AssessmentSessionFactory(
            competency_model=competency_model, status=SessionStatus.PLANNED
        )
        _fill(session, competency_model)
        session.refresh_from_db()

        assert session.status == SessionStatus.IN_PROGRESS
        assert session.scores.count() == 3

    def test_пустое_значение_удаляет_оценку_а_не_ставит_ноль(self, competency_model):
        """
        Ноль вне шкалы 1–5: попав в базу, он занизил бы средний уровень сотрудника на ровном месте.
        """
        session = AssessmentSessionFactory(
            competency_model=competency_model, status=SessionStatus.PLANNED
        )
        _fill(session, competency_model)
        first = competency_model.requirements.first()

        save_scores(session, {first.competency_id: None})

        assert not AssessmentScore.objects.filter(
            session=session, competency_id=first.competency_id
        ).exists()
        assert session.scores.count() == 2

    def test_оценка_вне_модели_отбрасывается(self, competency_model):
        """Состав формы задаёт сервер: лишнее поле означает подделанный запрос."""
        session = AssessmentSessionFactory(
            competency_model=competency_model, status=SessionStatus.PLANNED
        )
        stranger = CompetencyFactory(code="not-in-model")

        saved = save_scores(session, {stranger.pk: Decimal("5.0")})

        assert saved == 0
        assert not session.scores.exists()

    def test_частично_заполненную_сессию_внести_нельзя(self, competency_model):
        """
        Средний уровень по трети компетенций попадает в расчёт наравне с полной оценкой и искажает
        соответствие тем сильнее, чем меньше заполнено.
        """
        session = AssessmentSessionFactory(
            competency_model=competency_model, status=SessionStatus.PLANNED
        )
        first = competency_model.requirements.first()
        save_scores(session, {first.competency_id: Decimal("4.0")})

        with pytest.raises(AssessmentError):
            submit_session(session)

        session.refresh_from_db()
        assert session.status == SessionStatus.IN_PROGRESS

    def test_внесённую_сессию_изменить_нельзя(self, competency_model):
        session = AssessmentSessionFactory(
            competency_model=competency_model, status=SessionStatus.PLANNED
        )
        _fill(session, competency_model)
        submit_session(session)

        with pytest.raises(AssessmentError):
            _fill(session, competency_model, level="2.0")

    def test_балл_ноль_сто_вычисляется_из_уровня(self, competency_model):
        """
        Обе величины хранятся ради обратимости переноса из исходной базы, и расхождение между ними
        недопустимо.
        """
        session = AssessmentSessionFactory(
            competency_model=competency_model, status=SessionStatus.PLANNED
        )
        _fill(session, competency_model, level="5.0")

        score = session.scores.first()
        assert score.raw_score == Decimal("100.00")


# ---------------------------------------------------------------------------
# Оценка 360°
# ---------------------------------------------------------------------------
class TestSurvey360:
    """Опрос 360°: приглашения, анонимность, сведение."""

    def test_приглашения_создаются_по_организационной_структуре(self, campaign, team):
        """
        Возможность выбирать оценивающих превращает 360° в опрос друзей, поэтому состав
        определяется структурой, а не сотрудником.
        """
        invitations = invite_360(campaign, team["first"], rng=random.Random(1))
        relations = {item.relation for item in invitations}

        assert RespondentRelation.MANAGER in relations
        assert RespondentRelation.PEER in relations

    def test_повторное_приглашение_не_дублируется(self, campaign, team):
        invite_360(campaign, team["first"], rng=random.Random(1))
        second = invite_360(campaign, team["first"], rng=random.Random(1))

        assert second == []
        assert (
            Assessment360Respondent.objects.filter(
                campaign=campaign, employee=team["first"]
            ).count()
            > 0
        )

    def test_сессия_респондента_создаётся_при_первом_открытии(self, campaign, team):
        """
        Приглашённых больше, чем заполнивших: предварительное создание сессий дало бы кампании
        сотни пустых записей.
        """
        invitation = invite_360(campaign, team["first"], rng=random.Random(1))[0]
        assert invitation.session_id is None

        session = open_360_session(invitation)
        invitation.refresh_from_db()

        assert invitation.session_id == session.pk
        assert invitation.status == RespondentStatus.IN_PROGRESS
        assert session.method == AssessmentMethod.ASSESSMENT_360

    def test_повторное_открытие_возвращает_ту_же_сессию(self, campaign, team):
        invitation = invite_360(campaign, team["first"], rng=random.Random(1))[0]
        first = open_360_session(invitation)
        second = open_360_session(invitation)

        assert first.pk == second.pk

    def test_агрегат_скрывается_при_малом_числе_ответов(self, campaign, team, competency_model):
        """
        Два подчинённых легко узнают оценку друг друга вычитанием из среднего, и обещанная
        анонимность оказывается формальной.
        """
        invitations = invite_360(campaign, team["first"], rng=random.Random(1))
        peers = [item for item in invitations if item.relation == RespondentRelation.PEER]
        assert peers, "в подразделении должен быть хотя бы один коллега"

        session = open_360_session(peers[0])
        _fill(session, competency_model)
        submit_session(session)
        peers[0].refresh_from_db()

        aggregates = aggregate_360(campaign, team["first"])
        peer_aggregate = next(
            item for item in aggregates if item.relation == RespondentRelation.PEER
        )

        assert peer_aggregate.responses < Assessment360Respondent.MIN_RESPONSES_FOR_DISCLOSURE
        assert not peer_aggregate.is_disclosed
        assert peer_aggregate.by_competency == {}

    def test_самооценка_раскрывается_всегда(self, campaign, team, competency_model):
        """
        Скрывать от сотрудника его собственные ответы бессмысленно, а порог выключил бы самооценку
        навсегда: респондент там ровно один.
        """
        invitation = Assessment360Respondent.objects.create(
            campaign=campaign,
            employee=team["first"],
            respondent=team["first"].user,
            relation=RespondentRelation.SELF,
        )
        session = open_360_session(invitation)
        _fill(session, competency_model)
        submit_session(session)

        aggregates = aggregate_360(campaign, team["first"])
        own = next(item for item in aggregates if item.relation == RespondentRelation.SELF)

        assert own.is_disclosed

    def test_слепые_зоны_требуют_и_самооценки_и_внешних_ответов(self, campaign, team):
        """Без одной из сторон расхождение не определено."""
        assert blind_spots([]) == []


# ---------------------------------------------------------------------------
# Тестирование
# ---------------------------------------------------------------------------
@pytest.fixture
def quiz(db):
    """Тест из трёх заданий с известными правильными ответами."""
    competency = CompetencyFactory(code="tested-competency", is_testable=True)
    test = Quiz.objects.create(
        code="quiz-under-test",
        name="Проверочный тест",
        competency=competency,
        time_limit_minutes=10,
        questions_per_attempt=3,
        passing_score=60,
        max_attempts=2,
        shuffle_questions=False,
        shuffle_options=False,
    )
    for index in range(1, 4):
        question = QuizQuestion.objects.create(
            test=test,
            text=f"Задание {index}",
            kind=QuestionKind.SINGLE,
            weight=1,
            sort_order=index * 10,
        )
        QuizOption.objects.create(question=question, text="Верно", is_correct=True, sort_order=10)
        QuizOption.objects.create(question=question, text="Неверно", sort_order=20)
    return test


class TestTesting:
    """Встроенное тестирование: попытки, подсчёт, перенос в оценку."""

    def test_попытка_нумеруется_последовательно(self, quiz, team):
        first = start_attempt(quiz, team["first"])
        finish_attempt(first)
        second = start_attempt(quiz, team["first"])

        assert first.attempt_number == 1
        assert second.attempt_number == 2

    def test_незавершённая_попытка_возвращается_как_есть(self, quiz, team):
        """Иначе перезагрузка страницы обнуляла бы ответы и списывала попытку."""
        first = start_attempt(quiz, team["first"])
        second = start_attempt(quiz, team["first"])

        assert first.pk == second.pk

    def test_исчерпание_попыток_прерывает_запуск(self, quiz, team):
        for _ in range(quiz.max_attempts):
            finish_attempt(start_attempt(quiz, team["first"]))

        with pytest.raises(AssessmentError):
            start_attempt(quiz, team["first"])

    def test_состав_заданий_устойчив_внутри_попытки(self, quiz, team):
        """Обновление страницы не должно подменять вопросы: уже данные ответы потеряли бы смысл."""
        quiz.shuffle_questions = True
        quiz.save(update_fields=["shuffle_questions"])
        attempt = start_attempt(quiz, team["first"])

        first = [question.pk for question in attempt_questions(attempt)]
        second = [question.pk for question in attempt_questions(attempt)]

        assert first == second

    def test_верные_ответы_дают_полный_балл(self, quiz, team):
        attempt = start_attempt(quiz, team["first"])
        for question in attempt_questions(attempt):
            correct = question.options.filter(is_correct=True).first()
            save_answer(attempt, question, option_ids=[correct.pk])

        finish_attempt(attempt)
        attempt.refresh_from_db()

        assert attempt.score == Decimal("100.00")
        assert attempt.is_passed
        assert attempt.status == AttemptStatus.CHECKED

    def test_неверные_ответы_не_дают_баллов(self, quiz, team):
        attempt = start_attempt(quiz, team["first"])
        for question in attempt_questions(attempt):
            wrong = question.options.filter(is_correct=False).first()
            save_answer(attempt, question, option_ids=[wrong.pk])

        finish_attempt(attempt)
        attempt.refresh_from_db()

        assert attempt.score == Decimal("0.00")
        assert not attempt.is_passed

    def test_частичный_ответ_на_вопрос_с_несколькими_верными_не_засчитывается(self, quiz, team):
        """Иначе, выбрав все варианты, испытуемый получал бы часть балла за отсутствие знания."""
        question = QuizQuestion.objects.create(
            test=quiz,
            text="Отметьте все верные утверждения",
            kind=QuestionKind.MULTIPLE,
            weight=1,
            sort_order=40,
        )
        first = QuizOption.objects.create(question=question, text="A", is_correct=True)
        QuizOption.objects.create(question=question, text="B", is_correct=True)
        QuizOption.objects.create(question=question, text="C")

        attempt = start_attempt(quiz, team["first"])
        answer = save_answer(attempt, question, option_ids=[first.pk])

        assert not answer.is_correct
        assert answer.awarded_score == Decimal("0")

    def test_ответ_на_завершённую_попытку_не_принимается(self, quiz, team):
        attempt = start_attempt(quiz, team["first"])
        question = attempt_questions(attempt)[0]
        finish_attempt(attempt)

        with pytest.raises(AssessmentError):
            save_answer(attempt, question, option_ids=[])

    def test_истёкшая_попытка_завершается_с_подсчётом(self, quiz, team):
        """Засчитывается всё сохранённое, а не теряется целиком."""
        attempt = start_attempt(quiz, team["first"])
        question = attempt_questions(attempt)[0]
        correct = question.options.filter(is_correct=True).first()
        save_answer(attempt, question, option_ids=[correct.pk])

        attempt.expires_at = timezone.now() - dt.timedelta(minutes=1)
        attempt.save(update_fields=["expires_at"])
        finish_attempt(attempt, expired=True)
        attempt.refresh_from_db()

        assert attempt.status == AttemptStatus.EXPIRED
        assert attempt.score > 0

    def test_результат_переносится_в_оценку_компетенции(self, quiz, team, competency_model):
        """
        Здесь замыкается смысл встроенного тестирования: балл вычислен системой, и оценивающий на
        него повлиять не может.
        """
        employee = team["first"]
        employee.position.competency_models.add(competency_model)
        competency_model.requirements.create(
            competency=quiz.competency, target_level=4, weight=Decimal("0.000")
        )

        attempt = start_attempt(quiz, employee)
        for question in attempt_questions(attempt):
            correct = question.options.filter(is_correct=True).first()
            save_answer(attempt, question, option_ids=[correct.pk])
        finish_attempt(attempt)

        session = publish_attempt(attempt)

        assert session.method == AssessmentMethod.TEST
        assert session.assessor_id is None, "балл вычислен системой, а не человеком"
        assert session.status == SessionStatus.SUBMITTED
        score = session.scores.get(competency=quiz.competency)
        assert score.raw_score == attempt.score

    def test_незавершённую_попытку_перенести_нельзя(self, quiz, team):
        attempt = start_attempt(quiz, team["first"])

        with pytest.raises(AssessmentError):
            publish_attempt(attempt)


class TestExpertCheck:
    """Экспертная проверка развёрнутых ответов."""

    @pytest.fixture
    def attempt(self, quiz, team):
        """Завершённая попытка: три верных ответа с выбором и один развёрнутый."""
        QuizQuestion.objects.create(
            test=quiz, text="Опишите подход", kind=QuestionKind.OPEN, weight=2, sort_order=40
        )
        quiz.questions_per_attempt = 4
        quiz.save(update_fields=["questions_per_attempt"])
        attempt = start_attempt(quiz, team["first"])
        for question in attempt_questions(attempt):
            if question.kind == QuestionKind.OPEN:
                save_answer(attempt, question, text="Сначала уточню требования к результату.")
            else:
                save_answer(attempt, question, option_ids=question.correct_option_ids)
        return finish_attempt(attempt)

    def test_непроверенный_ответ_в_подсчёт_не_входит(self, attempt):
        assert float(attempt.score) == 100.0

    def test_ответ_попадает_в_очередь_проверки(self, attempt, hr_user):
        answer = attempt.answers.get(question__kind=QuestionKind.OPEN)

        assert answer in pending_manual_checks(hr_user)

    def test_экспертный_балл_пересчитывает_попытку(self, attempt, hr_user):
        """Три балла из трёх за выбор и один из двух за развёрнутый ответ — 80 из 100."""
        answer = attempt.answers.get(question__kind=QuestionKind.OPEN)

        check_open_answer(answer, score=Decimal("1"), actor=hr_user)
        attempt.refresh_from_db()

        assert float(attempt.score) == 80.0
        assert answer not in pending_manual_checks(hr_user)

    def test_балл_выше_веса_вопроса_отклоняется(self, attempt, hr_user):
        answer = attempt.answers.get(question__kind=QuestionKind.OPEN)

        with pytest.raises(AssessmentError):
            check_open_answer(answer, score=Decimal("3"), actor=hr_user)
