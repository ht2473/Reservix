"""Сборка программного интерфейса."""

from __future__ import annotations

import logging

from ninja import NinjaAPI
from ninja.errors import AuthenticationError, HttpError
from ninja.errors import ValidationError as NinjaValidationError

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest

from apps.api.auth import basic_auth, session_auth
from apps.api.routers import (
    analytics,
    assessment,
    competencies,
    development,
    people,
    reserve,
    succession,
    system,
)
from apps.api.routers import org as org_router

logger = logging.getLogger("reservix.api")

#: Описание интерфейса для страницы документации.
DESCRIPTION = """
Программный интерфейс информационной системы формирования и развития
кадрового резерва.

**Аутентификация.** Поддерживаются два способа: сеанс веб-интерфейса
(для запросов из самой системы) и базовая аутентификация HTTP по адресу
электронной почты и паролю (для внешних потребителей).

**Область видимости.** Каждый метод возвращает данные в границах прав
вызывающего. Руководитель видит своё подразделение с вложенными,
кадровая служба — организацию, сотрудник — собственные данные.
Отсутствие записи в ответе не означает, что её нет в системе.

**Постраничная навигация.** Списочные методы принимают параметры
`limit` и `offset` и возвращают объект с полями `items` и `count`.
"""

api = NinjaAPI(
    title=settings.API_TITLE,
    version=settings.API_VERSION,
    description=DESCRIPTION,
    auth=[session_auth, basic_auth],
    urls_namespace="api",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

api.add_router("/system", system.router)
api.add_router("/org", org_router.router)
api.add_router("/people", people.router)
api.add_router("/competencies", competencies.router)
api.add_router("/assessment", assessment.router)
api.add_router("/reserve", reserve.router)
api.add_router("/succession", succession.router)
api.add_router("/development", development.router)
api.add_router("/analytics", analytics.router)


# ---------------------------------------------------------------------------
# Обработка ошибок
# ---------------------------------------------------------------------------
@api.exception_handler(AuthenticationError)
def on_authentication_error(request: HttpRequest, exc: AuthenticationError):
    """Отказ в аутентификации: учётные данные не предъявлены либо неверны."""
    return api.create_response(
        request,
        {
            "code": "unauthorized",
            "detail": "Требуется аутентификация: сеанс системы либо базовая аутентификация HTTP",
        },
        status=401,
    )


@api.exception_handler(PermissionDenied)
def on_permission_denied(request: HttpRequest, exc: PermissionDenied):
    """Отказ в правах: пользователь опознан, но операция ему недоступна."""
    return api.create_response(
        request,
        {
            "code": "forbidden",
            "detail": str(exc) or "Недостаточно прав для выполнения операции",
        },
        status=403,
    )


@api.exception_handler(Http404)
def on_not_found(request: HttpRequest, exc: Http404):
    """Запись не найдена либо недоступна вызывающему."""
    return api.create_response(
        request,
        {"code": "not_found", "detail": "Запись не найдена"},
        status=404,
    )


@api.exception_handler(NinjaValidationError)
def on_request_validation_error(request: HttpRequest, exc: NinjaValidationError):
    """Ошибка разбора запроса."""
    fields: dict[str, list[str]] = {}
    for error in exc.errors:
        # Путь ошибки содержит служебные звенья: источник данных («body», «query») и имя параметра
        # обработчика.
        location = [str(part) for part in error.get("loc", ()) if not str(part).isdigit()]
        name = location[-1] if location else "__all__"
        fields.setdefault(name, []).append(str(error.get("msg", "")))

    return api.create_response(
        request,
        {
            "code": "validation_error",
            "detail": "Запрос содержит недопустимые значения",
            "fields": fields,
        },
        status=422,
    )


@api.exception_handler(ValidationError)
def on_model_validation_error(request: HttpRequest, exc: ValidationError):
    """Ошибка проверки данных на уровне модели: нарушено правило предметной области."""
    fields = exc.message_dict if hasattr(exc, "message_dict") else {"__all__": exc.messages}
    return api.create_response(
        request,
        {
            "code": "invalid_data",
            "detail": "Данные не соответствуют правилам предметной области",
            "fields": fields,
        },
        status=400,
    )


@api.exception_handler(HttpError)
def on_http_error(request: HttpRequest, exc: HttpError):
    """Явно возбуждённая ошибка обработчика."""
    return api.create_response(
        request,
        {"code": "http_error", "detail": str(exc)},
        status=exc.status_code,
    )


@api.exception_handler(Exception)
def on_unexpected_error(request: HttpRequest, exc: Exception):
    """Непредвиденная ошибка."""
    logger.exception(
        "Непредвиденная ошибка программного интерфейса",
        extra={"path": request.path, "method": request.method},
    )
    if settings.DEBUG:
        raise exc
    return api.create_response(
        request,
        {
            "code": "internal_error",
            "detail": "Внутренняя ошибка службы. Обратитесь к администратору системы",
        },
        status=500,
    )
