"""Служебные представления."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import translate_url
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import (
    activate,
    check_for_language,
    get_language,
    get_language_from_path,
)
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import requires_csrf_token
from django.views.decorators.http import require_GET, require_POST

from core.breadcrumbs import BreadcrumbTrail
from core.logging import get_logger

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

logger = get_logger("core")


# ---------------------------------------------------------------------------
# Переключение языка интерфейса
# ---------------------------------------------------------------------------
def set_language_cookie(response: HttpResponse, language: str) -> HttpResponse:
    """Записывает выбранный язык в cookie ответа."""
    response.set_cookie(
        settings.LANGUAGE_COOKIE_NAME,
        language,
        max_age=settings.LANGUAGE_COOKIE_AGE,
        path=settings.LANGUAGE_COOKIE_PATH,
        domain=settings.LANGUAGE_COOKIE_DOMAIN,
        secure=settings.LANGUAGE_COOKIE_SECURE,
        httponly=settings.LANGUAGE_COOKIE_HTTPONLY,
        # Настройка объявлена строкой, подпись метода требует литерального типа: приведение
        # выражает то же значение в терминах, понятных проверке типов.
        samesite=cast("Literal['Lax', 'Strict', 'None'] | None", settings.LANGUAGE_COOKIE_SAMESITE),
    )
    return response


@require_POST
def set_language(request: HttpRequest) -> HttpResponse:
    """Переключает язык интерфейса и переводит адрес возврата."""
    language = request.POST.get("language", "")
    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER", "")

    if not url_has_allowed_host_and_scheme(
        url=next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = "/"

    if not language or not check_for_language(language):
        return HttpResponseRedirect(next_url)

    # Язык адреса возврата, а не действующий: разрешение маршрута должно
    # выполняться в том языке, в котором адрес был построен.
    source_language = get_language_from_path(next_url) or settings.LANGUAGE_CODE
    previous = get_language()
    try:
        activate(source_language)
        translated = translate_url(next_url, language)
    finally:
        activate(previous or settings.LANGUAGE_CODE)

    return set_language_cookie(HttpResponseRedirect(translated), language)


# ---------------------------------------------------------------------------
# Обработчики ошибок
# ---------------------------------------------------------------------------
def _error_context(
    request: HttpRequest, code: int, title: StrOrPromise, message: StrOrPromise
) -> dict:
    """Общий контекст страницы ошибки."""
    trail = BreadcrumbTrail(request).add(str(title))
    return {
        "error_code": code,
        "page_title": str(title),
        "error_message": str(message),
        "breadcrumbs": trail.resolve(),
        "request_path": request.path,
    }


def bad_request(request: HttpRequest, exception=None) -> HttpResponse:
    """Обработчик 400: некорректный запрос."""
    context = _error_context(
        request,
        400,
        _("Некорректный запрос"),
        _("Сервер не смог обработать запрос. Проверьте адрес страницы и повторите попытку."),
    )
    return render(request, "errors/400.html", context, status=400)


@requires_csrf_token
def permission_denied(request: HttpRequest, exception=None) -> HttpResponse:
    """Обработчик 403: доступ запрещён."""
    logger.warning(
        "Отказано в доступе",
        extra={
            "path": request.path,
            "user_id": getattr(request.user, "pk", None),
            "role": getattr(request.user, "role", None),
        },
    )
    detail = str(exception) if exception else ""
    context = _error_context(
        request,
        403,
        _("Доступ запрещён"),
        detail or _("У вашей роли нет прав на просмотр этого раздела."),
    )
    return render(request, "errors/403.html", context, status=403)


def page_not_found(request: HttpRequest, exception=None) -> HttpResponse:
    """Обработчик 404: страница не найдена."""
    context = _error_context(
        request,
        404,
        _("Страница не найдена"),
        _("Запрошенная страница не существует или была перемещена."),
    )
    return render(request, "errors/404.html", context, status=404)


@requires_csrf_token
def server_error(request: HttpRequest) -> HttpResponse:
    """Обработчик 500: внутренняя ошибка."""
    return render(
        request,
        "errors/500.html",
        {
            "error_code": 500,
            "page_title": str(_("Внутренняя ошибка")),
            "error_message": str(_("Произошла непредвиденная ошибка. Администратор уведомлён.")),
        },
        status=500,
    )


@requires_csrf_token
def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """Обработчик отказа проверки CSRF."""
    logger.warning("Отказ проверки CSRF", extra={"path": request.path, "reason": reason})
    context = _error_context(
        request,
        403,
        _("Сессия устарела"),
        _("Время ожидания истекло. Обновите страницу и заполните форму заново."),
    )
    context["csrf_reason"] = reason
    return render(request, "errors/csrf.html", context, status=403)


# ---------------------------------------------------------------------------
# Диагностика
# ---------------------------------------------------------------------------
@require_GET
@never_cache
def health_check(request: HttpRequest) -> JsonResponse:
    """Проверка работоспособности сервиса."""
    checks: dict[str, str] = {}
    healthy = True

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = "fail"
        healthy = False
        logger.error("Проверка состояния: база данных недоступна", extra={"error": str(exc)})

    try:
        from django.core.cache import cache

        cache.set("healthcheck", "ok", 10)
        checks["cache"] = "ok" if cache.get("healthcheck") == "ok" else "degraded"
    except Exception as exc:
        # Недоступность кэша не выводит приложение из ротации: система
        # продолжает работать, теряя только скорость.
        checks["cache"] = "fail"
        logger.warning("Проверка состояния: кэш недоступен", extra={"error": str(exc)})

    return JsonResponse(
        {"status": "ok" if healthy else "unavailable", "checks": checks},
        status=200 if healthy else 503,
    )
