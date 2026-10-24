"""
Наполнение раздела развития: библиотека, планы, мероприятия, наставничество.

Запуск::

    uv run python manage.py seed_development
    uv run python manage.py seed_development --seed 42
    uv run python manage.py seed_development --flush
"""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.competencies.models import Competency, CompetencyKind
from apps.development.models import (
    ActivityEvidence,
    ActivityStatus,
    DevelopmentActivity,
    DevelopmentPlan,
    EvidenceKind,
    LearningResource,
    Mentorship,
    MentorshipStatus,
    PlanStatus,
    ResourceFormat,
)
from apps.development.services import DevelopmentError, create_plan, fill_plan_from_gaps
from apps.reserve.models import ReserveEntry
from core.constants import Role

#: Доля резервистов, для которых создаётся план развития.
PLAN_SHARE = 0.72

#: Распределение состояний планов. Сумма равна единице; доли подобраны так,
#: чтобы каждое состояние встречалось на стенде, а действующие преобладали.
PLAN_STATE_WEIGHTS: dict[str, float] = {
    PlanStatus.ACTIVE: 0.6,
    PlanStatus.ON_APPROVAL: 0.12,
    PlanStatus.DRAFT: 0.1,
    PlanStatus.COMPLETED: 0.15,
    PlanStatus.CANCELLED: 0.03,
}

#: Шаблоны учебных материалов по типам компетенций.
RESOURCE_PATTERNS: dict[str, list[tuple[str, str, int, int, int]]] = {
    # тип компетенции: (шаблон наименования, формат, часов, уровень от, до)
    CompetencyKind.MANAGERIAL: [
        ("Управленческая мастерская: {name}", ResourceFormat.WORKSHOP, 24, 2, 4),
        ("Программа развития руководителей: {name}", ResourceFormat.PROGRAM, 72, 3, 5),
    ],
    CompetencyKind.PROFESSIONAL: [
        ("Онлайн-курс: {name}", ResourceFormat.COURSE, 32, 1, 3),
        ("Практикум продвинутого уровня: {name}", ResourceFormat.WORKSHOP, 40, 3, 5),
    ],
    CompetencyKind.CORPORATE: [
        ("Корпоративный модуль: {name}", ResourceFormat.COURSE, 16, 1, 3),
        ("Видеокурс: {name}", ResourceFormat.VIDEO, 8, 1, 4),
    ],
    CompetencyKind.PERSONAL: [
        ("Книга и разбор: {name}", ResourceFormat.BOOK, 12, 1, 3),
        ("Практикум: {name}", ResourceFormat.WORKSHOP, 20, 2, 5),
    ],
}

#: Внутренние поставщики обучения. Смесь внутренних и внешних нужна ради
#: показателя доли внутреннего обучения в библиотеке.
PROVIDERS: tuple[tuple[str, bool], ...] = (
    ("Корпоративный университет «Меридиан»", True),
    ("Учебный центр отдела разработки", True),
    ("Академия управления проектами", False),
    ("Платформа профессионального обучения", False),
    ("Школа руководителей", False),
)

#: Цели наставничества. Формулируются от результата, а не от процесса:
#: «сопровождение» целью не является.
MENTORSHIP_GOALS: tuple[str, ...] = (
    "Довести подопечного до уровня самостоятельного ведения проектов "
    "целевой должности к концу срока плана развития.",
    "Передать практику принятия решений в нештатных ситуациях и закрыть "
    "разрыв по управленческим компетенциям.",
    "Подготовить к самостоятельному руководству командой: делегирование, "
    "обратная связь, планирование загрузки.",
    "Ввести в предметную область смежного направления и подтвердить "
    "готовность к горизонтальной ротации.",
)

#: Формулировки результата выполненного мероприятия. Описывают изменение в
#: работе, а не факт участия: именно этого требует форма отчёта.
ACTIVITY_RESULTS: tuple[str, ...] = (
    "Применил освоенный подход при планировании спринта: срок сдачи выдержан "
    "без переработок команды.",
    "Провёл две встречи по обратной связи с сотрудниками самостоятельно, "
    "разбор с руководителем показал корректную структуру разговора.",
    "Подготовил и защитил решение перед смежным подразделением, замечания устранены до внедрения.",
    "Взял на сопровождение направление на время отсутствия руководителя, инцидентов не допущено.",
)


class Command(BaseCommand):
    """Наполняет раздел развития демонстрационными данными."""

    help = "Создаёт библиотеку развития, планы, мероприятия и пары наставничества"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--seed", type=int, default=None, help="значение генератора")
        parser.add_argument(
            "--flush", action="store_true", help="удалить существующие данные раздела"
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        self.random = random.Random(options["seed"])  # noqa: S311  # nosec B311

        if options["flush"]:
            self._flush()

        resources = self._create_library()
        self.stdout.write(f"Библиотека развития: {resources} материалов")

        plans, activities = self._create_plans()
        self.stdout.write(f"Планы развития: {plans}, мероприятий: {activities}")

        evidences = self._create_evidence()
        self.stdout.write(f"Подтверждений выполнения: {evidences}")

        pairs = self._create_mentorships()
        self.stdout.write(f"Пар наставничества: {pairs}")

        self.stdout.write(self.style.SUCCESS("Раздел развития наполнен"))

    # -- Очистка -------------------------------------------------------------
    def _flush(self) -> None:
        ActivityEvidence.objects.all().delete()
        DevelopmentActivity.objects.all().delete()
        Mentorship.objects.all().delete()
        DevelopmentPlan.objects.all().delete()
        LearningResource.objects.all().delete()
        self.stdout.write(self.style.WARNING("Данные раздела развития удалены"))

    # -- Библиотека ----------------------------------------------------------
    def _create_library(self) -> int:
        """Создаёт учебные материалы под компетенции модели."""
        created = 0
        for competency in Competency.objects.filter(is_active=True):
            patterns = RESOURCE_PATTERNS.get(competency.kind, [])
            for template, resource_format, hours, level_min, level_max in patterns:
                provider, is_internal = self.random.choice(PROVIDERS)
                resource, is_new = LearningResource.objects.get_or_create(
                    code=f"lr-{competency.code}-{resource_format}"[:48],
                    defaults={
                        "name": template.format(name=competency.name)[:255],
                        "description": (
                            f"Материал развивает компетенцию «{competency.name}» "
                            f"до уровня {level_max} по корпоративной шкале. "
                            f"{competency.description[:180]}"
                        ),
                        "resource_format": resource_format,
                        "provider": provider,
                        "is_internal": is_internal,
                        "duration_hours": hours,
                        "target_level_min": level_min,
                        "target_level_max": level_max,
                        "cost": Decimal("0")
                        if is_internal
                        else Decimal(self.random.randrange(12000, 90000, 1000)),
                        "rating": Decimal(str(round(self.random.uniform(3.4, 4.9), 1))),
                    },
                )
                if is_new:
                    resource.competencies.add(competency)
                    created += 1
        return created

    # -- Планы ---------------------------------------------------------------
    def _create_plans(self) -> tuple[int, int]:
        """Создаёт планы развития резервистов и наполняет их по разрывам."""
        from apps.accounts.models import User

        author = User.objects.filter(role=Role.HR, is_active=True).first()
        if author is None:
            self.stdout.write(self.style.WARNING("Нет пользователя HR — планы не создаются"))
            return 0, 0

        entries = list(ReserveEntry.objects.in_reserve().with_related())
        self.random.shuffle(entries)
        selected = entries[: int(len(entries) * PLAN_SHARE)]

        states = list(PLAN_STATE_WEIGHTS)
        weights = [PLAN_STATE_WEIGHTS[state] for state in states]

        plans = activities = 0
        today = timezone.localdate()

        for entry in selected:
            # Период плана смещается назад, чтобы на стенде были и свежие планы, и такие, срок
            # которых уже истекает: просрочка мероприятий иначе не возникла бы ни у одного плана.
            offset = self.random.randint(0, 300)
            start = today - dt.timedelta(days=offset)

            try:
                plan = create_plan(
                    entry.employee,
                    actor=author,
                    entry=entry,
                    start_date=start,
                    supervisor=self._supervisor_for(entry),
                )
            except DevelopmentError:
                # У сотрудника уже есть незакрытый план: правило модуля,
                # а не сбой наполнения.
                continue

            created = fill_plan_from_gaps(plan, actor=author)
            activities += created
            plans += 1

            state = self.random.choices(states, weights=weights, k=1)[0]
            self._apply_state(plan, state, author)

        return plans, activities

    def _supervisor_for(self, entry: ReserveEntry):
        """Куратор плана: руководитель сотрудника, иначе — кадровая служба."""
        manager = entry.employee.manager
        if manager is not None and manager.user_id:
            return manager.user

        from apps.accounts.models import User

        return User.objects.filter(role=Role.HR, is_active=True).first()

    def _apply_state(self, plan: DevelopmentPlan, state: str, author) -> None:
        """Приводит план и его мероприятия в заданное состояние."""
        today = timezone.localdate()

        if state == PlanStatus.DRAFT:
            return

        plan.status = state
        if state in {PlanStatus.ACTIVE, PlanStatus.COMPLETED}:
            plan.approved_by = author
            plan.approved_at = timezone.now()

        if state == PlanStatus.CANCELLED:
            plan.summary = "План отменён: сотрудник переведён на другое направление."
        plan.save()

        if state in {PlanStatus.DRAFT, PlanStatus.ON_APPROVAL, PlanStatus.CANCELLED}:
            return

        for activity in plan.activities.all():
            if activity.due_date > today:
                if self.random.random() < 0.35:
                    activity.status = ActivityStatus.IN_PROGRESS
                    activity.save(update_fields=["status", "updated_at"])
                continue

            # Срок прошёл: выполнено либо просрочено.
            if self.random.random() < 0.72:
                activity.status = ActivityStatus.COMPLETED
                activity.completion_date = activity.due_date - dt.timedelta(
                    days=self.random.randint(0, 10)
                )
                activity.result = self.random.choice(ACTIVITY_RESULTS)
                activity.save(update_fields=["status", "completion_date", "result", "updated_at"])

        plan.recalculate_completion()

        if state == PlanStatus.COMPLETED:
            plan.summary = (
                "План выполнен. Закрыты разрывы по компетенциям, вынесенным в приоритет; "
                "по двум сохраняется отставание в пределах половины уровня — "
                "перенесены в следующий цикл."
            )
            plan.save(update_fields=["summary", "updated_at"])

    # -- Подтверждения --------------------------------------------------------
    def _create_evidence(self) -> int:
        """Прикладывает подтверждения к части выполненных мероприятий."""
        from apps.accounts.models import User

        verifier = User.objects.filter(role=Role.MENTOR, is_active=True).first()
        completed = list(
            DevelopmentActivity.objects.filter(status=ActivityStatus.COMPLETED).select_related(
                "plan__employee__user"
            )
        )

        created = 0
        for activity in completed:
            if self.random.random() > 0.7:
                continue

            author = activity.plan.employee.user or verifier
            if author is None:
                continue

            evidence = ActivityEvidence.objects.create(
                activity=activity,
                kind=self.random.choice(
                    [EvidenceKind.CERTIFICATE, EvidenceKind.REPORT, EvidenceKind.FEEDBACK]
                ),
                title=f"Подтверждение: {activity.title}"[:255],
                external_url=f"https://learning.meridian-soft.ru/certificates/{activity.public_id}",
                comment="Демонстрационное подтверждение: ссылка на запись во внутренней "
                "системе обучения.",
                submitted_by=author,
            )
            created += 1

            # Часть подтверждений оставлена непроверенной: очередь наставника
            # на странице обратной связи не должна быть пустой.
            if verifier is not None and verifier.pk != author.pk and self.random.random() < 0.6:
                evidence.is_verified = True
                evidence.verified_by = verifier
                evidence.verified_at = timezone.now()
                evidence.save(update_fields=["is_verified", "verified_by", "verified_at"])

        return created

    # -- Наставничество --------------------------------------------------------
    def _create_mentorships(self) -> int:
        """Создаёт пары наставничества для части резервистов с планами."""
        from apps.accounts.models import User

        mentors = list(User.objects.filter(role__in=[Role.MENTOR, Role.MANAGER], is_active=True))
        if not mentors:
            return 0

        plans = list(
            DevelopmentPlan.objects.filter(
                status__in=[PlanStatus.ACTIVE, PlanStatus.COMPLETED]
            ).select_related("employee__user")
        )
        self.random.shuffle(plans)

        created = 0
        today = timezone.localdate()

        for plan in plans[: int(len(plans) * 0.55)]:
            mentor = self.random.choice(mentors)
            if plan.employee.user_id == mentor.pk:
                continue
            if Mentorship.objects.filter(
                mentor=mentor,
                mentee=plan.employee,
                status__in=[
                    MentorshipStatus.PROPOSED,
                    MentorshipStatus.ACTIVE,
                    MentorshipStatus.PAUSED,
                ],
            ).exists():
                continue

            frequency = self.random.choice([14, 21, 30])
            meetings = self.random.randint(0, 12)
            # Часть пар намеренно оставлена с просроченной встречей: список
            # «требуют встречи» на рабочем месте наставника обязан работать.
            last_meeting = (
                None
                if meetings == 0
                else today - dt.timedelta(days=self.random.randint(1, frequency * 2))
            )

            is_completed = plan.status == PlanStatus.COMPLETED
            Mentorship.objects.create(
                mentor=mentor,
                mentee=plan.employee,
                plan=plan,
                status=MentorshipStatus.COMPLETED if is_completed else MentorshipStatus.ACTIVE,
                goal=self.random.choice(MENTORSHIP_GOALS),
                start_date=plan.start_date,
                end_date=plan.end_date,
                actual_end_date=plan.end_date if is_completed else None,
                meeting_frequency_days=frequency,
                last_meeting_at=last_meeting,
                meetings_count=meetings,
                result=(
                    "Подопечный доведён до самостоятельного ведения задач целевой "
                    "должности; рекомендован к рассмотрению на ближайшей комиссии."
                    if is_completed
                    else ""
                ),
                effectiveness=self.random.randint(3, 5) if is_completed else None,
            )
            created += 1

        return created
