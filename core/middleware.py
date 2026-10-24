"""Промежуточные слои обработки запроса."""

from __future__ import annotations

import zoneinfo
from collections.abc import Callable
from typing import ClassVar

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils import timezone


class TimezoneMiddleware:
    """Переключение часового пояса на пояс пользователя."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.default_tz = zoneinfo.ZoneInfo(settings.TIME_ZONE)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = getattr(request, "user", None)
        tz_name = ""

        if user is not None and user.is_authenticated:
            tz_name = getattr(user, "timezone", "") or ""

        if tz_name:
            try:
                timezone.activate(zoneinfo.ZoneInfo(tz_name))
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                # Некорректное значение в профиле не должно ронять запрос:
                # молча возвращаемся к поясу организации.
                timezone.activate(self.default_tz)
        else:
            timezone.activate(self.default_tz)

        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()


class SecurityHeadersMiddleware:
    """Защитные заголовки ответа."""

    #: Директивы политики безопасности содержимого.
    CSP_DIRECTIVES: ClassVar[dict[str, list[str]]] = {
        "default-src": ["'self'"],
        "script-src": ["'self'"],
        "style-src": ["'self'"],
        "img-src": ["'self'", "data:", "blob:"],
        "font-src": ["'self'"],
        "connect-src": ["'self'"],
        "frame-ancestors": ["'none'"],
        "form-action": ["'self'"],
        "base-uri": ["'self'"],
        "object-src": ["'none'"],
    }

    #: Отключение браузерных возможностей, которые системе не нужны.
    PERMISSIONS_POLICY = (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    )

    #: Пути, на которых политика ослабляется.
    RELAXED_PATH_PREFIXES = ("/api/v1/docs",)

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.policy = self._build_policy(self.CSP_DIRECTIVES)
        relaxed = {
            **self.CSP_DIRECTIVES,
            "script-src": ["'self'", "'unsafe-inline'"],
            "style-src": ["'self'", "'unsafe-inline'"],
        }
        self.relaxed_policy = self._build_policy(relaxed)

    @staticmethod
    def _build_policy(directives: dict[str, list[str]]) -> str:
        return "; ".join(f"{name} {' '.join(values)}" for name, values in directives.items())

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)

        if request.path.startswith(self.RELAXED_PATH_PREFIXES):
            policy = self.relaxed_policy
        else:
            policy = self.policy

        # В режиме отладки политика переводится в режим наблюдения: панель отладки использует
        # встроенные скрипты, блокировка которых сделала бы её бесполезной, но нарушения
        # по-прежнему видны в консоли браузера.
        header = (
            "Content-Security-Policy-Report-Only" if settings.DEBUG else "Content-Security-Policy"
        )
        response.headers.setdefault(header, policy)
        response.headers.setdefault("Permissions-Policy", self.PERMISSIONS_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")

        return response
