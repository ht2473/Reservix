"""
Создание демонстрационных учётных записей.

Запуск::

    uv run python manage.py seed_users
    uv run python manage.py seed_users --flush
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import User
from apps.org.models import Branch, Department
from core.constants import Role, ScopeLevel

#: Пароль демонстрационных учётных записей.
DEMO_PASSWORD = "Reserv-Demo-2026!"  # noqa: S105  # nosec B105

#: Демонстрационные учётные записи.
ACCOUNTS: list[dict[str, Any]] = [
    # --- Администратор ------------------------------------------------------
    {
        "email": "admin@meridian-soft.ru",
        "last_name": "Администратор",
        "first_name": "Системный",
        "middle_name": "Иванович",
        "role": Role.ADMIN,
        "scope": ScopeLevel.ORGANIZATION,
        "branch": "hq-msk",
        "department": "security",
        "superuser": True,
        "note": "Полный доступ к системе, справочникам и журналу аудита",
    },
    # --- Кадровая служба ----------------------------------------------------
    {
        "email": "maria.ivanova@meridian-soft.ru",
        "last_name": "Иванова",
        "first_name": "Мария",
        "middle_name": "Сергеевна",
        "phone": "+79031110001",
        "role": Role.HR,
        "scope": ScopeLevel.ORGANIZATION,
        "branch": "hq-msk",
        "department": "hr",
        "head": True,
        "note": "Руководитель отдела кадров: ведёт резерв всей организации",
    },
    {
        "email": "alexey.sidorov@meridian-soft.ru",
        "last_name": "Сидоров",
        "first_name": "Алексей",
        "middle_name": "Петрович",
        "phone": "+79031110002",
        "role": Role.HR,
        "scope": ScopeLevel.BRANCH,
        "branch": "br-spb",
        "department": "hr",
        "note": "HR-специалист: область видимости ограничена филиалом",
    },
    {
        "email": "olga.kuznetsova@meridian-soft.ru",
        "last_name": "Кузнецова",
        "first_name": "Ольга",
        "middle_name": "Игоревна",
        "phone": "+79031110003",
        "role": Role.HR,
        "scope": ScopeLevel.ORGANIZATION,
        "branch": "hq-msk",
        "department": "hr",
        "note": "HR-специалист: оценка и развитие резервистов",
    },
    # --- Руководители -------------------------------------------------------
    {
        "email": "dmitry.petrov@meridian-soft.ru",
        "last_name": "Петров",
        "first_name": "Дмитрий",
        "middle_name": "Олегович",
        "phone": "+79031110004",
        "role": Role.MANAGER,
        "scope": ScopeLevel.SUBORDINATES,
        "branch": "hq-msk",
        "department": "dev",
        "head": True,
        "note": "Руководитель департамента разработки: видит всё поддерево",
    },
    {
        "email": "elena.orlova@meridian-soft.ru",
        "last_name": "Орлова",
        "first_name": "Елена",
        "middle_name": "Викторовна",
        "phone": "+79031110005",
        "role": Role.MANAGER,
        "scope": ScopeLevel.SUBORDINATES,
        "branch": "hq-msk",
        "department": "dev-backend",
        "head": True,
        "note": "Начальник отдела серверной разработки",
    },
    {
        "email": "sergey.belov@meridian-soft.ru",
        "last_name": "Белов",
        "first_name": "Сергей",
        "middle_name": "Андреевич",
        "phone": "+79031110006",
        "role": Role.MANAGER,
        "scope": ScopeLevel.SUBORDINATES,
        "branch": "hq-msk",
        "department": "sales",
        "head": True,
        "note": "Руководитель департамента продаж",
    },
    {
        "email": "anna.romanova@meridian-soft.ru",
        "last_name": "Романова",
        "first_name": "Анна",
        "middle_name": "Павловна",
        "phone": "+79031110007",
        "role": Role.MANAGER,
        "scope": ScopeLevel.BRANCH,
        "branch": "br-nsk",
        "department": "dev-nsk",
        "head": True,
        "note": "Директор филиала: область видимости — весь филиал",
    },
    # --- Наставник ----------------------------------------------------------
    {
        "email": "maxim.lebedev@meridian-soft.ru",
        "last_name": "Лебедев",
        "first_name": "Максим",
        "middle_name": "Юрьевич",
        "phone": "+79031110008",
        "role": Role.MENTOR,
        "scope": ScopeLevel.SELF,
        "branch": "br-nsk",
        "department": "dev-nsk-platform",
        "note": "Наставник резервистов технического направления",
    },
    # --- Сотрудники ---------------------------------------------------------
    {
        "email": "igor.smirnov@meridian-soft.ru",
        "last_name": "Смирнов",
        "first_name": "Игорь",
        "middle_name": "Владимирович",
        "phone": "+79001112233",
        "role": Role.EMPLOYEE,
        "scope": ScopeLevel.SELF,
        "branch": "hq-msk",
        "department": "dev-backend",
        "note": "Сотрудник, состоящий в кадровом резерве",
    },
    {
        "email": "andrey.kuznetsov@meridian-soft.ru",
        "last_name": "Кузнецов",
        "first_name": "Андрей",
        "middle_name": "Дмитриевич",
        "phone": "+79004445566",
        "role": Role.EMPLOYEE,
        "scope": ScopeLevel.SELF,
        "branch": "hq-msk",
        "department": "dev-backend",
        "note": "Сотрудник с активным планом развития",
    },
    {
        "email": "artem.volkov@meridian-soft.ru",
        "last_name": "Волков",
        "first_name": "Артём",
        "middle_name": "Александрович",
        "phone": "+79007778899",
        "role": Role.EMPLOYEE,
        "scope": ScopeLevel.SELF,
        "branch": "hq-msk",
        "department": "sales-corp",
        "note": "Сотрудник направления продаж",
    },
    {
        "email": "natalya.gavrilova@meridian-soft.ru",
        "last_name": "Гаврилова",
        "first_name": "Наталья",
        "middle_name": "Сергеевна",
        "phone": "+79006667788",
        "role": Role.EMPLOYEE,
        "scope": ScopeLevel.SELF,
        "branch": "br-nsk",
        "department": "dev-nsk-platform",
        "note": "Сотрудник филиала: проверка ограничения видимости",
    },
]


class Command(BaseCommand):
    """Создаёт демонстрационные учётные записи всех ролей."""

    help = "Создаёт демонстрационные учётные записи пользователей системы"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--flush",
            action="store_true",
            help="удалить существующие учётные записи перед созданием",
        )
        parser.add_argument(
            "--password",
            default=DEMO_PASSWORD,
            help="пароль демонстрационных записей",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        if not Branch.objects.exists():
            self.stderr.write(
                self.style.ERROR(
                    "Справочники организационной структуры пусты. "
                    "Сначала выполните: python manage.py seed_org"
                )
            )
            return

        if options["flush"]:
            deleted, _ = User.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Удалено учётных записей: {deleted}"))

        branches = {branch.code: branch for branch in Branch.objects.all()}
        departments = {dept.code: dept for dept in Department.objects.all()}
        password = options["password"]

        created = self._create_accounts(branches, departments, password)
        self._assign_department_heads()

        self._report(created, password)

    def _create_accounts(
        self,
        branches: dict[str, Branch],
        departments: dict[str, Department],
        password: str,
    ) -> list[User]:
        """Создаёт учётные записи по описанию."""
        created: list[User] = []

        for item in ACCOUNTS:
            user, is_new = User.objects.update_or_create(
                email=item["email"],
                defaults={
                    "last_name": item["last_name"],
                    "first_name": item["first_name"],
                    "middle_name": item.get("middle_name", ""),
                    "phone": item.get("phone", ""),
                    "role": item["role"],
                    "scope_level": item["scope"],
                    "branch": branches[item["branch"]],
                    "department": departments[item["department"]],
                    "is_department_head": item.get("head", False),
                    "is_active": True,
                    "is_staff": item.get("superuser", False),
                    "is_superuser": item.get("superuser", False),
                    # Демонстрационные записи не требуют смены пароля: иначе
                    # проверяющий не сможет войти под ролью, не сменив пароль.
                    "must_change_password": False,  # nosec B105
                },
            )

            user.set_password(password)
            user.save(update_fields=["password", "password_changed_at", "must_change_password"])

            created.append(user)
            marker = "создан" if is_new else "обновлён"
            self.stdout.write(f"  {marker}: {user.email} — {user.role_label}")

        return created

    def _assign_department_heads(self) -> None:
        """Проставляет руководителей подразделений."""
        assigned = 0

        for user in User.objects.filter(is_department_head=True).select_related("department"):
            if user.department_id and user.department.head_id != user.pk:
                user.department.head = user
                user.department.save(update_fields=["head"])
                assigned += 1

        if assigned:
            self.stdout.write(f"Назначено руководителей подразделений: {assigned}")

    def _report(self, created: list[User], password: str) -> None:
        """Выводит сводку и учётные данные демонстрационных записей."""
        by_role: dict[str, int] = {}
        for user in created:
            by_role[user.role_label] = by_role.get(user.role_label, 0) + 1

        self.stdout.write(self.style.SUCCESS("\nУчётные записи созданы."))
        for role, count in sorted(by_role.items()):
            self.stdout.write(f"  {role}: {count}")

        self.stdout.write(self.style.WARNING(f"\nПароль всех демонстрационных записей: {password}"))
        self.stdout.write(
            "Полный перечень учётных данных приведён в docs/CREDENTIALS.md\n"
            "В рабочей системе учётные записи создаёт администратор, "
            "пароль выдаётся одноразовым и подлежит смене при первом входе."
        )
