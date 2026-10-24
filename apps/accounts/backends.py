"""Бэкенд аутентификации по адресу электронной почты."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

from core.logging import get_logger

if TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser
    from django.http import HttpRequest

    from apps.accounts.models import User

logger = get_logger("accounts")


class EmailBackend(ModelBackend):
    """Проверка учётных данных по адресу электронной почты."""

    def authenticate(
        self,
        request: HttpRequest | None,
        username: str | None = None,
        password: str | None = None,
        **kwargs,
    ) -> User | None:
        user_model = get_user_model()

        # Форма входа передаёт значение в поле `username`, программные вызовы —
        # в `email`. Поддерживаются оба варианта.
        email = username or kwargs.get("email") or kwargs.get(user_model.USERNAME_FIELD)

        if not email or not password:
            return None

        try:
            user = user_model.objects.get(Q(email__iexact=email.strip()))
        except user_model.DoesNotExist:
            # Хеширование выполняется даже для несуществующего адреса: иначе мгновенный отказ
            # выдаёт отсутствие учётной записи, и перебором можно составить список сотрудников
            # организации.
            user_model().set_password(password)
            return None
        except user_model.MultipleObjectsReturned:  # pragma: no cover — рассинхронизация данных
            logger.error("Найдено несколько учётных записей с адресом", extra={"email": email})
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user

        return None

    def user_can_authenticate(self, user: User | AnonymousUser | None) -> bool:
        """Дополняет базовую проверку активности."""
        # Анонимный посетитель и отсутствующая запись отсекаются до
        # обращения к полям: у AnonymousUser нет ни адреса, ни ключа.
        if user is None or not user.is_authenticated:
            return False
        if not user.is_active:
            logger.warning(
                "Попытка входа в заблокированную учётную запись",
                extra={"user_id": user.pk, "email": user.email},
            )
            return False
        return True
