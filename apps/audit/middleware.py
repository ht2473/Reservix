"""Контекст запроса для журнала аудита."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.http import HttpRequest, HttpResponse

if TYPE_CHECKING:
    from apps.accounts.models import User


@dataclass(slots=True)
class AuditContext:
    """Сведения о текущем запросе, необходимые журналу аудита."""

    actor: User | None = None
    ip_address: str | None = None
    user_agent: str = ""
    request_path: str = ""
    request_method: str = ""
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    reason: str = ""

    @property
    def actor_repr(self) -> str:
        if self.actor is None:
            return "Система"
        return f"{self.actor.short_name} <{self.actor.email}>"

    @property
    def actor_role(self) -> str:
        return getattr(self.actor, "role", "") or ""


#: Контекст текущего запроса.
# Значение по умолчанию не задаётся объектом: единственный экземпляр стал бы общим для всех
# контекстов выполнения, и основание, записанное в фоновой задаче, протекло бы в соседнюю.
_audit_context: ContextVar[AuditContext | None] = ContextVar("audit_context", default=None)


def get_audit_context() -> AuditContext:
    """Возвращает контекст текущего запроса."""
    context = _audit_context.get()
    if context is None:
        context = AuditContext()
        _audit_context.set(context)
    return context


def set_audit_reason(reason: str) -> None:
    """Задаёт основание для последующих записей журнала."""
    get_audit_context().reason = reason


@contextmanager
def audit_as(actor: User | None, *, reason: str = ""):
    """Временно подменяет контекст аудита."""
    token = _audit_context.set(AuditContext(actor=actor, reason=reason))
    try:
        yield
    finally:
        _audit_context.reset(token)


class AuditContextMiddleware:
    """Наполняет контекст аудита данными запроса."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = getattr(request, "user", None)
        actor = user if (user is not None and user.is_authenticated) else None

        context = AuditContext(
            actor=actor,
            ip_address=self._client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:400],
            request_path=request.path[:255],
            request_method=request.method or "",
        )

        token = _audit_context.set(context)
        # Идентификатор запроса доступен представлениям и попадает в заголовок ответа: по нему
        # администратор связывает жалобу пользователя с записями журнала и строками системного
        # лога.
        request.audit_context = context  # type: ignore[attr-defined]

        try:
            response = self.get_response(request)
            response.headers.setdefault("X-Request-ID", context.request_id)
            return response
        finally:
            _audit_context.reset(token)

    @staticmethod
    def _client_ip(request: HttpRequest) -> str | None:
        """Определяет адрес клиента с учётом обратного прокси."""
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
        return request.META.get("REMOTE_ADDR")
