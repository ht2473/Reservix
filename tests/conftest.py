"""Общие фикстуры тестов Reservix."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from django.conf import settings
from django.core.cache import cache
from django.test import Client

from tests.factories import (
    BranchFactory,
    DepartmentFactory,
    PositionFactory,
    PositionGradeFactory,
    UserFactory,
)


# ---------------------------------------------------------------------------
# Окружение
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_cache():
    """Очищает кеш перед каждым тестом."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(scope="session", autouse=True)
def _media_root():
    """Удаляет временный каталог медиафайлов по завершении сессии тестов."""
    yield
    media = Path(settings.MEDIA_ROOT)
    if media.exists() and ".pytest-media" in str(media):
        shutil.rmtree(media, ignore_errors=True)


# ---------------------------------------------------------------------------
# Организационная структура
# ---------------------------------------------------------------------------
@pytest.fixture
def branch(db):
    """Головной офис."""
    return BranchFactory(name="Головной офис", city="Москва", is_head_office=True)


@pytest.fixture
def other_branch(db):
    """Второй филиал — нужен для проверки ограничения видимости."""
    return BranchFactory(name="Филиал Новосибирск", city="Новосибирск")


@pytest.fixture
def department(branch):
    """Подразделение верхнего уровня."""
    return DepartmentFactory(name="Департамент разработки", branch=branch)


@pytest.fixture
def child_department(department):
    """Вложенное подразделение — проверка иерархии и области видимости."""
    return DepartmentFactory(
        name="Отдел серверной разработки",
        branch=department.branch,
        parent=department,
    )


@pytest.fixture
def other_department(branch):
    """Подразделение вне поддерева, к которому не должно быть доступа."""
    return DepartmentFactory(name="Департамент продаж", branch=branch)


@pytest.fixture
def grade(db):
    """Управленческий грейд."""
    return PositionGradeFactory(name="Руководитель отдела", level=8, is_managerial=True)


@pytest.fixture
def position(grade, department):
    """Критичная должность."""
    return PositionFactory(
        name="Руководитель отдела разработки",
        grade=grade,
        department=department,
        is_key_position=True,
    )


# ---------------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------------
@pytest.fixture
def admin_user(branch, department):
    """Администратор системы с видимостью на всю организацию."""
    return UserFactory(
        role="admin",
        scope_level="organization",
        branch=branch,
        department=department,
        is_superuser=True,
        is_staff=True,
    )


@pytest.fixture
def hr_user(branch, department):
    """HR-специалист с видимостью на всю организацию."""
    return UserFactory(role="hr", scope_level="organization", branch=branch, department=department)


@pytest.fixture
def manager_user(branch, department):
    """Руководитель подразделения: видит своё подразделение и вложенные."""
    return UserFactory(
        role="manager",
        scope_level="subordinates",
        branch=branch,
        department=department,
        is_department_head=True,
    )


@pytest.fixture
def employee_user(branch, department):
    """Рядовой сотрудник: видит только собственные данные."""
    return UserFactory(role="employee", scope_level="self", branch=branch, department=department)


@pytest.fixture
def mentor_user(branch, department):
    """Наставник."""
    return UserFactory(role="mentor", scope_level="self", branch=branch, department=department)


# ---------------------------------------------------------------------------
# Клиенты
# ---------------------------------------------------------------------------
@pytest.fixture
def client() -> Client:
    """Клиент без авторизации."""
    return Client()


def _logged_in(user) -> Client:
    """Создаёт отдельный клиент, авторизованный указанным пользователем."""
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def auth_client(employee_user) -> Client:
    """Клиент, авторизованный рядовым сотрудником."""
    return _logged_in(employee_user)


@pytest.fixture
def hr_client(hr_user) -> Client:
    """Клиент, авторизованный HR-специалистом."""
    return _logged_in(hr_user)


@pytest.fixture
def admin_client(admin_user) -> Client:
    """Клиент, авторизованный администратором."""
    return _logged_in(admin_user)


@pytest.fixture
def manager_client(manager_user) -> Client:
    """Клиент, авторизованный руководителем подразделения."""
    return _logged_in(manager_user)


@pytest.fixture
def mentor_client(mentor_user) -> Client:
    """Клиент, авторизованный наставником."""
    return _logged_in(mentor_user)


# ---------------------------------------------------------------------------
# Вспомогательные фикстуры
# ---------------------------------------------------------------------------
@pytest.fixture
def known_password() -> str:
    """Пароль, удовлетворяющий политике сложности."""
    return "Nadezhnyi-Parol-2026"
