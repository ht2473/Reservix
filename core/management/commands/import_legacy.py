"""
Перенос данных исходной базы данных в модель системы.

Запуск::

    uv run python manage.py import_legacy
    uv run python manage.py import_legacy --dry-run    # разбор без записи
    uv run python manage.py import_legacy --flush      # с очисткой перенесённого
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.assessment.models import (
    AssessmentMethod,
    AssessmentScore,
    AssessmentSession,
    SessionStatus,
)
from apps.competencies.models import (
    Competency,
    CompetencyCategory,
    CompetencyKind,
    CompetencyModel,
    CompetencyRequirement,
)
from apps.development.models import (
    ActivityStatus,
    ActivityType,
    DevelopmentActivity,
    DevelopmentPlan,
    PlanStatus,
)
from apps.org.models import Branch, Department, Position
from apps.people.models import (
    EducationLevel,
    Employee,
    EmploymentChange,
    EmploymentHistory,
)
from apps.reserve.models import ReserveEntry, ReserveStatus, ReserveStatusHistory
from core.constants import ReadinessLevel, Role, ScopeLevel
from core.legacy import (
    LegacyDumpError,
    as_bool,
    as_date,
    as_datetime,
    as_decimal,
    as_int,
    as_text,
    parse_dump,
)
from core.utils import slugify_ru

#: Путь к выгрузке исходной базы относительно корня проекта.
DEFAULT_DUMP = Path("db") / "cadr_reserve.sql"

#: Таблицы исходной схемы в порядке переноса: справочники, затем зависимые.
LEGACY_TABLES = [
    "branch",
    "department",
    "User",
    "candidate",
    "reserve",
    "reservestatushistory",
    "assessment",
    "developmentplan",
    "developmentactivity",
]

#: Домен почты организации.
ORG_DOMAIN = "meridian-soft.ru"

#: Пароль перенесённых учётных записей.
IMPORT_PASSWORD = "Reserv-Demo-2026!"  # noqa: S105  # nosec B105

#: Соответствие целевых должностей резерва и наименований в карточках сотрудников штатному
#: расписанию.
POSITION_MAP: dict[str, str] = {
    # --- Целевые должности резерва -----------------------------------------
    "Senior Developer": "senior-dev",
    "Middle Developer": "middle-dev",
    "Junior Developer": "junior-dev",
    "Tech Lead": "tech-lead",
    "Tech Lead NSK": "tech-lead-nsk",
    "Team Lead": "team-lead-dev",
    "Team Lead Sales": "team-lead-sales",
    "Head of Sales": "head-of-sales",
    "Head of Sales SPb": "head-of-sales-spb",
    "Sales Director EKB": "sales-director-ekb",
    "Senior Manager": "senior-sales",
    "Marketing Manager": "marketing-manager",
    "Marketing Director": "marketing-director",
    "SMM Lead": "smm-lead",
    "Senior Sysadmin": "senior-sysadmin",
    "Senior Analyst": "senior-analyst",
    "Chief Accountant": "chief-accountant",
    "Logistics Manager": "logistics-manager",
    "Logistics Director": "logistics-director",
    "Product Manager": "product-manager",
    "HR Director": "head-of-hr",
    "HRBP": "hr-bp",
    "DevOps Engineer": "devops-engineer",
    "UX/UI Lead": "ux-lead",
    "UX Designer": "ux-designer",
    "Data Analyst": "data-analyst",
    "Legal Counsel": "lawyer",
    "Head of Legal": "head-of-legal",
    "Finance Director": "cfo",
    "CFO": "cfo",
    "Finance Analyst": "financial-analyst",
    "IT Manager SPb": "it-manager-spb",
    "AHO Manager": "aho-manager",
    "Security Chief": "security-chief",
    "Warehouse Manager": "warehouse-manager",
    "Warehouse Chief EKB": "warehouse-chief-ekb",
    "Procurement Manager": "procurement-manager",
    "Branch Director SPb": "branch-director",
    # --- Текущие должности карточек ----------------------------------------
    "Менеджер": "sales-manager",
    "Менеджер по продажам": "sales-manager",
    "Старший менеджер": "senior-sales",
    "Маркетолог": "marketing-manager",
    "SMM-менеджер": "smm-manager",
    "Сисадмин": "sysadmin",
    "Логист": "logistician",
    "Кладовщик": "storekeeper",
    "Складской менеджер": "warehouse-manager",
    "Заведующий складом": "warehouse-manager",
    "Снабженец": "supply-specialist",
    "Бухгалтер": "accountant",
    "Главный бухгалтер": "chief-accountant",
    "Финансовый аналитик": "financial-analyst",
    "Финансовый директор": "cfo",
    "Юрист": "lawyer",
    "Инженер": "engineer",
    "Специалист по ОТ": "labor-safety-specialist",
    "Директор филиала": "branch-director",
}

#: Наименования должностей, означающие внешнего кандидата.
EXTERNAL_POSITION_MARKERS = ("(", ")")


#: Соответствие окончания строки образования уровню в новой модели. Исходная
#: база хранила образование одной строкой вида «МГТУ им. Баумана, магистр».
EDUCATION_MARKERS: tuple[tuple[str, str], ...] = (
    ("phd", EducationLevel.POSTGRADUATE),
    ("аспирант", EducationLevel.POSTGRADUATE),
    ("магистр", EducationLevel.MASTER),
    ("специалист", EducationLevel.SPECIALIST),
    ("бакалавр", EducationLevel.BACHELOR),
    ("колледж", EducationLevel.VOCATIONAL),
    ("техникум", EducationLevel.VOCATIONAL),
)

#: Соответствие статусов записи резерва.
RESERVE_STATUS_MAP: dict[str, str] = {
    "active": ReserveStatus.ACTIVE,
    "inactive": ReserveStatus.INACTIVE,
    "appointed": ReserveStatus.APPOINTED,
}

#: Соответствие уровней готовности.
READINESS_MAP: dict[str, str] = {
    "low": ReadinessLevel.LOW,
    "medium": ReadinessLevel.MEDIUM,
    "high": ReadinessLevel.HIGH,
}

#: Соответствие статусов плана развития.
PLAN_STATUS_MAP: dict[str, str] = {
    "draft": PlanStatus.DRAFT,
    "active": PlanStatus.ACTIVE,
    "completed": PlanStatus.COMPLETED,
    "cancelled": PlanStatus.CANCELLED,
}

#: Соответствие видов мероприятий развития.
ACTIVITY_TYPE_MAP: dict[str, str] = {
    "training": ActivityType.TRAINING,
    "mentoring": ActivityType.MENTORING,
    "task": ActivityType.TASK,
    "course": ActivityType.COURSE,
    "conference": ActivityType.CONFERENCE,
}

#: Соответствие состояний мероприятий развития.
ACTIVITY_STATUS_MAP: dict[str, str] = {
    "pending": ActivityStatus.PENDING,
    "in_progress": ActivityStatus.IN_PROGRESS,
    "completed": ActivityStatus.COMPLETED,
    "overdue": ActivityStatus.OVERDUE,
}

#: Соответствие методов оценки.
ASSESSMENT_METHOD_MAP: dict[str, str] = {
    "test": AssessmentMethod.TEST,
    "interview": AssessmentMethod.INTERVIEW,
    "360": AssessmentMethod.ASSESSMENT_360,
    "exam": AssessmentMethod.EXAM,
}

#: Код категории, в которую помещаются компетенции, восстановленные из строк
#: `assessment.competency_name`.
LEGACY_CATEGORY_CODE = "legacy-imported"

#: Код модели компетенций, объединяющей перенесённые компетенции.
LEGACY_MODEL_CODE = "legacy-imported-model"

#: Целевой уровень требований перенесённой модели компетенций.
LEGACY_TARGET_LEVEL = 4


class Command(BaseCommand):
    """Переносит данные исходной базы данных в модель системы."""

    help = "Переносит данные выгрузки db/cadr_reserve.sql в модель системы"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--path",
            type=str,
            default=str(DEFAULT_DUMP),
            help="путь к выгрузке исходной базы",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="разобрать выгрузку и показать отчёт, не записывая данные",
        )
        parser.add_argument(
            "--flush",
            action="store_true",
            help="удалить ранее перенесённые записи перед переносом",
        )

    # -- Основной ход -------------------------------------------------------
    def handle(self, *args, **options) -> None:
        """Выполняет перенос целиком в одной транзакции."""
        path = Path(options["path"])
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / path

        try:
            tables = parse_dump(path, LEGACY_TABLES)
        except LegacyDumpError as exc:
            raise CommandError(str(exc)) from exc

        self.stats: dict[str, int] = {}
        self.warnings: list[str] = []
        self.timezone = timezone.get_default_timezone()

        self.stdout.write(f"Выгрузка прочитана: {path}")
        for name, table in tables.items():
            self.stdout.write(f"  {name}: {len(table)} строк")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("\nРежим разбора: данные не записываются."))
            self._report_mapping(tables)
            return

        with transaction.atomic():
            if options["flush"]:
                self._flush()

            self._require_reference_data()

            users = self._import_users(tables["User"].rows)
            reserve_targets = {
                int(row["candidate_id"]): as_text(row["target_position"])
                for row in tables["reserve"].rows
                if row["candidate_id"]
            }
            employees = self._import_employees(tables["candidate"].rows, users, reserve_targets)
            entries = self._import_reserve(tables["reserve"].rows, employees, users)
            self._import_status_history(tables["reservestatushistory"].rows, entries, users)
            self._import_assessments(tables["assessment"].rows, employees, users)
            plans = self._import_plans(tables["developmentplan"].rows, entries, users)
            self._import_activities(tables["developmentactivity"].rows, plans, users)

        self._report()

    # -- Подготовка ---------------------------------------------------------
    def _require_reference_data(self) -> None:
        """Проверяет, что справочники наполнены."""
        if not Position.objects.exists() or not Department.objects.exists():
            raise CommandError(
                "Справочники организационной структуры пусты. "
                "Выполните: uv run python manage.py seed_org"
            )

    def _flush(self) -> None:
        """Удаляет ранее перенесённые записи, распознавая их по `legacy_id`."""
        counts = {
            "мероприятий развития": DevelopmentActivity.objects.filter(
                legacy_id__isnull=False
            ).count(),
            "планов развития": DevelopmentPlan.objects.filter(legacy_id__isnull=False).count(),
            "оценок": AssessmentScore.objects.filter(legacy_id__isnull=False).count(),
            "записей резерва": ReserveEntry.objects.filter(legacy_id__isnull=False).count(),
            "карточек сотрудников": Employee.all_objects.filter(legacy_id__isnull=False).count(),
        }

        DevelopmentActivity.objects.filter(legacy_id__isnull=False).delete()
        DevelopmentPlan.objects.filter(legacy_id__isnull=False).delete()
        AssessmentScore.objects.filter(legacy_id__isnull=False).delete()
        AssessmentSession.objects.filter(campaign__isnull=True).delete()
        ReserveStatusHistory.objects.filter(legacy_id__isnull=False).delete()
        ReserveEntry.objects.filter(legacy_id__isnull=False).delete()
        Employee.all_objects.filter(legacy_id__isnull=False).delete()

        summary = ", ".join(f"{name}: {value}" for name, value in counts.items() if value)
        if summary:
            self.stdout.write(self.style.WARNING(f"Удалено — {summary}"))

    # -- Учётные записи -----------------------------------------------------
    def _import_users(self, rows: list[dict[str, Any]]) -> dict[int, User]:
        """Переносит учётные записи, объединяя их с существующими по адресу почты."""
        branches = {branch.city: branch for branch in Branch.objects.all()}
        departments = {dept.name: dept for dept in Department.objects.all()}
        legacy_branches = self._legacy_branch_names()
        legacy_departments = self._legacy_department_names()

        created = 0
        linked = 0
        result: dict[int, User] = {}

        for row in rows:
            legacy_id = int(row["id"])
            email = self._normalize_email(row["email"])
            role = self._map_role(as_text(row["role"]))

            branch_name = legacy_branches.get(as_int(row["branch_id"]) or 0)
            department_name = legacy_departments.get(as_int(row["department_id"]) or 0)
            branch = branches.get(branch_name or "")
            department = departments.get(department_name or "")

            user = User.objects.filter(email=email).first()
            if user is None:
                user = User(email=email)
                created += 1
                user.set_password(IMPORT_PASSWORD)
                user.must_change_password = True
            else:
                linked += 1

            user.last_name = as_text(row["last_name"])
            user.first_name = as_text(row["first_name"])
            user.middle_name = as_text(row["middle_name"])
            user.phone = as_text(row["phone"])
            user.is_active = as_bool(row["is_active"]) if row["is_active"] is not None else True
            user.is_department_head = as_bool(row["is_department_head"])

            # Роль и права существующей учётной записи не понижаются: у демонстрационного
            # администратора роль назначена осознанно, и перенос не должен её переписывать данными
            # исходной базы.
            if user.pk is None:
                user.role = role
                user.scope_level = self._scope_for(role, user.is_department_head)

            if branch is not None:
                user.branch = branch
            if department is not None:
                user.department = department
                user.branch = department.branch
            elif user.is_department_head:
                # Признак руководителя без подразделения нарушает ограничение модели:
                # интерпретировать такую запись при согласовании заявок невозможно.
                user.is_department_head = False
                self.warnings.append(
                    f"Учётная запись {email}: снят признак руководителя — "
                    "подразделение в исходной базе не указано"
                )

            user.save()
            result[legacy_id] = user

        self.stats["Учётных записей создано"] = created
        self.stats["Учётных записей объединено с существующими"] = linked
        return result

    def _map_role(self, legacy_role: str) -> str:
        """Роль исходной базы. Перечни совпадают, отображение объявлено явно."""
        mapping = {
            "admin": Role.ADMIN,
            "hr": Role.HR,
            "manager": Role.MANAGER,
            "employee": Role.EMPLOYEE,
        }
        return mapping.get(legacy_role, Role.EMPLOYEE)

    def _scope_for(self, role: str, is_head: bool) -> str:
        """Область видимости, соответствующая роли."""
        if role == Role.ADMIN or role == Role.HR:
            return ScopeLevel.ORGANIZATION
        if role == Role.MANAGER or is_head:
            return ScopeLevel.SUBORDINATES
        return ScopeLevel.SELF

    def _normalize_email(self, value: str | None) -> str:
        """Приводит адрес к домену организации, сохраняя локальную часть."""
        address = (value or "").strip().lower()
        if not address:
            return ""
        local = address.split("@", 1)[0]
        return f"{local}@{ORG_DOMAIN}"

    # -- Карточки сотрудников -----------------------------------------------
    def _import_employees(
        self,
        rows: list[dict[str, Any]],
        users: dict[int, User],
        reserve_targets: dict[int, str],
    ) -> dict[int, Employee]:
        """Переносит карточки кандидатов в карточки сотрудников."""
        positions = {
            position.code: position
            for position in Position.objects.select_related(
                "department", "department__branch", "grade"
            )
        }
        legacy_branches = self._legacy_branch_names()
        legacy_departments = self._legacy_department_names()
        branches = {branch.city: branch for branch in Branch.objects.all()}
        departments = {dept.name: dept for dept in Department.objects.all()}
        head_office = Branch.objects.filter(is_head_office=True).first()
        users_by_email = {user.email: user for user in users.values()}

        restored_org = 0
        external = 0
        result: dict[int, Employee] = {}

        for row in rows:
            legacy_id = int(row["id"])
            raw_email = as_text(row["email"]).lower()
            email = self._normalize_email(raw_email)
            linked_user = users_by_email.get(email)

            title = as_text(row["current_position"])

            # Внешним признаётся только тот кандидат, чьё место работы названо прямо.
            is_external = self._looks_external(title)
            position = None if is_external else self._resolve_position(title, positions)

            branch = branches.get(legacy_branches.get(as_int(row["branch_id"]) or 0) or "")
            department = departments.get(
                legacy_departments.get(as_int(row["department_id"]) or 0) or ""
            )

            if is_external:
                # Внешний кандидат не входит в штат: подставлять ему подразделение организации
                # нельзя — это исказило бы и численность подразделения, и область видимости
                # руководителя.
                branch = None
                department = None
                external += 1
            else:
                if department is None:
                    # Восстановление привязки по цепочке: учётная запись того же человека →
                    # подразделение текущей должности → подразделение должности, на которую
                    # кандидат выдвинут в резерв.
                    target = self._resolve_position(reserve_targets.get(legacy_id, ""), positions)
                    department = self._restore_department(linked_user, position, target)
                    if department is not None:
                        restored_org += 1
                if department is not None:
                    branch = department.branch
                if branch is None:
                    branch = head_office

                if department is None:
                    self.warnings.append(
                        f"Карточка {legacy_id}: подразделение не восстановлено, запись пропущена"
                    )
                    continue

            hire_date = as_date(row["hire_date"]) or timezone.localdate()
            employee, _ = Employee.all_objects.update_or_create(
                legacy_id=legacy_id,
                defaults={
                    "user": None if is_external else linked_user,
                    "personnel_number": f"E-{legacy_id:04d}",
                    "last_name": as_text(row["last_name"]),
                    "first_name": as_text(row["first_name"]),
                    "middle_name": as_text(row["middle_name"]),
                    "birth_date": as_date(row["birth_date"]),
                    "email": raw_email if is_external else email,
                    "phone": as_text(row["phone"]),
                    "is_external": is_external,
                    "education": as_text(row["education"]),
                    "education_level": self._resolve_education(as_text(row["education"])),
                    "branch": branch,
                    "department": department,
                    "position": position,
                    "position_title": title if position is None else "",
                    "hire_date": hire_date,
                    "position_since": hire_date,
                    "notes": as_text(row["notes"]),
                    "prior_experience_months": self._estimate_prior_experience(
                        as_date(row["birth_date"]), hire_date
                    ),
                },
            )

            EmploymentHistory.objects.update_or_create(
                employee=employee,
                end_date__isnull=True,
                defaults={
                    "position": position,
                    "position_title": title or (position.name if position else "Не указана"),
                    "department": department,
                    "branch": branch,
                    "grade_level": position.grade.level if position else None,
                    "change_type": EmploymentChange.HIRE,
                    "start_date": hire_date,
                },
            )

            result[legacy_id] = employee

        self.stats["Карточек сотрудников перенесено"] = len(result)
        self.stats["Привязка к оргструктуре восстановлена"] = restored_org
        self.stats["Внешних кандидатов"] = external
        return result

    def _resolve_position(self, title: str, positions: dict[str, Position]) -> Position | None:
        """Сопоставляет наименование должности со штатным расписанием."""
        if not title:
            return None
        code = POSITION_MAP.get(title)
        if code is not None:
            position = positions.get(code)
            if position is None:
                self.warnings.append(f"Должность с кодом «{code}» отсутствует в справочнике")
            return position

        # Наименование не найдено в таблице соответствия: сопоставление по
        # приведённому коду — последняя попытка перед сохранением текстом.
        fallback = positions.get(slugify_ru(title))
        if fallback is None and not self._looks_external(title):
            self.warnings.append(f"Должность «{title}» не сопоставлена со штатным расписанием")
        return fallback

    def _looks_external(self, title: str) -> bool:
        """Признак должности во внешней организации: «Senior Dev (Yandex)»."""
        return all(marker in title for marker in EXTERNAL_POSITION_MARKERS)

    def _restore_department(self, *candidates: User | Position | None) -> Department | None:
        """Возвращает первое непустое подразделение из переданных источников."""
        for source in candidates:
            if source is not None and getattr(source, "department_id", None):
                return source.department
        return None

    def _resolve_education(self, education: str) -> str:
        """Определяет уровень образования по строке исходной базы."""
        lowered = education.lower()
        for marker, level in EDUCATION_MARKERS:
            if marker in lowered:
                return level
        return EducationLevel.BACHELOR

    def _estimate_prior_experience(self, birth_date: dt.date | None, hire_date: dt.date) -> int:
        """Оценивает стаж до приёма на работу."""
        if birth_date is None:
            return 0
        age_at_hire = hire_date.year - birth_date.year
        return max(0, (age_at_hire - 22) * 12)

    # -- Кадровый резерв ----------------------------------------------------
    def _import_reserve(
        self,
        rows: list[dict[str, Any]],
        employees: dict[int, Employee],
        users: dict[int, User],
    ) -> dict[int, ReserveEntry]:
        """Переносит записи резерва, связывая целевые должности со справочником."""
        positions = {position.code: position for position in Position.objects.all()}
        result: dict[int, ReserveEntry] = {}
        unmatched = 0

        for row in rows:
            legacy_id = int(row["id"])
            employee = employees.get(as_int(row["candidate_id"]) or 0)
            if employee is None:
                self.warnings.append(f"Запись резерва {legacy_id}: карточка кандидата не найдена")
                continue

            title = as_text(row["target_position"])
            position = self._resolve_position(title, positions)
            if position is None:
                unmatched += 1
                self.warnings.append(
                    f"Запись резерва {legacy_id}: должность «{title}» не сопоставлена"
                )
                continue

            inclusion_date = as_date(row["inclusion_date"]) or timezone.localdate()
            entry, _ = ReserveEntry.objects.update_or_create(
                legacy_id=legacy_id,
                defaults={
                    "employee": employee,
                    "target_position": position,
                    "status": RESERVE_STATUS_MAP.get(as_text(row["status"]), ReserveStatus.ACTIVE),
                    "readiness_level": READINESS_MAP.get(
                        as_text(row["readiness_level"]), ReadinessLevel.LOW
                    ),
                    "inclusion_date": inclusion_date,
                    "exclusion_date": as_date(row["exclusion_date"]),
                    "exclusion_reason": as_text(row["exclusion_reason"]),
                    # Срок пересмотра исходная база не содержала. Он назначается
                    # через год после включения: без даты пересмотра запись
                    # резерва не попадает ни в один рабочий список.
                    "review_date": inclusion_date + dt.timedelta(days=365),
                    "created_by": users.get(as_int(row["created_by"]) or 0),
                },
            )
            result[legacy_id] = entry

        self.stats["Записей резерва перенесено"] = len(result)
        if unmatched:
            self.stats["Записей резерва без сопоставленной должности"] = unmatched
        return result

    def _import_status_history(
        self,
        rows: list[dict[str, Any]],
        entries: dict[int, ReserveEntry],
        users: dict[int, User],
    ) -> None:
        """Переносит историю статусов."""
        imported = 0

        for row in rows:
            legacy_id = int(row["id"])
            entry = entries.get(as_int(row["reserve_id"]) or 0)
            if entry is None:
                continue

            record, _created = ReserveStatusHistory.objects.update_or_create(
                legacy_id=legacy_id,
                defaults={
                    "entry": entry,
                    "old_status": RESERVE_STATUS_MAP.get(as_text(row["old_status"]), ""),
                    "new_status": RESERVE_STATUS_MAP.get(
                        as_text(row["new_status"]), ReserveStatus.ACTIVE
                    ),
                    "changed_by": users.get(as_int(row["changed_by"]) or 0),
                    "reason": as_text(row["reason"]),
                },
            )
            changed_at = as_datetime(row["changed_at"], tz=self.timezone)
            if changed_at is not None:
                ReserveStatusHistory.objects.filter(pk=record.pk).update(changed_at=changed_at)
            imported += 1

        self.stats["Записей истории статусов перенесено"] = imported

    # -- Оценки -------------------------------------------------------------
    def _import_assessments(
        self,
        rows: list[dict[str, Any]],
        employees: dict[int, Employee],
        users: dict[int, User],
    ) -> None:
        """Переносит оценки, восстанавливая справочник компетенций и процедуру."""
        category, _ = CompetencyCategory.objects.get_or_create(
            code=LEGACY_CATEGORY_CODE,
            defaults={
                "name": "Перенесённые из исходной базы",
                "description": (
                    "Компетенции, восстановленные из текстового поля "
                    "assessment.competency_name исходной схемы. Подлежат "
                    "нормализации и переносу в корпоративную модель компетенций."
                ),
                "kind": CompetencyKind.PROFESSIONAL,
                "sort_order": 900,
            },
        )

        competencies = self._ensure_competencies(rows, category)
        model = self._ensure_legacy_model(competencies)

        sessions: dict[tuple[int, str, str, int], AssessmentSession] = {}
        imported = 0

        for row in rows:
            employee = employees.get(as_int(row["candidate_id"]) or 0)
            if employee is None:
                continue

            method = ASSESSMENT_METHOD_MAP.get(
                as_text(row["assessment_method"]), AssessmentMethod.EXPERT
            )
            assessed_on = as_date(row["assessment_date"]) or timezone.localdate()
            assessor = users.get(as_int(row["assessed_by"]) or 0)
            key = (employee.pk, method, assessed_on.isoformat(), assessor.pk if assessor else 0)

            session = sessions.get(key)
            if session is None:
                session = AssessmentSession.objects.create(
                    employee=employee,
                    assessor=assessor,
                    competency_model=model,
                    method=method,
                    status=SessionStatus.APPROVED,
                    assessment_date=assessed_on,
                    submitted_at=timezone.now(),
                    approved_at=timezone.now(),
                    summary="Перенесено из исходной базы данных",
                )
                sessions[key] = session

            competency = competencies[as_text(row["competency_name"])]
            raw_score = as_decimal(row["score"]) or Decimal("0")
            AssessmentScore.objects.update_or_create(
                legacy_id=int(row["id"]),
                defaults={
                    "session": session,
                    "competency": competency,
                    "level": AssessmentScore.score_to_level(float(raw_score)),
                    "raw_score": raw_score,
                    "comment": as_text(row["comment"]),
                },
            )
            imported += 1

        self.stats["Компетенций восстановлено"] = len(competencies)
        self.stats["Сессий оценки создано"] = len(sessions)
        self.stats["Оценок перенесено"] = imported

    def _ensure_competencies(
        self, rows: list[dict[str, Any]], category: CompetencyCategory
    ) -> dict[str, Competency]:
        """Создаёт компетенции по наименованиям, встретившимся в оценках."""
        names = sorted({as_text(row["competency_name"]) for row in rows if row["competency_name"]})
        result: dict[str, Competency] = {}

        for index, name in enumerate(names, start=1):
            code = slugify_ru(name, max_length=48) or f"legacy-{index}"
            competency, _ = Competency.objects.get_or_create(
                code=code,
                defaults={
                    "name": name,
                    "category": category,
                    "kind": CompetencyKind.PROFESSIONAL,
                    "description": "Наименование перенесено из исходной базы данных",
                    "sort_order": 900 + index,
                },
            )
            result[name] = competency

        return result

    def _ensure_legacy_model(self, competencies: dict[str, Competency]) -> CompetencyModel:
        """Собирает модель компетенций из перенесённых наименований."""
        model, _ = CompetencyModel.objects.get_or_create(
            code=LEGACY_MODEL_CODE,
            defaults={
                "name": "Модель компетенций исходной базы",
                "description": (
                    "Собрана при переносе из наименований компетенций, встретившихся "
                    "в оценках. Веса равные: исходная схема значимости компетенций "
                    "не хранила."
                ),
                "version": 1,
                "is_published": False,
            },
        )

        if competencies:
            weight = (Decimal("1") / Decimal(len(competencies))).quantize(Decimal("0.001"))
            for competency in competencies.values():
                CompetencyRequirement.objects.get_or_create(
                    competency_model=model,
                    competency=competency,
                    defaults={
                        "target_level": LEGACY_TARGET_LEVEL,
                        "weight": weight,
                        "comment": "Целевой уровень назначен при переносе",
                    },
                )

        return model

    # -- Развитие -----------------------------------------------------------
    def _import_plans(
        self,
        rows: list[dict[str, Any]],
        entries: dict[int, ReserveEntry],
        users: dict[int, User],
    ) -> dict[int, DevelopmentPlan]:
        """Переносит индивидуальные планы развития."""
        result: dict[int, DevelopmentPlan] = {}

        for row in rows:
            legacy_id = int(row["id"])
            entry = entries.get(as_int(row["reserve_id"]) or 0)
            if entry is None:
                self.warnings.append(f"План развития {legacy_id}: запись резерва не найдена")
                continue

            plan, _ = DevelopmentPlan.objects.update_or_create(
                legacy_id=legacy_id,
                defaults={
                    "employee": entry.employee,
                    "entry": entry,
                    "title": as_text(row["title"]),
                    "status": PLAN_STATUS_MAP.get(as_text(row["status"]), PlanStatus.DRAFT),
                    "start_date": as_date(row["start_date"]) or timezone.localdate(),
                    "end_date": as_date(row["end_date"]) or timezone.localdate(),
                    "created_by": users.get(as_int(row["created_by"]) or 0),
                    "goal": (
                        f"Подготовка к назначению на должность «{entry.target_position.name}»"
                    ),
                },
            )
            result[legacy_id] = plan

        self.stats["Планов развития перенесено"] = len(result)
        return result

    def _import_activities(
        self,
        rows: list[dict[str, Any]],
        plans: dict[int, DevelopmentPlan],
        users: dict[int, User],
    ) -> None:
        """Переносит мероприятия развития, устраняя противоречие исходных данных."""
        fixed_dates = 0
        imported = 0

        for row in rows:
            legacy_id = int(row["id"])
            plan = plans.get(as_int(row["plan_id"]) or 0)
            if plan is None:
                continue

            status = ACTIVITY_STATUS_MAP.get(as_text(row["status"]), ActivityStatus.PENDING)
            due_date = as_date(row["due_date"]) or plan.end_date
            completion_date = as_date(row["completion_date"])

            if status == ActivityStatus.COMPLETED and completion_date is None:
                completion_date = min(due_date, timezone.localdate())
                fixed_dates += 1

            DevelopmentActivity.objects.update_or_create(
                legacy_id=legacy_id,
                defaults={
                    "plan": plan,
                    "title": as_text(row["title"]),
                    "activity_type": ACTIVITY_TYPE_MAP.get(
                        as_text(row["type"]), ActivityType.TRAINING
                    ),
                    "description": as_text(row["description"]),
                    "status": status,
                    "start_date": as_date(row["start_date"]),
                    "due_date": due_date,
                    "completion_date": completion_date,
                    "responsible": users.get(as_int(row["responsible_user_id"]) or 0),
                    "mentor": users.get(as_int(row["mentor_id"]) or 0),
                    "result": as_text(row["result"]),
                },
            )
            imported += 1

        for plan in plans.values():
            plan.recalculate_completion()

        self.stats["Мероприятий развития перенесено"] = imported
        self.stats["Дат выполнения восстановлено"] = fixed_dates

    # -- Справочные соответствия --------------------------------------------
    def _legacy_branch_names(self) -> dict[int, str]:
        """Соответствие идентификаторов филиалов исходной базы их городам."""
        return {1: "Москва", 2: "Санкт-Петербург", 3: "Новосибирск", 4: "Екатеринбург"}

    def _legacy_department_names(self) -> dict[int, str]:
        """Соответствие идентификаторов подразделений исходной базы их наименованиям."""
        return {
            1: "Департамент разработки",
            2: "Отдел кадров (HR)",
            3: "Департамент продаж",
            4: "Финансовый отдел",
            5: "Отдел маркетинга",
            6: "Отдел продаж (СПб)",
            7: "Складская логистика",
            8: "Отдел IT-поддержки",
            9: "Департамент разработки (НСК)",
            10: "Бухгалтерия (НСК)",
            11: "Отдел продаж (ЕКБ)",
            12: "Склад (ЕКБ)",
            13: "Отдел снабжения",
            14: "Юридический отдел",
            15: "Бухгалтерия (Центр)",
            16: "АХО",
            17: "Охрана труда",
            18: "Служба безопасности",
        }

    # -- Отчёт --------------------------------------------------------------
    def _report_mapping(self, tables: dict[str, Any]) -> None:
        """Показывает, как сопоставились должности, без записи данных."""
        codes = set(Position.objects.values_list("code", flat=True))
        titles = {as_text(row["target_position"]) for row in tables["reserve"].rows}
        titles |= {as_text(row["current_position"]) for row in tables["candidate"].rows}

        matched = sorted(title for title in titles if POSITION_MAP.get(title) in codes)
        missing = sorted(title for title in titles if title and title not in POSITION_MAP)

        self.stdout.write(f"\nСопоставлено должностей: {len(matched)} из {len(titles)}")
        for title in missing:
            marker = "внешняя организация" if self._looks_external(title) else "нет соответствия"
            self.stdout.write(self.style.WARNING(f"  «{title}» — {marker}"))

    def _report(self) -> None:
        """Выводит итоги переноса."""
        self.stdout.write(self.style.SUCCESS("\nПеренос завершён."))
        width = max(len(name) for name in self.stats) if self.stats else 0
        for name, value in self.stats.items():
            self.stdout.write(f"  {name.ljust(width)} : {value}")

        if self.warnings:
            self.stdout.write(self.style.WARNING(f"\nЗамечаний: {len(self.warnings)}"))
            for message in self.warnings[:20]:
                self.stdout.write(f"  • {message}")
            if len(self.warnings) > 20:
                self.stdout.write(f"  … ещё {len(self.warnings) - 20}")
