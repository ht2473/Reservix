"""Обработчики сигналов приложения учётных записей."""

from __future__ import annotations

from typing import Any

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.accounts.models import User, UserProfile
from core.constants import Role, ScopeLevel
from core.logging import get_logger

logger = get_logger("accounts")

#: Область видимости, назначаемая по умолчанию при выдаче роли.
DEFAULT_SCOPE_BY_ROLE: dict[str, str] = {
    Role.ADMIN: ScopeLevel.ORGANIZATION,
    Role.HR: ScopeLevel.ORGANIZATION,
    Role.MANAGER: ScopeLevel.SUBORDINATES,
    Role.MENTOR: ScopeLevel.SELF,
    Role.EMPLOYEE: ScopeLevel.SELF,
}


@receiver(pre_save, sender=User, dispatch_uid="accounts_track_role_change")
def track_role_change(sender: type[User], instance: User, **kwargs: Any) -> None:
    """Приводит область видимости в соответствие новой роли."""
    if instance.pk is None:
        # Новая запись: область берётся из роли, если не задана явно.
        if instance.scope_level == ScopeLevel.SELF:
            instance.scope_level = DEFAULT_SCOPE_BY_ROLE.get(instance.role, ScopeLevel.SELF)
        return

    previous = User.objects.filter(pk=instance.pk).values("role", "scope_level").first()
    if previous is None or previous["role"] == instance.role:
        return

    default_scope = DEFAULT_SCOPE_BY_ROLE.get(instance.role, ScopeLevel.SELF)

    # Порядок широты областей: организация — филиал — подразделение —
    # подчинённые — только свои данные.
    breadth: dict[str, int] = {
        ScopeLevel.ORGANIZATION: 4,
        ScopeLevel.BRANCH: 3,
        ScopeLevel.DEPARTMENT: 2,
        ScopeLevel.SUBORDINATES: 1,
        ScopeLevel.SELF: 0,
    }

    if breadth.get(instance.scope_level, 0) > breadth.get(default_scope, 0):
        logger.info(
            "Область видимости сужена вслед за изменением роли",
            extra={
                "user_id": instance.pk,
                "role_from": previous["role"],
                "role_to": instance.role,
                "scope_from": instance.scope_level,
                "scope_to": default_scope,
            },
        )
        instance.scope_level = default_scope


@receiver(post_save, sender=User, dispatch_uid="accounts_create_profile")
def create_user_profile(sender: type[User], instance: User, created: bool, **kwargs: Any) -> None:
    """Создаёт профиль настроек вместе с учётной записью."""
    if created:
        UserProfile.objects.get_or_create(user=instance)
