"""Аутентификация программного интерфейса."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ninja.security import HttpBasicAuth, django_auth

from django.contrib.auth import authenticate
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest

from core.constants import Role

if TYPE_CHECKING:
    from apps.accounts.models import User

#: Сеансовая аутентификация: пользователь вошёл в систему через веб-интерфейс.
session_auth = django_auth


class BasicAuth(HttpBasicAuth):
    """Базовая аутентификация по адресу почты и паролю."""

    def authenticate(self, request: HttpRequest, username: str, password: str) -> User | None:
        user = authenticate(request, username=username, password=password)
        if user is None or not user.is_active:
            return None

        # Учётная запись помещается в запрос.
        request.user = user
        return user


basic_auth = BasicAuth()


def current_user(request: HttpRequest) -> User:
    """Возвращает учётную запись вызывающего."""
    user = request.user
    if not user.is_authenticated:
        raise PermissionDenied("Требуется аутентификация")
    return user


def require_roles(request: HttpRequest, *roles: str) -> bool:
    """Проверяет принадлежность пользователя к одной из ролей."""
    user = getattr(request, "user", None)
    return bool(user and user.is_authenticated and user.role in roles)


def is_privileged(request: HttpRequest) -> bool:
    """Признак доступа к персональным данным всей организации."""
    return require_roles(request, Role.ADMIN, Role.HR)
