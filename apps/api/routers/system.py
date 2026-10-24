"""Служебные методы: состояние системы и сведения о текущем пользователе."""

from ninja import Router

from django.conf import settings
from django.db import connection
from django.http import HttpRequest
from django.utils import timezone

from apps.api.auth import current_user
from apps.api.schemas import CurrentUserSchema, HealthSchema

router = Router(tags=["Служебные"])


@router.get(
    "/health",
    response=HealthSchema,
    auth=None,
    summary="Состояние службы",
    description=(
        "Проверка доступности приложения и базы данных. Метод открыт без "
        "аутентификации: его опрашивает контейнерное окружение и внешний "
        "мониторинг, у которых учётной записи нет."
    ),
)
def health(request: HttpRequest) -> dict[str, object]:
    """Возвращает состояние приложения и подключения к базе данных."""
    database_ready = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # pragma: no cover - проверка отказа базы данных
        database_ready = False

    return {
        "status": "ok" if database_ready else "degraded",
        "version": settings.API_VERSION,
        "database": database_ready,
        "server_time": timezone.now(),
    }


@router.get(
    "/me",
    response=CurrentUserSchema,
    summary="Текущий пользователь",
    description=(
        "Роль и область видимости вызывающего. Потребитель интерфейса должен "
        "знать границы своих прав до запроса данных, иначе единственным "
        "способом их выяснить остаётся перебор методов до отказа."
    ),
)
def me(request: HttpRequest) -> dict[str, object]:
    """Возвращает сведения о вызывающем пользователе."""
    user = current_user(request)
    employee = getattr(user, "employee_card", None)

    return {
        "id": user.pk,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "role_label": user.role_label,
        "scope_level": user.scope_level,
        "branch_id": user.branch_id,
        "department_id": user.department_id,
        "is_department_head": user.is_department_head,
        "employee_public_id": employee.public_id if employee else None,
    }
