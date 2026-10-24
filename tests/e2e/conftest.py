"""Окружение сквозных сценариев."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

# Синхронный интерфейс Playwright держит в основном потоке работающий цикл событий: он нужен ему
# для общения с процессом браузера.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from playwright.sync_api import Page, sync_playwright

from django.contrib.auth.hashers import make_password
from django.core.management import call_command

from apps.analytics.services import ensure_criteria, ensure_default_scheme
from apps.people.models import AttritionRisk
from tests.factories import (
    ArticleCategoryFactory,
    ArticleFactory,
    BranchFactory,
    DepartmentFactory,
    GlossaryTermFactory,
    PositionFactory,
    PositionGradeFactory,
    UserFactory,
)
from tests.factories.domain import (
    DevelopmentActivityFactory,
    DevelopmentPlanFactory,
    EmployeeFactory,
    NineBoxPlacementFactory,
    ReserveEntryFactory,
)

#: Пароль стенда. Совпадает с требованиями политики сложности: сценарий входа
#: проходит настоящую форму, а не подменяет сессию.
E2E_PASSWORD = "Skvoznoy-Scenariy-2026"


# ---------------------------------------------------------------------------
# Браузер
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def _installed_browsers() -> frozenset[str]:
    """Браузеры, установленные в системе."""
    available: set[str] = set()
    with sync_playwright() as playwright:
        for name in ("chromium", "firefox", "webkit"):
            executable = getattr(playwright, name).executable_path
            if executable and Path(executable).exists():
                available.add(name)
    return frozenset(available)


@pytest.fixture(autouse=True)
def _require_browser(browser_name: str, _installed_browsers: frozenset[str]) -> None:
    """Пропускает сценарий, если запрошенный браузер не установлен."""
    if browser_name not in _installed_browsers:
        pytest.skip(
            f"Браузер {browser_name} не установлен. "
            f"Выполните: uv run playwright install {browser_name}"
        )


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    """Единый контекст браузера для всех сценариев."""
    return {
        **browser_context_args,
        "viewport": {"width": 1440, "height": 900},
        "locale": "ru-RU",
        "timezone_id": "Europe/Moscow",
    }


# ---------------------------------------------------------------------------
# Стенд
# ---------------------------------------------------------------------------
@pytest.fixture
def e2e_data(transactional_db) -> dict:
    """Наполняет базу связным набором данных для сквозных сценариев."""
    password = make_password(E2E_PASSWORD)

    branch = BranchFactory(name="Головной офис", city="Москва", is_head_office=True)
    department = DepartmentFactory(name="Департамент разработки", branch=branch)
    grade = PositionGradeFactory(name="Руководитель отдела", level=9, is_managerial=True)
    position = PositionFactory(
        name="Руководитель отдела разработки",
        grade=grade,
        department=department,
        is_key_position=True,
    )

    users = {}
    for role, scope in (
        ("admin", "organization"),
        ("hr", "organization"),
        ("manager", "subordinates"),
        ("employee", "self"),
        ("mentor", "self"),
    ):
        user = UserFactory(
            email=f"{role}@e2e.reservix.ru",
            role=role,
            scope_level=scope,
            branch=branch,
            department=department,
            is_department_head=role == "manager",
            is_superuser=role == "admin",
            is_staff=role == "admin",
        )
        user.password = password
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        users[role] = user

    # Кадровый состав: пятеро в подразделении, один — с высоким риском
    # ухода, чтобы страница команды показывала сигнал, а не ровный список.
    employees = [
        EmployeeFactory(
            last_name=surname,
            branch=branch,
            department=department,
            position=position,
            attrition_risk=AttritionRisk.HIGH if surname == "Ершов" else AttritionRisk.LOW,
        )
        for surname in ("Абрамов", "Белов", "Гусев", "Дроздов", "Ершов")
    ]
    own = EmployeeFactory(
        last_name="Сотрудников",
        user=users["employee"],
        branch=branch,
        department=department,
        position=position,
    )

    entry = ReserveEntryFactory(employee=employees[0], target_position=position)
    ReserveEntryFactory(employee=own, target_position=PositionFactory(department=department))

    plan = DevelopmentPlanFactory(
        employee=own,
        title="Подготовка к роли руководителя отдела",
    )
    DevelopmentActivityFactory(plan=plan, title="Курс управления командой")

    # Ячейка матрицы не задаётся, а вычисляется из координат при сохранении: она — результат
    # правила, а не поле ввода.
    for index, employee in enumerate(employees):
        NineBoxPlacementFactory(
            employee=employee,
            performance=Decimal("85.00") - index * 15,
            potential=Decimal("80.00") - index * 10,
        )

    # Публичная часть: калькулятор требует действующей схемы весов,
    # база знаний — статей, глоссарий — терминов.
    ensure_criteria()
    ensure_default_scheme(author=users["hr"])

    # Каталог отчётов наполняется штатной командой, а не фабриками: состав каталога — часть
    # поставки, и сценарий обязан проверять именно его.
    call_command("seed_reporting")

    category = ArticleCategoryFactory(name="Методика")
    # Адрес статьи не задаётся: он выводится из заголовка при сохранении и обязан оставаться
    # выводимым — сценарий базы знаний переходит по ссылке из списка, а не по заранее известному
    # адресу.
    ArticleFactory(category=category, title="Как формируется кадровый резерв")
    for term in ("Кадровый резерв", "Матрица 9-box", "Индекс готовности"):
        GlossaryTermFactory(term=term)

    return {
        "users": users,
        "employees": employees,
        "own": own,
        "entry": entry,
        "position": position,
        "department": department,
    }


@pytest.fixture
def site(live_server, e2e_data) -> str:
    """Базовый адрес запущенного экземпляра системы."""
    return live_server.url


@pytest.fixture
def login(page: Page, site: str):
    """Выполняет вход через настоящую форму."""

    def _login(role: str = "hr") -> Page:
        # Сначала завершается текущий сеанс.
        page.goto(f"{site}/account/login/")
        if page.locator("#id_username").count() == 0:
            page.goto(f"{site}/")
            page.click("[data-dropdown-trigger='user']")
            page.click("form[action*='logout'] button[type=submit]")
            page.goto(f"{site}/account/login/")
        page.fill("#id_username", f"{role}@e2e.reservix.ru")
        page.fill("#id_password", E2E_PASSWORD)
        # Кнопка отбирается по принадлежности к содержимому страницы: в шапке и подвале стоят формы
        # переключения языка, и их кнопки отправки оказываются в разметке раньше.
        page.click("main form button[type=submit]")
        page.wait_for_load_state("networkidle")
        return page

    return _login
