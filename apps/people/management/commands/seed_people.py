"""
Наполнение кадрового состава организации.

Запуск::

    uv run python manage.py seed_people
    uv run python manage.py seed_people --seed 42 --vacancy-rate 0.05
    uv run python manage.py seed_people --flush    # удалить сгенерированные
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.management.commands.seed_users import ACCOUNTS as DEMO_ACCOUNTS
from apps.accounts.models import User
from apps.org.models import Department, Position
from apps.people.models import (
    AttritionRisk,
    EducationLevel,
    Employee,
    EmployeeStatus,
    EmploymentChange,
    EmploymentHistory,
    Mobility,
)
from core.constants import Role, ScopeLevel
from core.utils import slugify_ru

#: Значение генератора случайных чисел по умолчанию.
DEFAULT_SEED = 20260828

#: Доля штатных единиц, остающихся вакантными.
DEFAULT_VACANCY_RATE = 0.05

#: Префикс табельного номера сгенерированных карточек.
PERSONNEL_PREFIX = "S"

#: Домен корпоративной почты.
ORG_DOMAIN = "meridian-soft.ru"

#: Пароль учётных записей, создаваемых для руководителей. Совпадает с паролем
#: демонстрационного стенда: перечень учётных данных приведён в документации.
DEMO_PASSWORD = "Reserv-Demo-2026!"  # noqa: S105  # nosec B105

# fmt: off
MALE_FIRST_NAMES = [
    "Александр", "Алексей", "Андрей", "Антон", "Артём", "Борис", "Вадим", "Валерий", "Василий",
    "Виктор", "Виталий", "Владимир", "Владислав", "Вячеслав", "Геннадий", "Георгий", "Григорий",
    "Даниил", "Денис", "Дмитрий", "Евгений", "Егор", "Иван", "Игорь", "Илья", "Кирилл",
    "Константин", "Леонид", "Максим", "Марк", "Михаил", "Никита", "Николай", "Олег", "Павел",
    "Пётр", "Роман", "Руслан", "Сергей", "Станислав", "Степан", "Тимур", "Фёдор", "Юрий",
    "Ярослав"
]
# fmt: on

# fmt: off
FEMALE_FIRST_NAMES = [
    "Александра", "Алина", "Алла", "Анастасия", "Анна", "Валентина", "Валерия", "Вера",
    "Вероника", "Виктория", "Галина", "Дарья", "Диана", "Евгения", "Екатерина", "Елена",
    "Елизавета", "Ирина", "Кристина", "Ксения", "Лариса", "Любовь", "Людмила", "Маргарита",
    "Марина", "Мария", "Надежда", "Наталья", "Нина", "Оксана", "Ольга", "Полина", "Светлана",
    "София", "Тамара", "Татьяна", "Ульяна", "Юлия", "Яна"
]
# fmt: on

#: Отчества в готовом виде: образование отчества от имени имеет исключения,
#: и правило «имя плюс -ович» дало бы «Ильевич» вместо «Ильич».
# fmt: off
MALE_PATRONYMICS = [
    "Александрович", "Алексеевич", "Андреевич", "Антонович", "Борисович", "Вадимович",
    "Валерьевич", "Васильевич", "Викторович", "Владимирович", "Вячеславович", "Геннадьевич",
    "Григорьевич", "Дмитриевич", "Евгеньевич", "Иванович", "Игоревич", "Ильич", "Кириллович",
    "Константинович", "Леонидович", "Максимович", "Михайлович", "Николаевич", "Олегович",
    "Павлович", "Петрович", "Романович", "Сергеевич", "Станиславович", "Фёдорович", "Юрьевич",
    "Ярославович"
]
# fmt: on

# fmt: off
FEMALE_PATRONYMICS = [
    "Александровна", "Алексеевна", "Андреевна", "Антоновна", "Борисовна", "Вадимовна",
    "Валерьевна", "Васильевна", "Викторовна", "Владимировна", "Вячеславовна", "Геннадьевна",
    "Григорьевна", "Дмитриевна", "Евгеньевна", "Ивановна", "Игоревна", "Ильинична",
    "Кирилловна", "Константиновна", "Леонидовна", "Максимовна", "Михайловна", "Николаевна",
    "Олеговна", "Павловна", "Петровна", "Романовна", "Сергеевна", "Станиславовна", "Фёдоровна",
    "Юрьевна", "Ярославовна"
]
# fmt: on

#: Мужские формы фамилий. Женская форма образуется по правилу склонения.
# fmt: off
SURNAMES = [
    "Абрамов", "Агафонов", "Аксёнов", "Андреев", "Анисимов", "Антонов", "Артемьев", "Афанасьев",
    "Баранов", "Беляев", "Бирюков", "Блинов", "Богданов", "Борисов", "Быков", "Васильев",
    "Виноградов", "Власов", "Волков", "Воробьёв", "Воронов", "Гаврилов", "Герасимов", "Голубев",
    "Горбунов", "Гордеев", "Горшков", "Григорьев", "Гусев", "Давыдов", "Данилов", "Дементьев",
    "Денисов", "Дорофеев", "Дроздов", "Егоров", "Ермаков", "Ефимов", "Жданов", "Жуков",
    "Журавлёв", "Зайцев", "Захаров", "Зимин", "Зуев", "Иванов", "Игнатов", "Ильин", "Исаев",
    "Кабанов", "Казаков", "Калинин", "Капустин", "Карпов", "Киселёв", "Ковалёв", "Комаров",
    "Кондратьев", "Коновалов", "Константинов", "Копылов", "Королёв", "Костин", "Котов",
    "Кудрявцев", "Кузнецов", "Кузьмин", "Кулагин", "Куликов", "Лазарев", "Лапин", "Ларионов",
    "Лебедев", "Логинов", "Лукин", "Макаров", "Максимов", "Мамонтов", "Марков", "Мартынов",
    "Масленников", "Матвеев", "Медведев", "Мельников", "Миронов", "Михайлов", "Мишин",
    "Морозов", "Мухин", "Мясников", "Назаров", "Наумов", "Некрасов", "Нестеров", "Никитин",
    "Николаев", "Новиков", "Носков", "Овчинников", "Одинцов", "Орехов", "Орлов", "Осипов",
    "Павлов", "Панкратов", "Панов", "Пахомов", "Петров", "Поляков", "Пономарёв", "Попов",
    "Потапов", "Прохоров", "Романов", "Рыбаков", "Савельев", "Сазонов", "Самойлов", "Сафонов",
    "Селезнёв", "Семёнов", "Сергеев", "Сидоров", "Смирнов", "Соболев", "Соколов", "Соловьёв",
    "Сорокин", "Степанов", "Стрелков", "Субботин", "Суворов", "Судаков", "Тарасов", "Терентьев",
    "Тимофеев", "Титов", "Тихонов", "Токарев", "Трофимов", "Туров", "Уваров", "Ульянов",
    "Устинов", "Фадеев", "Фёдоров", "Филатов", "Филиппов", "Фомин", "Фролов", "Хохлов",
    "Цветков", "Чернов", "Черных", "Чижов", "Шарапов", "Шашков", "Шестаков", "Ширяев", "Шубин",
    "Щербаков", "Юдин", "Яковлев", "Якушев"
]
# fmt: on

#: Учебные заведения для поля «образование».
INSTITUTIONS = [
    "МГТУ им. Баумана",
    "МГУ им. Ломоносова",
    "СПбГУ",
    "СПбПУ Петра Великого",
    "НГУ",
    "НГТУ",
    "УрФУ",
    "ВШЭ",
    "МФТИ",
    "РЭУ им. Плеханова",
    "РАНХиГС",
    "Финансовый университет",
    "МИФИ",
    "МАИ",
    "ИТМО",
    "ТПУ",
    "КФУ",
    "СГУ им. Чернышевского",
    "МГЮА",
    "ГУУ",
]

#: Специальности, дополняющие сведения об образовании.
MAJORS = [
    "прикладная математика и информатика",
    "программная инженерия",
    "информационные системы и технологии",
    "экономика",
    "менеджмент",
    "управление персоналом",
    "бухгалтерский учёт и аудит",
    "юриспруденция",
    "маркетинг",
    "логистика",
    "информационная безопасность",
    "бизнес-информатика",
]

#: Подразделения, в которых встречаются типовые должности, и доли распределения штатных единиц
#: между ними.
TYPICAL_POSITION_PLACEMENT: dict[str, list[tuple[str, float]]] = {
    "branch-director": [("sales-spb", 0.34), ("dev-nsk", 0.33), ("sales-ekb", 0.33)],
    "sales-manager": [("sales-smb", 0.4), ("sales-spb", 0.32), ("sales-ekb", 0.28)],
    "senior-sales": [("sales-corp", 0.45), ("sales-spb", 0.3), ("sales-ekb", 0.25)],
    "accountant": [("accounting-hq", 0.55), ("accounting-nsk", 0.45)],
    "logistician": [("logistics-spb", 0.65), ("warehouse-ekb", 0.35)],
    "storekeeper": [("warehouse-ekb", 0.6), ("logistics-spb", 0.4)],
    "supply-specialist": [("supply-ekb", 0.6), ("logistics-spb", 0.4)],
    "engineer": [("facility", 0.4), ("warehouse-ekb", 0.4), ("labor-safety", 0.2)],
}

#: Распределение образования по уровню грейда.
EDUCATION_WEIGHTS: list[tuple[int, dict[str, float]]] = [
    (
        2,
        {
            EducationLevel.SECONDARY: 0.15,
            EducationLevel.VOCATIONAL: 0.35,
            EducationLevel.BACHELOR: 0.35,
            EducationLevel.SPECIALIST: 0.15,
        },
    ),
    (
        4,
        {
            EducationLevel.VOCATIONAL: 0.1,
            EducationLevel.BACHELOR: 0.4,
            EducationLevel.SPECIALIST: 0.3,
            EducationLevel.MASTER: 0.2,
        },
    ),
    (
        6,
        {
            EducationLevel.BACHELOR: 0.25,
            EducationLevel.SPECIALIST: 0.3,
            EducationLevel.MASTER: 0.4,
            EducationLevel.POSTGRADUATE: 0.05,
        },
    ),
    (
        20,
        {
            EducationLevel.SPECIALIST: 0.25,
            EducationLevel.MASTER: 0.6,
            EducationLevel.POSTGRADUATE: 0.15,
        },
    ),
]

#: Наименьший и наибольший возраст по уровню грейда. Верхняя граница растёт
#: медленнее нижней: на высоких грейдах молодых почти нет, а пожилые есть везде.
AGE_RANGES: list[tuple[int, tuple[int, int]]] = [
    (1, (19, 30)),
    (2, (21, 34)),
    (3, (23, 45)),
    (4, (26, 52)),
    (5, (29, 55)),
    (6, (30, 56)),
    (7, (33, 58)),
    (8, (36, 60)),
    (20, (38, 62)),
]

#: Доли статусов трудовых отношений.
STATUS_WEIGHTS: dict[str, float] = {
    EmployeeStatus.ACTIVE: 0.88,
    EmployeeStatus.PROBATION: 0.05,
    EmployeeStatus.LEAVE: 0.04,
    EmployeeStatus.DISMISSED: 0.03,
}

#: Доли уровней риска ухода.
RISK_WEIGHTS: dict[str, float] = {
    AttritionRisk.LOW: 0.68,
    AttritionRisk.MEDIUM: 0.24,
    AttritionRisk.HIGH: 0.08,
}

#: Доли готовности к перемещению.
MOBILITY_WEIGHTS: dict[str, float] = {
    Mobility.NONE: 0.28,
    Mobility.CITY: 0.42,
    Mobility.REGION: 0.19,
    Mobility.COUNTRY: 0.11,
}

#: Доля сотрудников, отмеченных как ключевые.
KEY_TALENT_SHARE = 0.09

#: Грейд, начиная с которого сотруднику создаётся учётная запись.
ACCOUNT_GRADE_LEVEL = 6

#: Подразделения, сотрудникам которых учётная запись создаётся независимо от
#: грейда: кадровая служба работает в системе целиком.
ALWAYS_ACCOUNT_DEPARTMENTS = {"hr"}

#: Адреса демонстрационных учётных записей.
DEMO_EMAILS = frozenset(item["email"] for item in DEMO_ACCOUNTS)

#: Демонстрационные учётные записи, которым закрепляется карточка из штата:
#: руководитель оценивает подчинённых и согласует их заявки как их начальник.
DEMO_STAFF_EMAILS = frozenset(
    item["email"] for item in DEMO_ACCOUNTS if item["role"] in {Role.HR, Role.MANAGER}
)


class Command(BaseCommand):
    """Достраивает кадровый состав до плановой численности."""

    help = "Наполняет кадровый состав организации до плановой численности"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--seed",
            type=int,
            default=DEFAULT_SEED,
            help="значение генератора случайных чисел для воспроизводимости",
        )
        parser.add_argument(
            "--vacancy-rate",
            type=float,
            default=DEFAULT_VACANCY_RATE,
            help="доля штатных единиц, остающихся вакантными",
        )
        parser.add_argument(
            "--flush",
            action="store_true",
            help="удалить ранее сгенерированные карточки (табельные номера с префиксом S)",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        """Наполняет штат целиком в одной транзакции."""
        self.random = random.Random(options["seed"])  # noqa: S311  # nosec B311
        self.vacancy_rate = max(0.0, min(0.5, options["vacancy_rate"]))
        self.today = timezone.localdate()

        if options["flush"]:
            self._flush()

        self._load_reference_data()
        created = self._fill_positions()
        self._attach_demo_staff(created)
        accounts = self._create_accounts(created)
        self._assign_managers()

        total = Employee.objects.count()
        self.stdout.write(self.style.SUCCESS("\nКадровый состав наполнен."))
        self.stdout.write(
            f"  Создано карточек: {len(created)}\n"
            f"  Создано учётных записей: {accounts}\n"
            f"  Всего сотрудников: {total}\n"
            f"  Вакантных штатных единиц: {self.vacancies}\n"
            f"  Плановая численность: {self.plan_total}\n"
            f"  Укомплектованность: {round(total * 100 / self.plan_total, 1)}%"
        )

    # -- Подготовка ---------------------------------------------------------
    def _flush(self) -> None:
        """Удаляет сгенерированные карточки, не трогая перенесённые из дампа."""
        generated = Employee.all_objects.filter(personnel_number__startswith=PERSONNEL_PREFIX)
        count = generated.count()
        User.objects.filter(employee_card__in=generated).exclude(
            email__in=DEMO_STAFF_EMAILS
        ).delete()
        generated.delete()
        if count:
            self.stdout.write(self.style.WARNING(f"Удалено сгенерированных карточек: {count}"))

    def _load_reference_data(self) -> None:
        """Читает справочники и подготавливает наборы для генерации."""
        self.departments = {dept.code: dept for dept in Department.objects.select_related("branch")}
        self.positions = list(
            Position.objects.select_related("grade", "department", "department__branch")
        )
        self.plan_total = sum(position.headcount_plan for position in self.positions)
        self.used_emails = set(Employee.all_objects.values_list("email", flat=True))
        self.used_emails |= set(User.objects.values_list("email", flat=True))
        self.next_number = self._next_personnel_number()

    def _next_personnel_number(self) -> int:
        """Определяет следующий свободный табельный номер сгенерированных карточек."""
        numbers = [
            int(value.split("-")[1])
            for value in Employee.all_objects.filter(
                personnel_number__startswith=f"{PERSONNEL_PREFIX}-"
            ).values_list("personnel_number", flat=True)
            if value.split("-")[-1].isdigit()
        ]
        return max(numbers, default=0) + 1

    # -- Наполнение штата ---------------------------------------------------
    def _fill_positions(self) -> list[Employee]:
        """Создаёт карточки под незакрытые штатные единицы."""
        created: list[Employee] = []
        self.vacancies = 0

        for position in self.positions:
            for department, quota in self._distribute(position):
                existing = Employee.all_objects.filter(
                    position=position, department=department
                ).count()

                for _ in range(max(0, quota - existing)):
                    # Вакансия разыгрывается на каждую штатную единицу отдельно, а не вычисляется
                    # долей от квоты.
                    if self.random.random() < self.vacancy_rate:
                        self.vacancies += 1
                        continue
                    created.append(self._create_employee(position, department))

        return created

    def _distribute(self, position: Position) -> list[tuple[Department, int]]:
        """Раскладывает штатные единицы должности по подразделениям."""
        # Описанное размещение важнее подразделения в штатном расписании: директора
        # филиалов числятся за одним подразделением, а работают в разных городах.
        if position.department is not None and position.code not in TYPICAL_POSITION_PLACEMENT:
            return [(position.department, position.headcount_plan)]

        placement = TYPICAL_POSITION_PLACEMENT.get(position.code)
        if not placement:
            # Типовая должность без описанного размещения: карточки создаются
            # в головном офисе, а несоответствие выводится предупреждением.
            fallback = self.departments.get("facility")
            self.stdout.write(
                self.style.WARNING(f"Должность «{position.name}» не имеет описанного размещения")
            )
            if fallback is None:
                return []
            return [(fallback, position.headcount_plan)]

        result: list[tuple[Department, int]] = []
        assigned = 0
        for index, (code, share) in enumerate(placement):
            department = self.departments.get(code)
            if department is None:
                continue
            if index == len(placement) - 1:
                quota = position.headcount_plan - assigned
            else:
                quota = round(position.headcount_plan * share)
                assigned += quota
            result.append((department, quota))

        return result

    def _create_employee(self, position: Position, department: Department) -> Employee:
        """Создаёт карточку сотрудника, согласованную с должностью."""
        level = position.grade.level
        is_male = self.random.random() < 0.55
        last_name, first_name, middle_name = self._make_name(is_male)

        birth_date = self._make_birth_date(level)
        hire_date = self._make_hire_date(birth_date)
        position_since = self._make_position_since(hire_date)
        status = self._pick(STATUS_WEIGHTS)
        dismissal_date = None
        if status == EmployeeStatus.DISMISSED:
            dismissal_date = self._random_date(position_since, self.today)
        elif status == EmployeeStatus.ACTIVE and (self.today - hire_date).days < 90:
            # Принятый недавно проходит испытательный срок: статус выводится из
            # даты приёма, а не назначается независимо от неё.
            status = EmployeeStatus.PROBATION

        is_managerial = position.grade.is_managerial
        management_months = 0
        subordinates = 0
        if is_managerial:
            management_months = self.random.randint(12, max(13, (level - 4) * 24))
            subordinates = self.random.randint(2, 4 * max(1, level - 4))
        elif level >= 4 and self.random.random() < 0.35:
            # Ведущие специалисты часто руководят проектными группами: этот опыт
            # существен при отборе в управленческий резерв.
            management_months = self.random.randint(6, 24)
            subordinates = self.random.randint(2, 5)

        employee = Employee(
            personnel_number=f"{PERSONNEL_PREFIX}-{self.next_number:04d}",
            last_name=last_name,
            first_name=first_name,
            middle_name=middle_name,
            birth_date=birth_date,
            email=self._make_email(last_name, first_name),
            phone=self._make_phone(),
            education_level=self._pick(self._education_weights(level)),
            education=self._make_education(),
            branch=department.branch,
            department=department,
            position=position,
            status=status,
            hire_date=hire_date,
            position_since=position_since,
            dismissal_date=dismissal_date,
            prior_experience_months=self._make_prior_experience(birth_date, hire_date),
            management_experience_months=management_months,
            max_subordinates=subordinates,
            mobility=self._pick(MOBILITY_WEIGHTS),
            attrition_risk=self._pick(RISK_WEIGHTS),
            is_key_talent=self.random.random() < KEY_TALENT_SHARE,
        )
        employee.save()
        self.next_number += 1

        self._create_history(employee, position, department)
        return employee

    def _create_history(
        self, employee: Employee, position: Position, department: Department
    ) -> None:
        """Создаёт кадровую историю сотрудника."""
        records: list[EmploymentHistory] = []
        position_since = employee.position_since or employee.hire_date

        if position_since > employee.hire_date:
            previous_level = max(1, position.grade.level - 1)
            records.append(
                EmploymentHistory(
                    employee=employee,
                    position=None,
                    position_title=f"{position.name} (предыдущий уровень)",
                    department=department,
                    branch=department.branch,
                    grade_level=previous_level,
                    change_type=EmploymentChange.HIRE,
                    start_date=employee.hire_date,
                    end_date=position_since - dt.timedelta(days=1),
                )
            )

        records.append(
            EmploymentHistory(
                employee=employee,
                position=position,
                position_title=position.name,
                department=department,
                branch=department.branch,
                grade_level=position.grade.level,
                change_type=(
                    EmploymentChange.PROMOTION
                    if position_since > employee.hire_date
                    else EmploymentChange.HIRE
                ),
                start_date=position_since,
                end_date=employee.dismissal_date,
                subordinates_count=employee.max_subordinates,
            )
        )

        EmploymentHistory.objects.bulk_create(records)

    # -- Учётные записи -----------------------------------------------------
    def _create_accounts(self, employees: list[Employee]) -> int:
        """Создаёт учётные записи руководителям и кадровой службе."""
        created = 0

        for employee in employees:
            if employee.status == EmployeeStatus.DISMISSED or employee.user_id:
                continue
            if not self._needs_account(employee):
                continue

            role = self._role_for(employee)
            user = User(
                email=employee.email,
                last_name=employee.last_name,
                first_name=employee.first_name,
                middle_name=employee.middle_name,
                phone=employee.phone,
                role=role,
                scope_level=(
                    ScopeLevel.ORGANIZATION if role == Role.HR else ScopeLevel.SUBORDINATES
                ),
                branch=employee.branch,
                department=employee.department,
                must_change_password=True,
            )
            user.set_password(DEMO_PASSWORD)
            user.save()

            employee.user = user
            employee.save(update_fields=["user", "updated_at"])
            created += 1

        return created

    def _attach_demo_staff(self, employees: list[Employee]) -> None:
        """Закрепляет за демонстрационными руководителями и кадровиками карточки штата."""
        users = User.objects.filter(
            email__in=DEMO_STAFF_EMAILS, employee_card__isnull=True
        ).order_by("-is_department_head", "email")

        for user in users:
            candidates = sorted(
                (
                    item
                    for item in employees
                    if item.user_id is None
                    and item.department_id == user.department_id
                    and item.status != EmployeeStatus.DISMISSED
                ),
                key=lambda item: -(item.position.grade.level if item.position else 0),
            )
            if not candidates:
                continue

            # Руководитель занимает старшую должность подразделения, специалист — следующую.
            employee = candidates[0 if user.is_department_head else min(1, len(candidates) - 1)]
            for field in ("last_name", "first_name", "middle_name"):
                setattr(employee, f"{field}_ru", getattr(user, field))
                setattr(employee, f"{field}_en", None)
            employee.email = user.email
            employee.status = EmployeeStatus.ACTIVE
            employee.user = user
            employee.save()

    def _needs_account(self, employee: Employee) -> bool:
        """Определяет, нужна ли сотруднику учётная запись."""
        if employee.department and employee.department.code in ALWAYS_ACCOUNT_DEPARTMENTS:
            return True
        return bool(employee.position and employee.position.grade.level >= ACCOUNT_GRADE_LEVEL)

    def _role_for(self, employee: Employee) -> str:
        """Роль учётной записи по подразделению и грейду должности."""
        if employee.department and employee.department.code in ALWAYS_ACCOUNT_DEPARTMENTS:
            return Role.HR
        return Role.MANAGER

    # -- Руководители -------------------------------------------------------
    def _assign_managers(self) -> None:
        """Назначает руководителей подразделений и непосредственных начальников."""
        assigned_departments = 0
        assigned_managers = 0

        for department in Department.objects.select_related("branch"):
            staff = list(
                Employee.objects.filter(department=department)
                .exclude(status=EmployeeStatus.DISMISSED)
                .select_related("position", "position__grade", "user")
                .order_by("-position__grade__level")
            )
            if not staff:
                continue

            # Руководитель из демонстрационных записей сохраняет за собой подразделение.
            head = next(
                (item for item in staff if item.user is not None and item.user.is_department_head),
                None,
            ) or next((item for item in staff if item.user is not None), None)
            if head is not None and head.user is not None:
                department.head = head.user
                department.save(update_fields=["head", "updated_at"])
                head.user.is_department_head = True
                head.user.save(update_fields=["is_department_head", "updated_at"])
                assigned_departments += 1

            if head is None:
                continue

            # Крупный отдел делится на группы: сотрудники подчиняются руководителям групп,
            # руководители групп и демонстрационные сотрудники — начальнику отдела.
            leads = [
                item
                for item in staff
                if item.pk != head.pk and item.user is not None and item.user.role == Role.MANAGER
            ]
            subordinates = [
                item for item in staff if item.pk != head.pk and item.manager_id is None
            ]
            for index, employee in enumerate(subordinates):
                direct = (
                    not leads
                    or employee in leads
                    or (employee.user is not None and employee.user.email in DEMO_EMAILS)
                )
                employee.manager = head if direct else leads[index % len(leads)]
                employee.save(update_fields=["manager", "updated_at"])
                assigned_managers += 1

        # Руководитель вложенного подразделения подчиняется руководителю вышестоящего.
        for department in Department.objects.filter(
            parent__isnull=False, head__isnull=False
        ).select_related("parent"):
            parent_head = Employee.objects.filter(user_id=department.parent.head_id).first()
            if parent_head is not None:
                Employee.objects.filter(user_id=department.head_id).exclude(
                    pk=parent_head.pk
                ).update(manager=parent_head)

        self.stdout.write(
            f"Руководителей подразделений назначено: {assigned_departments}; "
            f"связей подчинения: {assigned_managers}"
        )

    # -- Генерация значений -------------------------------------------------
    def _make_name(self, is_male: bool) -> tuple[str, str, str]:
        """Формирует фамилию, имя и отчество согласованного рода."""
        surname = self.random.choice(SURNAMES)
        if is_male:
            return (
                surname,
                self.random.choice(MALE_FIRST_NAMES),
                self.random.choice(MALE_PATRONYMICS),
            )

        if surname.endswith(("ых", "их", "ко")):
            female_surname = surname
        elif surname.endswith("ий"):
            female_surname = surname[:-2] + "ая"
        else:
            female_surname = surname + "а"

        return (
            female_surname,
            self.random.choice(FEMALE_FIRST_NAMES),
            self.random.choice(FEMALE_PATRONYMICS),
        )

    def _make_email(self, last_name: str, first_name: str) -> str:
        """Строит уникальный корпоративный адрес почты."""
        base = f"{slugify_ru(first_name)}.{slugify_ru(last_name)}"
        address = f"{base}@{ORG_DOMAIN}"
        suffix = 2
        while address in self.used_emails:
            address = f"{base}{suffix}@{ORG_DOMAIN}"
            suffix += 1
        self.used_emails.add(address)
        return address

    def _make_phone(self) -> str:
        """Номер телефона в едином формате хранения."""
        return f"+79{self.random.randint(10, 69):02d}{self.random.randint(0, 9999999):07d}"

    def _make_birth_date(self, level: int) -> dt.date:
        """Дата рождения, согласованная с уровнем грейда."""
        low, high = next(bounds for threshold, bounds in AGE_RANGES if level <= threshold)
        age = self.random.randint(low, high)
        return self.today.replace(year=self.today.year - age) - dt.timedelta(
            days=self.random.randint(0, 364)
        )

    def _make_hire_date(self, birth_date: dt.date) -> dt.date:
        """Дата приёма, не противоречащая возрасту."""
        earliest_working = birth_date.replace(year=birth_date.year + 20)
        earliest = max(earliest_working, self.today - dt.timedelta(days=14 * 365))
        latest = self.today - dt.timedelta(days=30)
        if earliest >= latest:
            earliest = latest - dt.timedelta(days=365)
        return self._random_date(earliest, latest)

    def _make_position_since(self, hire_date: dt.date) -> dt.date:
        """Дата назначения на текущую должность."""
        if self.random.random() < 0.3:
            latest = self.today - dt.timedelta(days=30)
            if hire_date + dt.timedelta(days=365) < latest:
                return self._random_date(hire_date + dt.timedelta(days=365), latest)
        return hire_date

    def _make_prior_experience(self, birth_date: dt.date, hire_date: dt.date) -> int:
        """Опыт до приёма: от нуля до срока с возраста начала трудовой деятельности."""
        available_years = max(0, (hire_date.year - birth_date.year) - 22)
        if available_years <= 0:
            return 0
        return self.random.randint(0, available_years) * 12 + self.random.randint(0, 11)

    def _make_education(self) -> str:
        """Строка сведений об образовании."""
        return (
            f"{self.random.choice(INSTITUTIONS)}, "
            f"{self.random.choice(MAJORS)}, "
            f"{self.random.randint(1998, 2024)}"
        )

    def _education_weights(self, level: int) -> dict[str, float]:
        """Распределение уровней образования для указанного грейда."""
        return next(weights for threshold, weights in EDUCATION_WEIGHTS if level <= threshold)

    def _pick(self, weights: dict[str, float]) -> Any:
        """Выбирает значение согласно заданным долям."""
        options = list(weights)
        shares = [weights[option] for option in options]
        return self.random.choices(options, weights=shares, k=1)[0]

    def _random_date(self, start: dt.date, end: dt.date) -> dt.date:
        """Случайная дата в границах промежутка."""
        span = max((end - start).days, 1)
        return start + dt.timedelta(days=self.random.randint(0, span))
