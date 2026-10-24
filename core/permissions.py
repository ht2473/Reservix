"""Права доступа: роль и область видимости."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from django.core.exceptions import PermissionDenied
from django.db.models import Q, QuerySet

from core.constants import (
    ADMIN_ROLES,
    ASSESSOR_ROLES,
    DECISION_ROLES,
    PRIVILEGED_ROLES,
    Role,
    ScopeLevel,
)

if TYPE_CHECKING:
    from django.http import HttpRequest

    from apps.accounts.models import User


def current_user(request: HttpRequest) -> User:
    """Учётная запись пользователя запроса, прошедшего проверку входа."""
    return cast("User", request.user)


# ---------------------------------------------------------------------------
# Проверки роли
# ---------------------------------------------------------------------------
def has_role(user: User | None, *roles: str) -> bool:
    """Проверяет принадлежность пользователя к одной из перечисленных ролей."""
    if user is None or not user.is_authenticated:
        return False
    return user.role in roles


def is_admin(user: User | None) -> bool:
    """Администратор системы."""
    return has_role(user, *ADMIN_ROLES)


def is_privileged(user: User | None) -> bool:
    """Роль с доступом к персональным данным всех сотрудников."""
    return has_role(user, *PRIVILEGED_ROLES)


def can_decide_reserve(user: User | None) -> bool:
    """Право принимать решение о составе кадрового резерва."""
    return has_role(user, *DECISION_ROLES)


def can_assess(user: User | None) -> bool:
    """Право выставлять оценки сотрудникам."""
    return has_role(user, *ASSESSOR_ROLES)


def require_role(user: User | None, *roles: str) -> None:
    """Требует наличия роли, иначе прерывает обработку запроса."""
    if not has_role(user, *roles):
        raise PermissionDenied("Недостаточно прав для выполнения операции")


# ---------------------------------------------------------------------------
# Область видимости
# ---------------------------------------------------------------------------
def employee_scope_filter(user: User) -> Q:
    """Строит условие отбора сотрудников, доступных пользователю."""
    scope = user.scope_level

    if scope == ScopeLevel.ORGANIZATION:
        return Q()

    if scope == ScopeLevel.BRANCH:
        return Q(branch_id=user.branch_id) if user.branch_id else Q(pk__in=[])

    if scope == ScopeLevel.DEPARTMENT:
        return Q(department_id=user.department_id) if user.department_id else Q(pk__in=[])

    if scope == ScopeLevel.SUBORDINATES:
        # Руководитель видит сотрудников своего подразделения и всех вложенных
        # подразделений: организационная структура иерархична.
        if not user.department_id:
            return Q(pk__in=[])
        from apps.org.models import Department

        subtree = Department.objects.descendant_ids(user.department_id)
        return Q(department_id__in=subtree)

    # ScopeLevel.SELF и любое неизвестное значение: только собственная карточка.
    return Q(user_id=user.pk)


def apply_employee_scope[QS: QuerySet](queryset: QS, user: User) -> QS:
    """Ограничивает выборку сотрудников областью видимости пользователя."""
    if user.scope_level == ScopeLevel.ORGANIZATION:
        return queryset
    return queryset.filter(employee_scope_filter(user))


def apply_scope_by_path[QS: QuerySet](queryset: QS, user: User, *, prefix: str) -> QS:
    """Применяет ограничение видимости к модели, связанной с сотрудником."""
    if user.scope_level == ScopeLevel.ORGANIZATION:
        return queryset
    return queryset.filter(scope_condition(user, prefix=prefix))


def scope_condition(user: User, *, prefix: str = "") -> Q:
    """Условие видимости для модели, связанной с сотрудником."""
    base = employee_scope_filter(user)
    if not prefix:
        return base
    return _prefix_q(base, prefix)


def _prefix_q(condition: Q, prefix: str) -> Q:
    """Рекурсивно добавляет префикс связи ко всем полям условия."""
    result = Q()
    result.connector = condition.connector
    result.negated = condition.negated

    for child in condition.children:
        if isinstance(child, Q):
            result.children.append(_prefix_q(child, prefix))
        elif isinstance(child, tuple):
            lookup, value = child
            result.children.append((f"{prefix}__{lookup}", value))
        else:  # pragma: no cover — форма узла изменилась в новой версии Django
            result.children.append(child)

    return result


# ---------------------------------------------------------------------------
# Объектные проверки
# ---------------------------------------------------------------------------
def can_view_employee(user: User, employee) -> bool:
    """Проверяет право просмотра карточки конкретного сотрудника."""
    if employee.user_id == user.pk:
        return True

    # Роль определяет операции, область видимости — круг сотрудников: кадровая
    # служба филиала видит только свой филиал.
    scope = user.scope_level
    if scope == ScopeLevel.ORGANIZATION:
        return is_privileged(user)
    if scope == ScopeLevel.BRANCH:
        return employee.branch_id == user.branch_id
    if scope == ScopeLevel.DEPARTMENT:
        return employee.department_id == user.department_id
    if scope == ScopeLevel.SUBORDINATES:
        from apps.org.models import Department

        if not user.department_id:
            return False
        return employee.department_id in Department.objects.descendant_ids(user.department_id)
    return False


def can_edit_employee(user: User, employee) -> bool:
    """Право редактирования карточки сотрудника."""
    if is_privileged(user):
        return can_view_employee(user, employee)
    return employee.user_id == user.pk


def can_manage_reserve_entry(user: User, entry) -> bool:
    """Право изменять запись кадрового резерва."""
    if is_admin(user):
        return True
    if user.role == Role.HR:
        return can_view_employee(user, entry.employee)
    return False


def can_approve_nomination(user: User, nomination) -> bool:
    """Право согласовать заявку на включение в резерв."""
    if is_privileged(user):
        return can_view_employee(user, nomination.employee)
    if user.role != Role.MANAGER or not user.is_department_head:
        return False
    if nomination.employee.user_id == user.pk:
        return False
    return nomination.employee.department_id == user.department_id


def can_view_assessment(user: User, session) -> bool:
    """Право просмотра сессии оценки."""
    if session.assessor_id == user.pk:
        return True
    return can_view_employee(user, session.employee)


def ensure_object_permission(user: User, obj, check) -> None:
    """Применяет объектную проверку, прерывая обработку при отказе."""
    if not check(user, obj):
        raise PermissionDenied("Объект недоступен для текущего пользователя")
