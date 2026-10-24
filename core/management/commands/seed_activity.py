"""
Текущая работа ролей на демонстрационном стенде: события последних двух недель.

Действия выполняются через сервисы приложений от имени демонстрационных
учётных записей, поэтому порождают те же уведомления, очереди согласования и
записи журнала, что и работа в интерфейсе.

Запуск::

    uv run python manage.py seed_activity
    uv run python manage.py seed_activity --seed 42
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Max
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import LoginRecord, User
from apps.analytics.models import CalibrationSession, CalibrationStatus
from apps.assessment.models import (
    AssessmentCampaign,
    AssessmentMethod,
    AssessmentSession,
    AttemptStatus,
    CampaignStatus,
    QuestionKind,
    SessionStatus,
    Test,
    TestAttempt,
)
from apps.assessment.services import (
    AssessmentError,
    approve_session,
    attempt_questions,
    check_open_answer,
    finish_attempt,
    launch_campaign,
    pending_manual_checks,
    question_options,
    reject_session,
    save_answer,
    save_scores,
    start_attempt,
)
from apps.audit.middleware import audit_as
from apps.audit.models import AuditAction, AuditLog
from apps.development.models import (
    ActivityEvidence,
    ActivityStatus,
    DevelopmentActivity,
    DevelopmentPlan,
    EvidenceKind,
    Mentorship,
    MentorshipStatus,
    PlanStatus,
)
from apps.development.services import (
    DevelopmentError,
    add_evidence,
    approve_plan,
    complete_activity,
    create_plan,
    fill_plan_from_gaps,
    log_meeting,
    start_mentorship,
    submit_plan,
    verify_evidence,
)
from apps.development.tasks import notify_due_soon, notify_overdue
from apps.notifications.models import Notification
from apps.org.models import Department, Position
from apps.people.models import Employee
from apps.portal.models import Feedback
from apps.reporting.models import ReportTemplate
from apps.reporting.services import ReportError, generate
from apps.reserve.management.commands.seed_reserve import department_family
from apps.reserve.models import (
    ACTIVE_RESERVE_STATUSES,
    PENDING_NOMINATION_STATUSES,
    NominationKind,
    NominationStatus,
    ReserveNomination,
)
from apps.reserve.services import ReserveError, decide_hr, decide_manager, submit_nomination

#: Значение генератора случайных чисел по умолчанию.
DEFAULT_SEED = 20261010

DOMAIN = "meridian-soft.ru"

#: Демонстрационные сотрудники: их заявки и планы ведутся отдельными сценариями.
DEMO_EMPLOYEES = ("igor.smirnov", "andrey.kuznetsov", "artem.volkov", "natalya.gavrilova")

#: Адреса и сети, с которых входят пользователи стенда.
OFFICE_NETWORKS = ("10.20.4.", "10.20.7.", "10.31.2.", "10.44.1.")
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/129.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 Version/18.0 Safari/605.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 YaBrowser/24.10 Safari/537.36",
)

FEEDBACK = (
    {
        "name": "Павел Королёв",
        "email": "p.korolev@example.ru",
        "organization": "ООО «Северный терминал»",
        "topic": Feedback.Topic.QUESTION,
        "subject": "Можно ли выгрузить план развития в Excel",
        "message": (
            "Добрый день. Руководители просят выгружать планы развития подчинённых "
            "в табличном виде для еженедельных встреч. Есть ли такая возможность?"
        ),
        "status": Feedback.Status.ANSWERED,
        "answer": (
            "Здравствуйте. Отчёт «Выполнение планов развития» формируется в формате XLSX "
            "из раздела «Отчёты» и учитывает область видимости руководителя."
        ),
        "days_ago": 12,
    },
    {
        "name": "Елена Гусева",
        "email": "e.guseva@example.ru",
        "organization": "",
        "topic": Feedback.Topic.RESERVE,
        "subject": "Как подать заявку в резерв самостоятельно",
        "message": (
            "Я работаю в компании второй год и хотела бы попасть в резерв на должность "
            "ведущего аналитика. Нужно ли согласие руководителя заранее?"
        ),
        "status": Feedback.Status.IN_PROGRESS,
        "answer": "",
        "days_ago": 4,
    },
    {
        "name": "Игорь Мельников",
        "email": "i.melnikov@example.ru",
        "organization": "ГК «Профиль»",
        "topic": Feedback.Topic.SUGGESTION,
        "subject": "Предложение: напоминания о встречах наставника",
        "message": (
            "Было бы удобно получать напоминание за день до плановой встречи с "
            "подопечным, а не только отметку о просрочке."
        ),
        "status": Feedback.Status.NEW,
        "answer": "",
        "days_ago": 2,
    },
    {
        "name": "Ольга Тихонова",
        "email": "o.tikhonova@example.ru",
        "organization": "",
        "topic": Feedback.Topic.ACCESS,
        "subject": "Не приходит письмо для смены пароля",
        "message": (
            "Запросила восстановление пароля, но письмо так и не пришло. Проверила папку "
            "«Спам» — пусто."
        ),
        "status": Feedback.Status.NEW,
        "answer": "",
        "days_ago": 1,
    },
)

OPEN_ANSWERS = (
    "Начал бы с выяснения, какие требования действительно обязательны к релизу, и "
    "согласовал бы перенос остальных. Риски зафиксировал бы письменно.",
    "Разделил бы задачу на этапы, для каждого определил бы ответственного и срок, "
    "промежуточный результат показывал бы владельцу процесса раз в неделю.",
    "Собрал бы данные о частоте сбоя, воспроизвёл бы его на тестовом стенде и только "
    "потом предлагал бы исправление.",
)


class Command(BaseCommand):
    """Воспроизводит работу ролей за последние две недели."""

    help = "Наполняет стенд текущей работой ролей: заявки, оценки, планы, отчёты, уведомления"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--seed",
            type=int,
            default=DEFAULT_SEED,
            help="значение генератора случайных чисел для воспроизводимости",
        )

    @transaction.atomic
    # Письма по уведомлениям при наполнении не отправляются.
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def handle(self, *args, **options) -> None:
        self.random = random.Random(options["seed"])  # noqa: S311  # nosec B311
        self.now = timezone.now()
        self.users = {
            user.email.split("@")[0]: user
            for user in User.objects.filter(email__endswith=f"@{DOMAIN}").select_related(
                "department"
            )
        }
        self.done: list[str] = []

        steps: tuple[Callable[[], None], ...] = (
            self._logins,
            self._nominations,
            self._assessment,
            self._tests,
            self._development,
            self._mentorship,
            self._calibration,
            self._reports,
            self._feedback,
            self._reminders,
            self._read_old_notifications,
        )
        for step in steps:
            step()

        self.stdout.write(self.style.SUCCESS("Текущая работа ролей воспроизведена:"))
        for line in self.done:
            self.stdout.write(f"  {line}")

    # -- Вспомогательное ----------------------------------------------------
    def _user(self, login: str) -> User:
        return self.users[login]

    def _moment(self, days_ago: int, hour: int | None = None) -> dt.datetime:
        """Рабочее время указанного дня в прошлом."""
        day = timezone.localtime(self.now) - dt.timedelta(days=days_ago)
        while day.weekday() >= 5:
            day -= dt.timedelta(days=1)
        hour = hour if hour is not None else self.random.randint(9, 17)
        return day.replace(hour=hour, minute=self.random.randint(0, 59), second=0, microsecond=0)

    @contextmanager
    def _at(self, days_ago: int, actor: User | None = None) -> Iterator[dt.datetime]:
        """Относит уведомления и записи журнала, созданные в блоке, к моменту в прошлом."""
        moment = self._moment(days_ago)
        last_notification = Notification.objects.aggregate(top=Max("id"))["top"] or 0
        last_audit = AuditLog.objects.aggregate(top=Max("id"))["top"] or 0
        with audit_as(actor):
            yield moment
        Notification.objects.filter(id__gt=last_notification).update(created_at=moment)
        AuditLog.objects.filter(id__gt=last_audit).update(created_at=moment)

    # -- Входы в систему ----------------------------------------------------
    def _logins(self) -> None:
        """История входов демонстрационных пользователей за две недели."""
        records: list[LoginRecord] = []
        audit: list[AuditLog] = []
        demo = [user for login, user in self.users.items() if not user.must_change_password]

        for user in demo:
            network = self.random.choice(OFFICE_NETWORKS)
            agent = self.random.choice(USER_AGENTS)
            address = f"{network}{self.random.randint(10, 240)}"
            for days_ago in sorted(self.random.sample(range(1, 15), self.random.randint(4, 9))):
                moment = self._moment(days_ago, hour=self.random.randint(8, 11))
                records.append(
                    LoginRecord(
                        user=user,
                        outcome=LoginRecord.Outcome.SUCCESS,
                        ip_address=address,
                        user_agent=agent,
                    )
                )
                audit.append(
                    AuditLog(
                        actor=user,
                        actor_repr=user.email,
                        actor_role=user.role,
                        action=AuditAction.LOGIN,
                        message=f"Вход в систему: {user.email}",
                        ip_address=address,
                        user_agent=agent,
                    )
                )
                records[-1].created_at = audit[-1].created_at = moment

        # Неудачные попытки: опечатка в пароле и подбор к чужой записи.
        target = self._user("igor.smirnov")
        for days_ago, address in ((6, "10.20.4.57"), (6, "10.20.4.57"), (3, "185.112.40.9")):
            moment = self._moment(days_ago)
            records.append(
                LoginRecord(
                    user=target,
                    email_attempted=target.email,
                    outcome=LoginRecord.Outcome.FAILED,
                    ip_address=address,
                    user_agent=USER_AGENTS[0],
                )
            )
            audit.append(
                AuditLog(
                    actor=None,
                    actor_repr=target.email,
                    action=AuditAction.LOGIN_FAILED,
                    message=f"Неудачная попытка входа: {target.email}",
                    ip_address=address,
                    user_agent=USER_AGENTS[0],
                )
            )
            records[-1].created_at = audit[-1].created_at = moment

        created = LoginRecord.objects.bulk_create(records)
        AuditLog.objects.bulk_create(audit)
        # Отметка времени задаётся при создании автоматически и переписывается отдельно.
        for record_, original in zip(created, records, strict=True):
            LoginRecord.objects.filter(pk=record_.pk).update(created_at=original.created_at)
        for user in demo:
            last = max(
                (item.created_at for item in records if item.user_id == user.pk),
                default=None,
            )
            if last is not None:
                User.objects.filter(pk=user.pk).update(last_login=last, last_activity_at=last)
        self.done.append(f"входов в систему: {len(records)}")

    # -- Кадровый резерв ----------------------------------------------------
    def _nominations(self) -> None:
        """Заявки в резерв на всех ступенях согласования."""
        orlova = self._user("elena.orlova")
        petrov = self._user("dmitry.petrov")
        ivanova = self._user("maria.ivanova")
        kuznetsov = self._user("andrey.kuznetsov")
        submitted = 0

        # Начальник отдела выдвигает сотрудников: заявка сразу уходит в кадровую службу.
        candidates = (
            Employee.objects.active()
            .filter(department=orlova.department)
            .exclude(user=orlova)
            .exclude(user__email__in=[f"{login}@{DOMAIN}" for login in DEMO_EMPLOYEES])
            .exclude(nominations__status__in=PENDING_NOMINATION_STATUSES)
            .exclude(reserve_entries__status__in=ACTIVE_RESERVE_STATUSES)
            .order_by("-position__grade__level", "last_name")
        )
        for employee in candidates[:12]:
            if submitted == 2:
                break
            if self._nominate(employee, orlova, NominationKind.MANAGER, days_ago=9 - submitted * 4):
                submitted += 1

        # Самовыдвижение: заявка ждёт решения непосредственного руководителя.
        employee = kuznetsov.employee
        if employee is not None and self._nominate(
            employee, kuznetsov, NominationKind.SELF, days_ago=3
        ):
            submitted += 1

        # Руководитель департамента согласует заявку из своей очереди.
        from apps.reserve.services import pending_for_user

        for nomination in pending_for_user(petrov)[:1]:
            with self._at(2, petrov):
                decide_manager(
                    nomination,
                    actor=petrov,
                    approve=True,
                    comment="Поддерживаю: сотрудник уже ведёт смежное направление.",
                )

        # Кадровая служба включает одного кандидата и отклоняет другого.
        decisions = [
            (True, "Соответствует требованиям программы, включён с пересмотром через полгода."),
            (False, "Недостаточный управленческий опыт; рекомендовано повторное выдвижение."),
        ]
        decided = 0
        for nomination in pending_for_user(ivanova)[:10]:
            if not decisions:
                break
            approve, comment = decisions[0]
            try:
                with self._at(1, ivanova):
                    decide_hr(nomination, actor=ivanova, approve=approve, comment=comment)
            except ReserveError:
                # Кандидат, уже состоящий в резерве на эту должность, остаётся в очереди.
                continue
            decisions.pop(0)
            decided += 1
        self.done.append(f"заявок подано: {submitted}, решений кадровой службы: {decided}")

    def _nominate(self, employee: Employee, actor: User, kind: str, days_ago: int) -> bool:
        """Создаёт и подаёт заявку на ближайшую подходящую вышестоящую должность."""
        if employee.position is None:
            return False
        family = department_family(employee.department)
        target = next(
            (
                position
                for position in Position.objects.filter(
                    department__branch=employee.branch,
                    grade__level__gt=employee.position.grade.level,
                    is_active=True,
                )
                .exclude(nominations__employee=employee)
                .select_related("department")
                .order_by("grade__level", "name")
                if department_family(position.department) == family
            ),
            None,
        )
        if target is None:
            return False

        nomination = ReserveNomination.objects.create(
            employee=employee,
            target_position=target,
            kind=kind,
            status=NominationStatus.DRAFT,
            nominated_by=actor,
            justification=(
                "Стабильно выполняет задачи уровня следующей должности, замещал руководителя "
                "в период отпуска, проявил себя в межфункциональном проекте."
            ),
            achievements="Запуск сервиса уведомлений; наставничество двух новых сотрудников.",
        )
        try:
            with self._at(days_ago, actor):
                submit_nomination(nomination, actor=actor)
        except ReserveError:
            nomination.delete()
            return False
        ReserveNomination.objects.filter(pk=nomination.pk).update(
            submitted_at=self._moment(days_ago)
        )
        return True

    # -- Оценка ---------------------------------------------------------------
    def _assessment(self) -> None:
        """Запуск кампании, заполнение, утверждение и возврат сессий."""
        kuznetsova = self._user("olga.kuznetsova")
        orlova = self._user("elena.orlova")
        romanova = self._user("anna.romanova")
        unit = romanova.department
        template = AssessmentCampaign.objects.filter(status=CampaignStatus.ACTIVE).first()
        if unit is None or template is None:
            return

        campaign = AssessmentCampaign.objects.create(
            code="dev-nsk-360-2026",
            name="Круговая оценка департамента разработки (Новосибирск)",
            description=(
                "Обратная связь коллег, подчинённых и руководителя для сотрудников "
                "департамента перед формированием планов развития на следующий год."
            ),
            competency_model=template.competency_model,
            status=CampaignStatus.DRAFT,
            period_start=timezone.localdate() - dt.timedelta(days=6),
            period_end=timezone.localdate() + dt.timedelta(days=24),
            deadline=timezone.localdate() + dt.timedelta(days=17),
            methods=[
                AssessmentMethod.SELF,
                AssessmentMethod.MANAGER,
                AssessmentMethod.ASSESSMENT_360,
            ],
            branch=unit.branch,
            department=unit,
            is_self_assessment_required=True,
            is_360_enabled=True,
            is_anonymous_360=True,
            instructions=(
                "Оценивайте наблюдаемое поведение за последние полгода. Уровень ставится по "
                "описанию шкалы, а не по общему впечатлению."
            ),
            created_by=kuznetsova,
        )
        with self._at(6, kuznetsova):
            totals = launch_campaign(campaign, actor=kuznetsova)

        # Руководитель начал заполнять одну сессию и успел внести половину оценок.
        session = (
            AssessmentSession.objects.filter(
                campaign=campaign, assessor=romanova, method=AssessmentMethod.MANAGER
            )
            .select_related("competency_model")
            .first()
        )
        if session is not None:
            competencies = list(
                session.competency_model.requirements.values_list("competency_id", flat=True)
            )
            half = competencies[: max(1, len(competencies) // 2)]
            save_scores(session, {pk: Decimal(self.random.choice((2, 3, 3, 4))) for pk in half})

        # Кадровая служба утверждает внесённые оценки и одну возвращает на доработку.
        submitted = list(
            AssessmentSession.objects.filter(
                status=SessionStatus.SUBMITTED,
                method=AssessmentMethod.MANAGER,
                campaign__status=CampaignStatus.ACTIVE,
                employee__user__isnull=False,
            ).select_related("employee__user", "assessor")[:4]
        )
        for days_ago, item in zip((4, 3, 2), submitted[:3], strict=False):
            with self._at(days_ago, kuznetsova):
                approve_session(item, actor=kuznetsova)
        returned = (
            AssessmentSession.objects.filter(
                status=SessionStatus.SUBMITTED,
                assessor=orlova,
                campaign__status=CampaignStatus.ACTIVE,
            )
            .select_related("employee", "assessor")
            .first()
        )
        if returned is not None:
            with self._at(1, kuznetsova):
                reject_session(
                    returned,
                    actor=kuznetsova,
                    reason=(
                        "По двум компетенциям оценка ниже ожидаемой без комментария. "
                        "Добавьте примеры поведения."
                    ),
                )
        self.done.append(
            f"кампания «{campaign.name}»: сессий {totals['sessions']}, "
            f"приглашений 360° {totals['invitations']}"
        )

    # -- Тестирование ---------------------------------------------------------
    def _tests(self) -> None:
        """Попытки с развёрнутыми ответами: одна проверена экспертом, остальные ждут."""
        ivanova = self._user("maria.ivanova")
        tests = [
            test
            for test in Test.objects.filter(is_active=True).prefetch_related("questions")
            if test.is_ready
            and test.questions.filter(kind=QuestionKind.OPEN, is_active=True).exists()
        ]
        if not tests:
            return

        finished: list[TestAttempt] = []
        for login, days_ago in (("igor.smirnov", 5), ("artem.volkov", 3), ("andrey.kuznetsov", 2)):
            employee = self._user(login).employee
            if employee is None:
                continue
            test = next(
                (
                    item
                    for item in tests
                    if TestAttempt.objects.filter(test=item, employee=employee).count()
                    < item.max_attempts
                ),
                None,
            )
            if test is None:
                continue
            try:
                attempt = start_attempt(test, employee)
            except AssessmentError:
                continue
            for question in attempt_questions(attempt):
                if question.kind == QuestionKind.OPEN:
                    save_answer(attempt, question, text=self.random.choice(OPEN_ANSWERS))
                    continue
                options = list(question_options(question, attempt))
                correct = [option.pk for option in options if option.is_correct]
                if correct and self.random.random() < 0.75:
                    chosen = correct if question.kind == QuestionKind.MULTIPLE else correct[:1]
                else:
                    chosen = [self.random.choice(options).pk] if options else []
                save_answer(attempt, question, option_ids=chosen)
            finish_attempt(attempt)
            moment = self._moment(days_ago)
            TestAttempt.objects.filter(pk=attempt.pk).update(
                started_at=moment - dt.timedelta(minutes=self.random.randint(14, 35)),
                finished_at=moment,
            )
            finished.append(attempt)

        # Эксперт проверил первый развёрнутый ответ из очереди, остальные ждут проверки.
        answer = pending_manual_checks(ivanova).first()
        if answer is not None:
            with self._at(1, ivanova):
                check_open_answer(
                    answer,
                    score=Decimal(answer.question.weight) * Decimal("0.75"),
                    actor=ivanova,
                )
        pending = pending_manual_checks(ivanova).count()
        self.done.append(
            f"попыток тестирования: {len(finished)}, развёрнутых ответов на проверке: {pending}"
        )
        # Незавершённые попытки сразу закрываются: истёкшее время не должно висеть в стенде.
        TestAttempt.objects.filter(
            pk__in=[attempt.pk for attempt in finished], status=AttemptStatus.IN_PROGRESS
        ).update(status=AttemptStatus.CHECKED)

    # -- Развитие -----------------------------------------------------------
    def _development(self) -> None:
        """Отчёты сотрудников, проверка руководителем, новый план на согласовании."""
        events = 0

        # Сотрудники отчитываются о выполнении и прикладывают подтверждения.
        for login, days_ago in (("artem.volkov", 8), ("igor.smirnov", 4), ("andrey.kuznetsov", 2)):
            user = self._user(login)
            activity = (
                DevelopmentActivity.objects.filter(
                    plan__employee__user=user,
                    plan__status=PlanStatus.ACTIVE,
                    status__in=[ActivityStatus.IN_PROGRESS, ActivityStatus.PENDING],
                )
                .select_related("plan__supervisor", "mentor")
                .order_by("due_date")
                .first()
            )
            if activity is None:
                continue
            with self._at(days_ago, user):
                add_evidence(
                    activity,
                    actor=user,
                    kind=EvidenceKind.REPORT,
                    title=f"Отчёт о выполнении: {activity.title}"[:255],
                    external_url="https://wiki.meridian-soft.ru/reports/development",
                    comment="Результат обсуждён с руководителем на еженедельной встрече.",
                )
                with suppress(DevelopmentError):
                    complete_activity(
                        activity,
                        actor=user,
                        result="Задача выполнена в срок, выводы оформлены в базе знаний отдела.",
                        on_date=timezone.localdate() - dt.timedelta(days=days_ago),
                    )
            events += 1

        # Руководитель проверяет одно подтверждение своего подчинённого.
        orlova = self._user("elena.orlova")
        evidence = (
            ActivityEvidence.objects.filter(is_verified=False, activity__plan__supervisor=orlova)
            .exclude(submitted_by=orlova)
            .order_by("created_at")
            .first()
        )
        if evidence is not None:
            with self._at(1, orlova):
                verify_evidence(evidence, actor=orlova)
            events += 1

        # Руководитель департамента утверждает план, ожидавший согласования.
        petrov = self._user("dmitry.petrov")
        plan = (
            DevelopmentPlan.objects.filter(status=PlanStatus.ON_APPROVAL, supervisor=petrov)
            .exclude(employee__user=petrov)
            .first()
        )
        if plan is not None:
            with self._at(3, petrov):
                approve_plan(plan, actor=petrov)
            events += 1

        # Сотрудник после завершения прежнего плана составляет новый и подаёт на согласование.
        gavrilova = self._user("natalya.gavrilova")
        employee = gavrilova.employee
        if (
            employee is not None
            and not DevelopmentPlan.objects.filter(
                employee=employee, status__in=[PlanStatus.DRAFT, PlanStatus.ON_APPROVAL]
            ).exists()
        ):
            new_plan = create_plan(
                employee,
                actor=gavrilova,
                title="План развития на 2027 год",
                goal=(
                    "Подготовиться к роли ведущего разработчика: архитектурные решения, "
                    "проведение ревью кода, наставничество младших коллег."
                ),
                start_date=timezone.localdate() + dt.timedelta(days=7),
                end_date=timezone.localdate() + dt.timedelta(days=372),
            )
            if fill_plan_from_gaps(new_plan, actor=gavrilova, limit=4):
                with self._at(2, gavrilova):
                    submit_plan(new_plan, actor=gavrilova)
                events += 1
        self.done.append(f"событий по планам развития: {events}")

    # -- Наставничество -------------------------------------------------------
    def _mentorship(self) -> None:
        """Новая пара, назначенная руководителем филиала, и встречи наставника."""
        lebedev = self._user("maxim.lebedev")
        romanova = self._user("anna.romanova")
        if romanova.department_id is None:
            return

        mentee = (
            Employee.objects.active()
            .filter(department_id__in=Department.objects.descendant_ids(romanova.department_id))
            .exclude(user=lebedev)
            .exclude(mentorships__status=MentorshipStatus.ACTIVE)
            .filter(position__grade__level__lte=4)
            .order_by("position__grade__level", "last_name")
            .first()
        )
        created = 0
        if mentee is not None:
            try:
                with self._at(20, romanova) as moment:
                    pair = start_mentorship(
                        mentor=lebedev,
                        mentee=mentee,
                        goal=(
                            "Освоение платформенных сервисов и переход к самостоятельному "
                            "дежурству."
                        ),
                        actor=romanova,
                        end_date=timezone.localdate() + dt.timedelta(days=160),
                    )
                Mentorship.objects.filter(pk=pair.pk).update(start_date=moment.date())
                created = 1
            except DevelopmentError as error:
                self.stdout.write(self.style.WARNING(f"Пара не создана: {error}"))

        meetings = 0
        for pair in Mentorship.objects.filter(mentor=lebedev, status=MentorshipStatus.ACTIVE):
            for days_ago in (12, 5):
                with self._at(days_ago, lebedev):
                    log_meeting(
                        pair,
                        actor=lebedev,
                        on_date=timezone.localdate() - dt.timedelta(days=days_ago),
                        note="Разбор архитектурного решения и плана на следующие две недели.",
                    )
                meetings += 1
        self.done.append(f"встреч наставника: {meetings}, новых пар: {created}")

    # -- Калибровка -----------------------------------------------------------
    def _calibration(self) -> None:
        """Предстоящая калибровка руководителей департамента разработки."""
        ivanova = self._user("maria.ivanova")
        petrov = self._user("dmitry.petrov")
        session = CalibrationSession.objects.create(
            name="Калибровка 9-box — департамент разработки",
            description=(
                "Сверка размещений в матрице «результативность — потенциал» по итогам "
                "годовой оценки перед утверждением резерва на следующий год."
            ),
            held_on=timezone.localdate() + dt.timedelta(days=9),
            status=CalibrationStatus.PLANNED,
            branch=petrov.branch,
            department=petrov.department,
            facilitator=ivanova,
            agenda=(
                "1. Расхождения между предварительным размещением и мнением руководителей.\n"
                "2. Кандидаты зоны «высокий потенциал».\n"
                "3. Сотрудники с риском ухода в зонах 7–9."
            ),
        )
        session.participants.set(
            [petrov, self._user("elena.orlova"), self._user("olga.kuznetsova")]
        )
        self.done.append("запланирована калибровка департамента разработки")

    # -- Отчёты ---------------------------------------------------------------
    def _reports(self) -> None:
        """Выгрузки, сформированные пользователями разных ролей."""
        orders = (
            ("maria.ivanova", "reserve-roster", 9),
            ("maria.ivanova", "succession-coverage", 6),
            ("olga.kuznetsova", "development-progress", 3),
            ("dmitry.petrov", "unit-summary", 2),
            ("alexey.sidorov", "readiness-ranking", 1),
        )
        made = 0
        for login, code, days_ago in orders:
            template = ReportTemplate.objects.filter(code=code).first()
            if template is None:
                continue
            user = self._user(login)
            try:
                with self._at(days_ago, user) as moment:
                    report = generate(template, {}, user)
                type(report).objects.filter(pk=report.pk).update(
                    created_at=moment, started_at=moment, finished_at=moment
                )
                made += 1
            except ReportError as error:
                self.stdout.write(self.style.WARNING(f"Отчёт {code} не сформирован: {error}"))
        self.done.append(f"сформировано отчётов: {made}")

    # -- Обратная связь -------------------------------------------------------
    def _feedback(self) -> None:
        """Обращения через форму публичной части."""
        admin = self._user("admin")
        ivanova = self._user("maria.ivanova")
        for item in FEEDBACK:
            data: dict[str, Any] = {
                key: value for key, value in item.items() if key not in {"days_ago", "answer"}
            }
            moment = self._moment(int(str(item["days_ago"])))
            feedback = Feedback.objects.create(**data, consent_given=True)
            updates: dict[str, Any] = {"created_at": moment}
            if item["status"] != Feedback.Status.NEW:
                updates["assigned_to"] = (
                    ivanova if item["topic"] == Feedback.Topic.RESERVE else admin
                )
            if item["answer"]:
                updates.update(
                    answer=item["answer"],
                    answered_by=admin,
                    answered_at=moment + dt.timedelta(hours=5),
                )
            Feedback.objects.filter(pk=feedback.pk).update(**updates)
        self.done.append(f"обращений через форму: {len(FEEDBACK)}")

    # -- Напоминания ----------------------------------------------------------
    def _reminders(self) -> None:
        """Плановые напоминания о сроках мероприятий."""
        due = notify_due_soon()
        overdue = notify_overdue()
        self.done.append(f"напоминаний о сроках: {due}, о просрочке: {overdue}")

    def _read_old_notifications(self) -> None:
        """Уведомления старше трёх дней в основном прочитаны."""
        border = self.now - dt.timedelta(days=3)
        old = list(
            Notification.objects.filter(read_at__isnull=True, created_at__lt=border).values_list(
                "pk", "created_at"
            )
        )
        read = 0
        for pk, created in old:
            if self.random.random() < 0.8:
                Notification.objects.filter(pk=pk).update(
                    read_at=created + dt.timedelta(hours=self.random.randint(1, 30))
                )
                read += 1
        self.done.append(f"прочитано уведомлений: {read} из {len(old)} старше трёх дней")
