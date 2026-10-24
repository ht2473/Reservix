"""Процедуры оценки: запуск кампании, опрос 360°, встроенное тестирование."""

from __future__ import annotations

import datetime as dt
import random
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.assessment.models import (
    AUTO_CHECKED_KINDS,
    RESPONDENT_WEIGHTS,
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
    Test,
    TestAnswer,
    TestAttempt,
    TestQuestion,
)
from apps.competencies.models import CompetencyModel
from apps.people.models import Employee, EmployeeStatus
from core.settings_types import get_reservix_settings

if TYPE_CHECKING:
    from apps.accounts.models import User


class AssessmentError(Exception):
    """Нарушение правил процедуры оценки."""


# ---------------------------------------------------------------------------
# Кампания оценки
# ---------------------------------------------------------------------------
#: Число коллег, приглашаемых в опрос 360° по умолчанию.
DEFAULT_PEER_COUNT = 4

#: Наибольшее число подчинённых, приглашаемых в опрос 360°.
DEFAULT_SUBORDINATE_COUNT = 4


def campaign_participants(campaign: AssessmentCampaign) -> QuerySet[Employee]:
    """Состав участников кампании."""
    queryset = Employee.objects.active().internal().with_org()

    if campaign.department_id:
        queryset = queryset.in_department_tree(campaign.department_id)
    elif campaign.branch_id:
        queryset = queryset.in_branch(campaign.branch_id)

    return queryset.exclude(status=EmployeeStatus.LEAVE).order_by(
        "department__name", "last_name", "first_name"
    )


def _model_for(employee: Employee, default: CompetencyModel) -> CompetencyModel:
    """Модель компетенций, по которой оценивается участник."""
    from apps.competencies.services import model_for_employee

    return model_for_employee(employee) or default


@transaction.atomic
def launch_campaign(campaign: AssessmentCampaign, *, actor: User | None = None) -> dict[str, int]:
    """Запускает кампанию: формирует сессии оценки и приглашения 360°."""
    if campaign.status in {CampaignStatus.CLOSED, CampaignStatus.CANCELLED}:
        raise AssessmentError(_("Завершённую кампанию нельзя запустить повторно"))

    participants = list(campaign_participants(campaign))
    existing = set(
        AssessmentSession.objects.filter(campaign=campaign).values_list("employee_id", "method")
    )

    created_sessions = 0
    created_invitations = 0

    for employee in participants:
        competency_model = _model_for(employee, campaign.competency_model)

        if campaign.is_self_assessment_required and employee.user_id:
            # Самооценку заполняет сам сотрудник, поэтому сессия создаётся
            # только при наличии учётной записи: некому будет её открыть.
            key = (employee.pk, AssessmentMethod.SELF)
            if key not in existing:
                _create_session(
                    campaign,
                    employee,
                    competency_model,
                    method=AssessmentMethod.SELF,
                    assessor_id=employee.user_id,
                )
                created_sessions += 1

        manager_user_id = _manager_user_id(employee)
        if manager_user_id is not None:
            key = (employee.pk, AssessmentMethod.MANAGER)
            if key not in existing:
                _create_session(
                    campaign,
                    employee,
                    competency_model,
                    method=AssessmentMethod.MANAGER,
                    assessor_id=manager_user_id,
                )
                created_sessions += 1

        if campaign.is_360_enabled:
            created_invitations += len(invite_360(campaign, employee))

    if campaign.status == CampaignStatus.DRAFT:
        campaign.status = CampaignStatus.ACTIVE
        campaign.updated_by = actor
        campaign.save(update_fields=["status", "updated_by", "updated_at"])

    _notify_launch(campaign, actor=actor)

    return {
        "participants": len(participants),
        "sessions": created_sessions,
        "invitations": created_invitations,
    }


def _notify_launch(campaign: AssessmentCampaign, *, actor: User | None) -> None:
    """Извещает оценивающих о назначенных сессиях, оцениваемых — о начале кампании."""
    if campaign.deadline < timezone.localdate():
        return

    from apps.accounts.models import User
    from apps.notifications.models import NotificationKind
    from apps.notifications.services import notify

    pending = AssessmentSession.objects.filter(
        campaign=campaign, status__in=[SessionStatus.PLANNED, SessionStatus.IN_PROGRESS]
    )
    loads = dict(
        pending.exclude(assessor__isnull=True)
        .values_list("assessor_id")
        .annotate(total=Count("id"))
        .values_list("assessor_id", "total")
    )
    deadline = campaign.deadline.strftime("%d.%m.%Y")
    for assessor in User.objects.filter(pk__in=loads):
        notify(
            assessor,
            NotificationKind.ASSESSMENT_REQUESTED,
            str(_("Требуется оценка: %(campaign)s") % {"campaign": campaign.name}),
            body=str(
                _("Назначено сессий: %(total)s. Срок заполнения — %(deadline)s.")
                % {"total": loads[assessor.pk], "deadline": deadline}
            ),
            obj=campaign,
            url=reverse("assessment:inbox"),
            actor=actor,
        )

    for user in User.objects.filter(
        employee_card__in=campaign_participants(campaign), is_active=True
    ).exclude(pk__in=loads):
        notify(
            user,
            NotificationKind.CAMPAIGN_STARTED,
            str(_("Началась кампания оценки: %(campaign)s") % {"campaign": campaign.name}),
            body=str(_("Результаты будут доступны после утверждения кадровой службой.")),
            obj=campaign,
            url=reverse("assessment:my_results"),
            actor=actor,
        )


@transaction.atomic
def approve_session(session: AssessmentSession, *, actor: User) -> None:
    """Утверждает сессию и извещает оцениваемого о готовности результата."""
    from apps.notifications.models import NotificationKind
    from apps.notifications.services import notify

    session.approve(actor)
    employee_user = session.employee.user
    if employee_user is not None and session.method != AssessmentMethod.ASSESSMENT_360:
        notify(
            employee_user,
            NotificationKind.ASSESSMENT_COMPLETED,
            str(_("Оценка утверждена: %(method)s") % {"method": session.get_method_display()}),
            obj=session,
            url=reverse("assessment:my_results"),
            actor=actor,
        )


@transaction.atomic
def reject_session(session: AssessmentSession, *, actor: User, reason: str) -> None:
    """Возвращает сессию оценивающему с указанием причины."""
    from apps.notifications.models import NotificationKind
    from apps.notifications.services import notify

    session.reject(reason)
    if session.assessor is not None:
        notify(
            session.assessor,
            NotificationKind.ASSESSMENT_REQUESTED,
            str(_("Оценка возвращена на доработку: %(employee)s") % {"employee": session.employee}),
            body=reason,
            obj=session,
            actor=actor,
            deduplicate=False,
        )


def _create_session(
    campaign: AssessmentCampaign,
    employee: Employee,
    competency_model: CompetencyModel,
    *,
    method: str,
    assessor_id: int | None,
) -> AssessmentSession:
    """Создаёт запланированную сессию оценки."""
    return AssessmentSession.objects.create(
        campaign=campaign,
        employee=employee,
        assessor_id=assessor_id,
        competency_model=competency_model,
        method=method,
        status=SessionStatus.PLANNED,
        assessment_date=min(timezone.localdate(), campaign.deadline),
    )


def _manager_user_id(employee: Employee) -> int | None:
    """Учётная запись руководителя, оценивающего сотрудника."""
    if employee.manager_id and employee.manager and employee.manager.user_id:
        return employee.manager.user_id

    # Руководитель подразделения не оценивает сам себя как подчинённого.
    department = employee.department
    if department is not None and department.head_id and department.head_id != employee.user_id:
        return department.head_id
    return None


@transaction.atomic
def close_campaign(campaign: AssessmentCampaign, *, actor: User | None = None) -> dict[str, int]:
    """Завершает кампанию."""
    cancelled = AssessmentSession.objects.filter(
        campaign=campaign,
        status__in=[SessionStatus.PLANNED, SessionStatus.IN_PROGRESS],
    ).update(status=SessionStatus.CANCELLED, updated_at=timezone.now())

    expired = Assessment360Respondent.objects.filter(
        campaign=campaign,
        status__in=[RespondentStatus.INVITED, RespondentStatus.IN_PROGRESS],
    ).update(status=RespondentStatus.EXPIRED)

    campaign.status = CampaignStatus.CLOSED
    campaign.closed_at = timezone.now()
    campaign.updated_by = actor
    campaign.save(update_fields=["status", "closed_at", "updated_by", "updated_at"])

    return {"cancelled_sessions": cancelled, "expired_invitations": expired}


@dataclass(frozen=True, slots=True)
class CampaignProgress:
    """Ход кампании в разрезе состояний сессий."""

    total: int
    planned: int
    in_progress: int
    submitted: int
    approved: int
    rejected: int
    cancelled: int

    @property
    def done(self) -> int:
        return self.submitted + self.approved

    @property
    def completion_rate(self) -> float:
        """Доля завершённых от подлежащих заполнению."""
        base = self.total - self.cancelled
        return round(self.done * 100 / base, 1) if base else 0.0

    @property
    def approval_rate(self) -> float:
        """Доля утверждённых от внесённых — показатель работы кадровой службы."""
        return round(self.approved * 100 / self.done, 1) if self.done else 0.0


def campaign_progress(campaign: AssessmentCampaign) -> CampaignProgress:
    """Считает ход кампании одним запросом с группировкой."""
    counts = AssessmentSession.objects.filter(campaign=campaign).aggregate(
        total=Count("id"),
        planned=Count("id", filter=Q(status=SessionStatus.PLANNED)),
        in_progress=Count("id", filter=Q(status=SessionStatus.IN_PROGRESS)),
        submitted=Count("id", filter=Q(status=SessionStatus.SUBMITTED)),
        approved=Count("id", filter=Q(status=SessionStatus.APPROVED)),
        rejected=Count("id", filter=Q(status=SessionStatus.REJECTED)),
        cancelled=Count("id", filter=Q(status=SessionStatus.CANCELLED)),
    )
    return CampaignProgress(**counts)


# ---------------------------------------------------------------------------
# Заполнение сессии оценки
# ---------------------------------------------------------------------------
@transaction.atomic
def save_scores(
    session: AssessmentSession,
    levels: Mapping[int, Decimal | float | None],
    *,
    comments: Mapping[int, str] | None = None,
) -> int:
    """Сохраняет баллы сессии оценки."""
    if not session.is_editable:
        raise AssessmentError(_("Сессия закрыта для изменений"))

    comments = comments or {}
    allowed = set(session.competency_model.requirements.values_list("competency_id", flat=True))
    saved = 0

    for competency_id, level in levels.items():
        if competency_id not in allowed:
            # Оценка по компетенции вне модели молча отбрасывается: состав
            # формы задаёт сервер, и лишнее поле означает подделанный запрос.
            continue

        if level in (None, ""):
            AssessmentScore.objects.filter(session=session, competency_id=competency_id).delete()
            continue

        AssessmentScore.objects.update_or_create(
            session=session,
            competency_id=competency_id,
            defaults={
                "level": Decimal(str(level)),
                # Балл 0–100 пересчитывается моделью при сохранении, поэтому здесь он сбрасывается:
                # сохранённое ранее значение относилось к прежнему уровню.
                "raw_score": None,
                "comment": comments.get(competency_id, ""),
            },
        )
        saved += 1

    if session.status == SessionStatus.PLANNED and saved:
        session.status = SessionStatus.IN_PROGRESS
        session.started_at = session.started_at or timezone.now()
        session.save(update_fields=["status", "started_at", "updated_at"])

    return saved


@transaction.atomic
def submit_session(session: AssessmentSession) -> None:
    """Вносит сессию оценки."""
    if not session.is_editable:
        raise AssessmentError(_("Сессия уже внесена"))
    if not session.is_complete:
        raise AssessmentError(
            _("Заполнены не все компетенции модели: внести можно только полную оценку")
        )

    session.submit()

    invitation = getattr(session, "respondent_invitation", None)
    if invitation is not None and invitation.is_pending:
        invitation.mark_completed()


# ---------------------------------------------------------------------------
# Оценка 360°
# ---------------------------------------------------------------------------
def suggest_respondents(
    campaign: AssessmentCampaign,
    employee: Employee,
    *,
    peers: int = DEFAULT_PEER_COUNT,
    subordinates: int = DEFAULT_SUBORDINATE_COUNT,
    rng: random.Random | None = None,
) -> list[tuple[Employee, str]]:
    """Подбирает состав респондентов опроса 360°."""
    chooser = rng or random.Random()  # noqa: S311  # nosec B311
    result: list[tuple[Employee, str]] = []

    if employee.manager_id and employee.manager and employee.manager.user_id:
        result.append((employee.manager, RespondentRelation.MANAGER))

    if employee.department_id is None:
        # Сотрудник вне подразделения: коллег и подчинённых определить не по чему.
        return result

    colleagues = list(
        Employee.objects.active()
        .internal()
        .filter(department_id=employee.department_id)
        .exclude(pk=employee.pk)
        .exclude(pk=employee.manager_id or 0)
        .exclude(user__isnull=True)
        .select_related("user")
    )
    chooser.shuffle(colleagues)
    result.extend((colleague, RespondentRelation.PEER) for colleague in colleagues[:peers])

    reports = list(
        Employee.objects.active()
        .internal()
        .filter(manager_id=employee.pk)
        .exclude(user__isnull=True)
        .select_related("user")
    )
    chooser.shuffle(reports)
    result.extend((report, RespondentRelation.SUBORDINATE) for report in reports[:subordinates])

    return result


@transaction.atomic
def invite_360(
    campaign: AssessmentCampaign,
    employee: Employee,
    respondents: Sequence[tuple[Employee, str]] | None = None,
    *,
    rng: random.Random | None = None,
) -> list[Assessment360Respondent]:
    """Создаёт приглашения в опрос 360° по сотруднику."""
    candidates = (
        respondents if respondents is not None else suggest_respondents(campaign, employee, rng=rng)
    )

    already = set(
        Assessment360Respondent.objects.filter(campaign=campaign, employee=employee).values_list(
            "respondent_id", flat=True
        )
    )

    created: list[Assessment360Respondent] = []
    for candidate, relation in candidates:
        if candidate.user_id is None or candidate.user_id in already:
            continue
        created.append(
            Assessment360Respondent(
                campaign=campaign,
                employee=employee,
                respondent_id=candidate.user_id,
                relation=relation,
                access_token=secrets.token_urlsafe(36)[:64],
            )
        )
        already.add(candidate.user_id)

    if created:
        Assessment360Respondent.objects.bulk_create(created)
    return created


@transaction.atomic
def open_360_session(invitation: Assessment360Respondent) -> AssessmentSession:
    """Открывает форму респондента, создавая сессию при первом обращении."""
    existing = invitation.session
    if existing is not None:
        return existing

    from apps.competencies.services import model_for_employee

    employee = invitation.employee
    competency_model = model_for_employee(employee) or invitation.campaign.competency_model

    session = AssessmentSession.objects.create(
        campaign=invitation.campaign,
        employee=employee,
        assessor_id=invitation.respondent_id,
        competency_model=competency_model,
        method=AssessmentMethod.ASSESSMENT_360,
        status=SessionStatus.IN_PROGRESS,
        assessment_date=min(timezone.localdate(), invitation.campaign.deadline),
        started_at=timezone.now(),
    )

    invitation.session = session
    invitation.status = RespondentStatus.IN_PROGRESS
    invitation.save(update_fields=["session", "status", "updated_at"])
    return session


@dataclass(frozen=True, slots=True)
class RelationAggregate:
    """Свод оценок 360° по одной категории респондентов."""

    relation: str
    label: str
    weight: Decimal
    responses: int
    invited: int
    average_level: float | None
    is_disclosed: bool
    by_competency: dict[int, float]


def aggregate_360(campaign: AssessmentCampaign, employee: Employee) -> list[RelationAggregate]:
    """Сводит результаты опроса 360° по категориям респондентов."""
    threshold = Assessment360Respondent.MIN_RESPONSES_FOR_DISCLOSURE

    invitations = list(
        Assessment360Respondent.objects.filter(campaign=campaign, employee=employee).values_list(
            "relation", "status", "session_id"
        )
    )
    if not invitations:
        return []

    completed_sessions = [
        session_id
        for _relation, status, session_id in invitations
        if status == RespondentStatus.COMPLETED and session_id is not None
    ]
    scores = list(
        AssessmentScore.objects.counted()
        .filter(session_id__in=completed_sessions)
        .values_list("session_id", "competency_id", "level")
    )
    session_relation = {
        session_id: relation
        for relation, status, session_id in invitations
        if session_id is not None and status == RespondentStatus.COMPLETED
    }

    grouped: dict[str, dict[int, list[float]]] = {}
    for session_id, competency_id, level in scores:
        relation = session_relation.get(session_id)
        if relation is None:
            continue
        grouped.setdefault(relation, {}).setdefault(competency_id, []).append(float(level))

    result: list[RelationAggregate] = []
    for relation, label in RespondentRelation.choices:
        invited = sum(1 for item in invitations if item[0] == relation)
        if not invited:
            continue

        responses = sum(
            1
            for item in invitations
            if item[0] == relation and item[1] == RespondentStatus.COMPLETED
        )
        per_competency = grouped.get(relation, {})
        by_competency = {
            competency_id: round(sum(values) / len(values), 2)
            for competency_id, values in per_competency.items()
        }
        all_levels = [value for values in per_competency.values() for value in values]

        # Самооценка раскрывается всегда: скрывать от сотрудника его собственные ответы
        # бессмысленно, а порог, применённый к ней, выключил бы её навсегда — респондент там ровно
        # один.
        is_disclosed = relation == RespondentRelation.SELF or responses >= threshold

        result.append(
            RelationAggregate(
                relation=relation,
                label=str(label),
                weight=RESPONDENT_WEIGHTS.get(relation, Decimal("0.10")),
                responses=responses,
                invited=invited,
                average_level=(round(sum(all_levels) / len(all_levels), 2) if all_levels else None),
                is_disclosed=is_disclosed,
                by_competency=by_competency if is_disclosed else {},
            )
        )

    return result


def blind_spots(aggregates: Sequence[RelationAggregate]) -> list[dict]:
    """Компетенции с наибольшим расхождением самооценки и мнения окружения."""
    self_view = next(
        (item for item in aggregates if item.relation == RespondentRelation.SELF), None
    )
    if self_view is None or not self_view.by_competency:
        return []

    others = [
        item
        for item in aggregates
        if item.relation != RespondentRelation.SELF and item.is_disclosed
    ]
    if not others:
        return []

    result = []
    for competency_id, own in self_view.by_competency.items():
        external_values = [
            (item.weight, item.by_competency[competency_id])
            for item in others
            if competency_id in item.by_competency
        ]
        if not external_values:
            continue
        total_weight = sum((weight for weight, _ in external_values), Decimal("0"))
        external = float(
            sum((weight * Decimal(str(value)) for weight, value in external_values), Decimal("0"))
            / total_weight
        )
        result.append(
            {
                "competency_id": competency_id,
                "self_level": own,
                "external_level": round(external, 2),
                "delta": round(own - external, 2),
            }
        )

    result.sort(key=lambda item: -abs(item["delta"]))
    return result


# ---------------------------------------------------------------------------
# Встроенное тестирование
# ---------------------------------------------------------------------------
@transaction.atomic
def start_attempt(
    test: Test,
    employee: Employee,
    *,
    session: AssessmentSession | None = None,
) -> TestAttempt:
    """Начинает попытку прохождения теста."""
    if not test.is_ready:
        raise AssessmentError(_("Тест не готов: вопросов меньше, чем требует настройка"))

    unfinished = TestAttempt.objects.filter(
        test=test, employee=employee, status=AttemptStatus.IN_PROGRESS
    ).first()
    if unfinished is not None:
        if unfinished.is_expired:
            finish_attempt(unfinished, expired=True)
        else:
            # Незавершённая попытка возвращается как есть: иначе перезагрузка
            # страницы обнуляла бы ответы и списывала попытку.
            return unfinished

    used = TestAttempt.objects.filter(test=test, employee=employee).exclude(
        status=AttemptStatus.CANCELLED
    )
    if used.count() >= test.max_attempts:
        raise AssessmentError(_("Исчерпано допустимое число попыток"))

    return TestAttempt.objects.create(
        test=test,
        employee=employee,
        session=session,
        attempt_number=used.count() + 1,
        status=AttemptStatus.IN_PROGRESS,
    )


def attempt_questions(
    attempt: TestAttempt, *, rng: random.Random | None = None
) -> list[TestQuestion]:
    """Состав вопросов попытки."""
    test = attempt.test
    questions = list(test.questions.filter(is_active=True).prefetch_related("options"))

    chooser = rng or random.Random(f"{attempt.public_id}")  # noqa: S311  # nosec B311
    if test.shuffle_questions:
        chooser.shuffle(questions)
    else:
        questions.sort(key=lambda question: question.sort_order)

    limit = test.questions_per_attempt or len(questions)
    return questions[:limit]


def question_options(question: TestQuestion, attempt: TestAttempt) -> list:
    """Варианты ответа в порядке, устойчивом внутри попытки."""
    options = list(question.options.all())
    if question.test.shuffle_options:
        chooser = random.Random(f"{attempt.public_id}:{question.pk}")  # noqa: S311  # nosec B311
        chooser.shuffle(options)
    else:
        options.sort(key=lambda option: option.sort_order)
    return options


@transaction.atomic
def save_answer(
    attempt: TestAttempt,
    question: TestQuestion,
    *,
    option_ids: Iterable[int] = (),
    text: str = "",
) -> TestAnswer:
    """Сохраняет ответ на вопрос попытки."""
    if attempt.status != AttemptStatus.IN_PROGRESS:
        raise AssessmentError(_("Попытка завершена"))
    if attempt.is_expired:
        finish_attempt(attempt, expired=True)
        raise AssessmentError(_("Время выполнения теста истекло"))

    answer, _created = TestAnswer.objects.update_or_create(
        attempt=attempt,
        question=question,
        defaults={"text_answer": text},
    )

    valid_ids = [
        option_id
        for option_id in option_ids
        if option_id in set(question.options.values_list("pk", flat=True))
    ]
    answer.selected_options.set(valid_ids)

    if question.is_auto_checked:
        answer.is_correct = _check_answer(question, set(valid_ids))
        answer.awarded_score = Decimal(question.weight) if answer.is_correct else Decimal("0")
        answer.save(update_fields=["is_correct", "awarded_score", "updated_at"])

    return answer


def _check_answer(question: TestQuestion, selected: set[int]) -> bool:
    """Проверяет ответ на вопрос с выбором вариантов."""
    correct = set(question.correct_option_ids)
    if not correct:
        return False
    if question.kind == QuestionKind.SINGLE:
        return len(selected) == 1 and selected == correct
    return selected == correct


def _attempt_score(attempt: TestAttempt) -> Decimal:
    """Балл попытки по вопросам с автоматической проверкой и проверенным развёрнутым ответам."""
    answers = {answer.question_id: answer for answer in attempt.answers.all()}
    counted = [
        question
        for question in attempt_questions(attempt)
        if question.kind in AUTO_CHECKED_KINDS
        or (question.pk in answers and answers[question.pk].checked_by_id is not None)
    ]
    total_weight = sum(question.weight for question in counted)
    earned = sum(
        float(answers[question.pk].awarded_score) for question in counted if question.pk in answers
    )
    if not total_weight:
        return Decimal("0")
    return Decimal(str(round(earned * 100 / total_weight, 2)))


@transaction.atomic
def finish_attempt(attempt: TestAttempt, *, expired: bool = False) -> TestAttempt:
    """Завершает попытку и вычисляет результат."""
    if attempt.status not in {AttemptStatus.IN_PROGRESS}:
        return attempt

    attempt.score = _attempt_score(attempt)
    attempt.is_passed = float(attempt.score) >= attempt.test.passing_score
    attempt.status = AttemptStatus.EXPIRED if expired else AttemptStatus.CHECKED
    attempt.finished_at = timezone.now()
    attempt.save(update_fields=["score", "is_passed", "status", "finished_at", "updated_at"])
    return attempt


@transaction.atomic
def publish_attempt(
    attempt: TestAttempt, *, campaign: AssessmentCampaign | None = None
) -> AssessmentSession:
    """Переносит результат теста в оценку компетенции."""
    if attempt.status not in {AttemptStatus.CHECKED, AttemptStatus.EXPIRED}:
        raise AssessmentError(_("Результат попытки ещё не подсчитан"))

    existing = attempt.session
    if existing is not None:
        session = existing
    else:
        from apps.competencies.services import model_for_employee

        employee = attempt.employee
        competency_model = model_for_employee(employee)
        if competency_model is None:
            raise AssessmentError(_("Для должности сотрудника не задана модель компетенций"))

        session = AssessmentSession.objects.create(
            campaign=campaign,
            employee=employee,
            assessor=None,
            competency_model=competency_model,
            method=AssessmentMethod.TEST,
            status=SessionStatus.SUBMITTED,
            assessment_date=timezone.localdate(),
            submitted_at=timezone.now(),
            summary=str(
                _("Результат теста «%(test)s»: %(score)s из 100")
                % {"test": attempt.test.name, "score": attempt.score}
            ),
        )
        attempt.session = session
        attempt.save(update_fields=["session", "updated_at"])

    AssessmentScore.objects.update_or_create(
        session=session,
        competency=attempt.test.competency,
        defaults={
            "level": AssessmentScore.score_to_level(float(attempt.score or 0)),
            "raw_score": attempt.score,
            "comment": str(
                _("Автоматический подсчёт по попытке №%(number)s")
                % {"number": attempt.attempt_number}
            ),
        },
    )
    return session


def pending_manual_checks(user: User) -> QuerySet[TestAnswer]:
    """Развёрнутые ответы в границах видимости пользователя, ожидающие проверки экспертом."""
    from core.permissions import apply_scope_by_path

    answers = TestAnswer.objects.filter(
        question__kind=QuestionKind.OPEN,
        checked_by__isnull=True,
        attempt__status__in=[AttemptStatus.CHECKED, AttemptStatus.EXPIRED],
    ).exclude(text_answer="")
    return (
        apply_scope_by_path(answers, user, prefix="attempt__employee")
        .select_related("attempt__employee", "attempt__test", "question")
        .order_by("attempt__finished_at")
    )


@transaction.atomic
def check_open_answer(answer: TestAnswer, *, score: Decimal, actor: User) -> TestAnswer:
    """Выставляет экспертный балл за развёрнутый ответ и пересчитывает попытку."""
    question = answer.question
    if question.kind != QuestionKind.OPEN:
        raise AssessmentError(_("Ответ на этот вопрос проверяется автоматически"))
    if not Decimal("0") <= score <= question.weight:
        raise AssessmentError(
            _("Балл должен быть в пределах от 0 до %(weight)s") % {"weight": question.weight}
        )

    answer.awarded_score = score
    answer.is_correct = score * 2 >= question.weight
    answer.checked_by = actor
    answer.save(update_fields=["awarded_score", "is_correct", "checked_by", "updated_at"])

    attempt = answer.attempt
    if attempt.status in {AttemptStatus.CHECKED, AttemptStatus.EXPIRED}:
        attempt.score = _attempt_score(attempt)
        attempt.is_passed = float(attempt.score) >= attempt.test.passing_score
        attempt.save(update_fields=["score", "is_passed", "updated_at"])
        # Результат, уже учтённый как оценка компетенции, следует за пересчётом.
        if attempt.session_id is not None:
            publish_attempt(attempt)
    return answer


# ---------------------------------------------------------------------------
# Сроки
# ---------------------------------------------------------------------------
def campaigns_due_soon(days: int | None = None) -> QuerySet[AssessmentCampaign]:
    """Активные кампании, срок которых наступает в ближайшие дни."""
    horizon = days if days is not None else get_reservix_settings()["ACTIVITY_DUE_SOON_DAYS"]
    today = timezone.localdate()
    return AssessmentCampaign.objects.filter(
        status=CampaignStatus.ACTIVE,
        deadline__gte=today,
        deadline__lte=today + dt.timedelta(days=horizon),
    ).order_by("deadline")


def pending_sessions_for(user: User) -> QuerySet[AssessmentSession]:
    """Сессии, ожидающие заполнения конкретным оценивающим."""
    return (
        AssessmentSession.objects.filter(
            assessor=user,
            status__in=[SessionStatus.PLANNED, SessionStatus.IN_PROGRESS],
            campaign__status=CampaignStatus.ACTIVE,
        )
        .select_related("employee", "campaign", "competency_model")
        .order_by("campaign__deadline", "employee__last_name")
    )
