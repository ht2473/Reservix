"""
Наполнение процедур оценки: кампании, сессии, баллы, опрос 360°, тесты.

Запуск::

    uv run python manage.py seed_assessment
    uv run python manage.py seed_assessment --seed 42
    uv run python manage.py seed_assessment --flush
"""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
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
    RespondentStatus,
    SessionStatus,
    Test,
    TestAnswer,
    TestAttempt,
    TestOption,
    TestQuestion,
)
from apps.assessment.question_bank import TESTS
from apps.assessment.services import invite_360, launch_campaign
from apps.competencies.models import Competency, CompetencyModel
from apps.people.models import Employee
from core.settings_types import get_reservix_settings

#: Значение генератора случайных чисел по умолчанию — то же, что в наполнении
#: кадрового состава: демонстрационный стенд должен воспроизводиться целиком.
DEFAULT_SEED = 20260828

#: Разброс личной силы сотрудника в единицах шкалы компетенций.
TALENT_SPREAD = 0.55

#: Разброс отклонения по отдельной компетенции.
COMPETENCY_SPREAD = 0.45

#: Надбавка к личной силе для сотрудников, отмеченных ключевыми.
KEY_TALENT_BONUS = 0.45

#: Систематический недобор организации до целевых уровней, единиц шкалы.
TARGET_SHORTFALL = 0.4

#: Смещение оценки по методам. Положительное значение — метод завышает.
METHOD_BIAS: dict[str, float] = {
    AssessmentMethod.SELF: 0.42,
    AssessmentMethod.MANAGER: -0.12,
    AssessmentMethod.ASSESSMENT_360: 0.05,
    AssessmentMethod.EXPERT: -0.05,
}

#: Разброс ошибки метода: насколько мнение конкретного оценивающего
#: отклоняется от действительного уровня.
METHOD_NOISE: dict[str, float] = {
    AssessmentMethod.SELF: 0.5,
    AssessmentMethod.MANAGER: 0.35,
    AssessmentMethod.ASSESSMENT_360: 0.55,
    AssessmentMethod.EXPERT: 0.4,
}

#: Доля сессий прошедшей кампании, оставшихся незаполненными.
CLOSED_CAMPAIGN_MISS_RATE = 0.06

#: Доля заполненных сессий идущей кампании.
ACTIVE_CAMPAIGN_FILL_RATE = 0.55

#: Доля сотрудников, включаемых в опрос 360°. Полный охват не нужен: опрос
#: трудоёмок и проводится для руководителей и резервистов.
SURVEY_360_SHARE = 0.18

#: Доля приглашённых респондентов, заполнивших анкету.
RESPONSE_RATE_360 = 0.72

#: Число попыток тестирования, порождаемых для демонстрации.
TEST_ATTEMPTS = 90


class Command(BaseCommand):
    """Наполняет систему процедурами оценки и их результатами."""

    help = "Создаёт кампании оценки, сессии, баллы, опросы 360° и тесты"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
        parser.add_argument(
            "--flush",
            action="store_true",
            help="удалить порождённые кампании, сохранив перенесённые из дампа",
        )
        parser.add_argument(
            "--skip-tests",
            action="store_true",
            help="не создавать банк тестов и попытки прохождения",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        """Наполняет процедуры оценки целиком в одной транзакции."""
        self.random = random.Random(options["seed"])  # noqa: S311  # nosec B311
        self.today = timezone.localdate()
        scale = get_reservix_settings()
        self.scale_min = scale["COMPETENCY_SCALE_MIN"]
        self.scale_max = scale["COMPETENCY_SCALE_MAX"]

        if options["flush"]:
            self._flush()

        self._load_employees()

        # Три кампании дают все состояния, которые встречаются в работе.
        closed = self._create_campaign(
            code="annual-2025",
            name=f"Годовая оценка {self.today.year - 1}",
            year=self.today.year - 1,
        )
        active = self._create_campaign(
            code="annual-2026",
            name=f"Годовая оценка {self.today.year}",
            year=self.today.year,
        )
        survey_campaign = self._create_campaign(
            code="reserve-review-2026",
            name="Оценка управленческого потенциала",
            year=self.today.year,
            is_360=True,
        )

        launched_closed = launch_campaign(closed)
        launched_active = launch_campaign(active)

        filled_closed = self._fill_campaign(
            closed, fill_rate=1 - CLOSED_CAMPAIGN_MISS_RATE, approve=True
        )
        filled_active = self._fill_campaign(
            active, fill_rate=ACTIVE_CAMPAIGN_FILL_RATE, approve=False
        )
        survey = self._run_360(survey_campaign)

        tests = attempts = 0
        if not options["skip_tests"]:
            tests = self._create_tests()
            attempts = self._run_attempts()

        self.stdout.write(self.style.SUCCESS("\nПроцедуры оценки наполнены."))
        self.stdout.write(
            f"  Кампаний: 3\n"
            f"  Сессий завершённой кампании: {launched_closed['sessions']}"
            f" (заполнено {filled_closed})\n"
            f"  Сессий идущей кампании: {launched_active['sessions']}"
            f" (заполнено {filled_active})\n"
            f"  Приглашений 360°: {survey['invited']}"
            f" (ответили {survey['completed']})\n"
            f"  Тестов: {tests}, попыток: {attempts}\n"
            f"  Всего оценок в системе: {AssessmentScore.objects.count()}"
        )

    # -- Подготовка ---------------------------------------------------------
    def _flush(self) -> None:
        """Удаляет порождённые кампании."""
        campaigns = AssessmentCampaign.objects.filter(
            code__in=["annual-2025", "annual-2026", "reserve-review-2026"]
        )
        sessions = AssessmentSession.objects.filter(campaign__in=campaigns)
        AssessmentScore.objects.filter(session__in=sessions).delete()
        Assessment360Respondent.objects.filter(campaign__in=campaigns).delete()
        removed = sessions.count()
        sessions.delete()
        campaigns.delete()

        TestAnswer.objects.all().delete()
        TestAttempt.objects.all().delete()

        if removed:
            self.stdout.write(self.style.WARNING(f"Удалено сессий: {removed}"))

    def _load_employees(self) -> None:
        """Готовит справочные данные и скрытые уровни сотрудников."""
        self.employees = list(
            Employee.objects.active().internal().select_related("position", "position__grade")
        )
        self.talent: dict[int, float] = {}
        for employee in self.employees:
            base = self.random.gauss(0, TALENT_SPREAD)
            if employee.is_key_talent:
                base += KEY_TALENT_BONUS
            self.talent[employee.pk] = base

        self.competency_noise: dict[tuple[int, int], float] = {}

        # Модель по умолчанию нужна кампании и тем сотрудникам, должность которых модели не имеет.
        default_model = (
            CompetencyModel.objects.published().filter(code="tier-specialist").first()
            or CompetencyModel.objects.published().first()
        )
        if default_model is None:
            raise CommandError(
                "Не найдено ни одной опубликованной модели компетенций. "
                "Сначала выполните: python manage.py seed_competencies"
            )
        self.default_model: CompetencyModel = default_model

    # -- Кампании -----------------------------------------------------------
    def _create_campaign(
        self,
        *,
        code: str,
        name: str,
        year: int,
        is_360: bool = False,
    ) -> AssessmentCampaign:
        """Создаёт кампанию оценки за указанный год."""
        period_start = dt.date(year, 1, 1)
        period_end = dt.date(year, 12, 31)
        # Срок кампании прошедшего года относится к прошлому, текущей — к
        # будущему: иначе идущая кампания сразу выглядела бы просроченной.
        deadline = (
            dt.date(year, 12, 15)
            if period_end < self.today
            else min(self.today + dt.timedelta(days=45), period_end)
        )

        campaign, _created = AssessmentCampaign.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "description": (
                    "Оценка соответствия сотрудников требованиям должности по "
                    "корпоративной модели компетенций."
                ),
                "competency_model": self.default_model,
                "status": CampaignStatus.DRAFT,
                "period_start": period_start,
                "period_end": period_end,
                "deadline": deadline,
                "methods": [AssessmentMethod.SELF, AssessmentMethod.MANAGER],
                "is_self_assessment_required": True,
                "is_360_enabled": is_360,
                "instructions": (
                    "Оценивайте наблюдаемое поведение, а не отношение к сотруднику. "
                    "Каждому уровню шкалы соответствуют поведенческие индикаторы — "
                    "они показаны рядом с полем оценки."
                ),
            },
        )
        return campaign

    def _fill_campaign(
        self, campaign: AssessmentCampaign, *, fill_rate: float, approve: bool
    ) -> int:
        """Заполняет сессии кампании баллами."""
        sessions = list(
            AssessmentSession.objects.filter(campaign=campaign).select_related(
                "employee", "competency_model"
            )
        )
        requirements = self._requirements_by_model(
            {session.competency_model_id for session in sessions}
        )

        scores: list[AssessmentScore] = []
        filled = 0

        for session in sessions:
            if self.random.random() > fill_rate:
                continue

            targets = requirements.get(session.competency_model_id, [])
            if not targets:
                continue

            for competency_id, target_level in targets:
                level = self._observed_level(
                    session.employee, competency_id, target_level, session.method
                )
                scores.append(
                    AssessmentScore(
                        session=session,
                        competency_id=competency_id,
                        level=Decimal(str(level)),
                        raw_score=AssessmentScore.level_to_score(level),
                        comment="",
                    )
                )

            session.status = SessionStatus.APPROVED if approve else SessionStatus.SUBMITTED
            session.submitted_at = timezone.now()
            session.assessment_date = self._assessment_date(campaign)
            if approve:
                session.approved_at = timezone.now()
            filled += 1

        AssessmentScore.objects.bulk_create(scores, batch_size=1000)
        AssessmentSession.objects.bulk_update(
            [
                session
                for session in sessions
                if session.status in {SessionStatus.SUBMITTED, SessionStatus.APPROVED}
            ],
            ["status", "submitted_at", "approved_at", "assessment_date"],
            batch_size=500,
        )

        campaign.status = self._campaign_status(campaign)
        if campaign.status == CampaignStatus.CLOSED:
            campaign.closed_at = timezone.now()
        campaign.save(update_fields=["status", "closed_at", "updated_at"])
        return filled

    def _campaign_status(self, campaign: AssessmentCampaign) -> str:
        """Итоговое состояние кампании: прошлая завершена, текущая идёт."""
        return CampaignStatus.CLOSED if campaign.period_end < self.today else CampaignStatus.ACTIVE

    def _assessment_date(self, campaign: AssessmentCampaign) -> dt.date:
        """Дата оценки внутри периода кампании."""
        latest = min(campaign.deadline, self.today)
        span = max(1, (latest - campaign.period_start).days)
        return campaign.period_start + dt.timedelta(days=self.random.randint(0, span))

    @staticmethod
    def _requirements_by_model(model_ids: set[int]) -> dict[int, list[tuple[int, int]]]:
        """Требования моделей одним запросом: «модель → [(компетенция, уровень)]»."""
        from apps.competencies.models import CompetencyRequirement

        result: dict[int, list[tuple[int, int]]] = {}
        rows = CompetencyRequirement.objects.filter(competency_model_id__in=model_ids).values_list(
            "competency_model_id", "competency_id", "target_level"
        )
        for model_id, competency_id, target_level in rows:
            result.setdefault(model_id, []).append((competency_id, target_level))
        return result

    # -- Порождение баллов --------------------------------------------------
    def _latent_level(self, employee: Employee, competency_id: int, target: int) -> float:
        """Действительный уровень сотрудника по компетенции."""
        key = (employee.pk, competency_id)
        if key not in self.competency_noise:
            self.competency_noise[key] = self.random.gauss(0, COMPETENCY_SPREAD)

        return target - TARGET_SHORTFALL + self.talent[employee.pk] + self.competency_noise[key]

    def _observed_level(
        self, employee: Employee, competency_id: int, target: int, method: str
    ) -> float:
        """Наблюдаемый уровень: скрытая величина плюс ошибка метода."""
        latent = self._latent_level(employee, competency_id, target)
        bias = METHOD_BIAS.get(method, 0.0)
        noise = METHOD_NOISE.get(method, 0.4)
        observed = latent + bias + self.random.gauss(0, noise)

        # Оценка выставляется с шагом в половину уровня: эксперт различает
        # «уверенно» и «скорее уверенно», но не сотые доли балла.
        rounded = round(observed * 2) / 2
        return float(min(self.scale_max, max(self.scale_min, rounded)))

    # -- Оценка 360° ---------------------------------------------------------
    def _run_360(self, campaign: AssessmentCampaign) -> dict[str, int]:
        """Проводит опрос 360° для выборки сотрудников."""
        campaign.status = CampaignStatus.ACTIVE
        campaign.save(update_fields=["status", "updated_at"])

        candidates = [
            employee
            for employee in self.employees
            if employee.is_key_talent
            or (employee.position and employee.position.grade.is_managerial)
        ]
        self.random.shuffle(candidates)
        limit = max(1, int(len(self.employees) * SURVEY_360_SHARE))
        selected = candidates[:limit]

        invited = 0
        completed = 0
        requirements = self._requirements_by_model(
            {model.pk for model in CompetencyModel.objects.published()}
        )

        for employee in selected:
            invitations = invite_360(campaign, employee, rng=self.random)
            invited += len(invitations)

            for invitation in invitations:
                if self.random.random() > RESPONSE_RATE_360:
                    continue
                completed += self._complete_invitation(invitation, employee, requirements)

        return {"invited": invited, "completed": completed, "employees": len(selected)}

    def _complete_invitation(
        self,
        invitation: Assessment360Respondent,
        employee: Employee,
        requirements: dict[int, list[tuple[int, int]]],
    ) -> int:
        """Заполняет анкету респондента опроса 360°."""
        from apps.competencies.services import model_for_employee

        competency_model = model_for_employee(employee) or self.default_model
        targets = requirements.get(competency_model.pk, [])
        if not targets:
            return 0

        session = AssessmentSession.objects.create(
            campaign=invitation.campaign,
            employee=employee,
            assessor_id=invitation.respondent_id,
            competency_model=competency_model,
            method=AssessmentMethod.ASSESSMENT_360,
            status=SessionStatus.SUBMITTED,
            assessment_date=self._assessment_date(invitation.campaign),
            submitted_at=timezone.now(),
        )

        # Балл 0–100 заполняется явно: пакетная вставка не выполняет метод сохранения модели,
        # который достраивает его по уровню, и оценки опроса 360° остались бы без балла.
        observed = [
            (
                competency_id,
                self._observed_level(
                    employee, competency_id, target_level, AssessmentMethod.ASSESSMENT_360
                ),
            )
            for competency_id, target_level in targets
        ]
        AssessmentScore.objects.bulk_create(
            [
                AssessmentScore(
                    session=session,
                    competency_id=competency_id,
                    level=Decimal(str(level)),
                    raw_score=AssessmentScore.level_to_score(level),
                )
                for competency_id, level in observed
            ]
        )

        invitation.session = session
        invitation.status = RespondentStatus.COMPLETED
        invitation.completed_at = timezone.now()
        invitation.save(update_fields=["session", "status", "completed_at", "updated_at"])
        return 1

    # -- Тестирование --------------------------------------------------------
    def _create_tests(self) -> int:
        """Создаёт банк тестов с заданиями и вариантами ответа."""
        competencies = {
            competency.code: competency
            for competency in Competency.objects.filter(
                code__in=[spec.competency for spec in TESTS]
            )
        }

        created = 0
        for spec in TESTS:
            competency = competencies.get(spec.competency)
            if competency is None:  # pragma: no cover - защита от рассогласования
                self.stderr.write(f"Не найдена компетенция {spec.competency}")
                continue

            test, _new = Test.objects.update_or_create(
                code=spec.code,
                defaults={
                    "name": spec.name,
                    "competency": competency,
                    "description": spec.description,
                    "instructions": spec.instructions,
                    "time_limit_minutes": spec.time_limit_minutes,
                    "questions_per_attempt": spec.questions_per_attempt,
                    "passing_score": spec.passing_score,
                    "max_attempts": spec.max_attempts,
                    "is_active": True,
                },
            )

            # Задания пересоздаются целиком, а ответы попыток при этом
            # защищены внешним ключом с PROTECT: команда не может втихую
            # удалить задание, на которое кто-то отвечал.
            if not TestAnswer.objects.filter(question__test=test).exists():
                test.questions.all().delete()
                self._create_questions(test, spec)
            created += 1

        return created

    def _create_questions(self, test: Test, spec) -> None:
        """Создаёт задания теста вместе с вариантами ответа."""
        for order, question_spec in enumerate(spec.questions, start=1):
            question = TestQuestion.objects.create(
                test=test,
                text=question_spec.text,
                kind=question_spec.kind,
                difficulty=question_spec.difficulty,
                weight=question_spec.weight,
                explanation=question_spec.explanation,
                sort_order=order * 10,
            )
            TestOption.objects.bulk_create(
                [
                    TestOption(
                        question=question,
                        text=option.text,
                        is_correct=option.is_correct,
                        sort_order=index * 10,
                    )
                    for index, option in enumerate(question_spec.options, start=1)
                ]
            )

    def _run_attempts(self) -> int:
        """Порождает попытки прохождения тестов."""
        tests = list(Test.objects.filter(is_active=True).prefetch_related("questions__options"))
        if not tests:
            return 0

        candidates = [employee for employee in self.employees if employee.user_id is not None]
        self.random.shuffle(candidates)

        created = 0
        for employee in candidates[:TEST_ATTEMPTS]:
            test = self.random.choice(tests)
            questions = [question for question in test.questions.all() if question.is_active]
            if not questions:
                continue

            attempt = TestAttempt.objects.create(
                test=test,
                employee=employee,
                attempt_number=1,
                status=AttemptStatus.IN_PROGRESS,
            )
            self._answer_questions(attempt, employee, questions)
            created += 1

        return created

    def _answer_questions(
        self, attempt: TestAttempt, employee: Employee, questions: list[TestQuestion]
    ) -> None:
        """Заполняет ответы попытки и подсчитывает результат."""
        from apps.assessment.services import attempt_questions, finish_attempt

        selected = attempt_questions(attempt)
        if not selected:
            selected = questions[: attempt.test.questions_per_attempt]

        ability = self.talent[employee.pk]

        for question in selected:
            answer = TestAnswer.objects.create(attempt=attempt, question=question)

            if question.kind == QuestionKind.OPEN:
                answer.text_answer = (
                    "Сравнил бы среды по составу данных, версиям зависимостей и "
                    "настройкам, проверяя гипотезы по одной."
                )
                answer.save(update_fields=["text_answer", "updated_at"])
                continue

            options = list(question.options.all())
            correct = [option for option in options if option.is_correct]
            # Вероятность верного ответа: базовая, скорректированная силой
            # сотрудника и сложностью задания.
            chance = 0.55 + ability * 0.22 - (question.difficulty - 3) * 0.09
            if self.random.random() < max(0.1, min(0.95, chance)) and correct:
                answer.selected_options.set(correct)
                answer.is_correct = True
                answer.awarded_score = Decimal(question.weight)
            else:
                wrong = [option for option in options if not option.is_correct]
                if wrong:
                    answer.selected_options.set([self.random.choice(wrong)])
            answer.save(update_fields=["is_correct", "awarded_score", "updated_at"])

        finish_attempt(attempt)
