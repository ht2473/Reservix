"""
Формы раздела уведомлений.
"""

from __future__ import annotations

from typing import cast

from django import forms
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.notifications.models import NotificationKind, NotificationQuerySet
from core.forms import FilterForm


class NotificationFilterForm(FilterForm):
    """Отбор уведомлений по состоянию прочтения и поводу."""

    STATE_ALL = "all"
    STATE_UNREAD = "unread"
    STATE_ACTIONABLE = "actionable"

    state = forms.ChoiceField(
        label=_("Показывать"),
        choices=[
            (STATE_UNREAD, _("Непрочитанные")),
            (STATE_ACTIONABLE, _("Требующие действия")),
            (STATE_ALL, _("Все")),
        ],
        required=False,
    )
    kind = forms.ChoiceField(
        label=_("Повод"),
        choices=[("", _("Любой")), *NotificationKind.choices],
        required=False,
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        # Приведение выражает то, что известно вызывающей стороне: представление передаёт именно
        # выборку уведомлений с её собственными методами.
        notifications = cast(NotificationQuerySet, queryset)
        state = self.cleaned_data.get("state") or self.STATE_UNREAD

        if state == self.STATE_UNREAD:
            notifications = notifications.unread()
        elif state == self.STATE_ACTIONABLE:
            notifications = notifications.actionable()

        kind = self.cleaned_data.get("kind")
        if kind:
            notifications = notifications.filter(kind=kind)

        return notifications

    @property
    def active_count(self) -> int:
        """Число применённых условий отбора."""
        if not self.is_valid():
            return 0
        count = 1 if self.cleaned_data.get("kind") else 0
        state = self.cleaned_data.get("state")
        if state and state != self.STATE_UNREAD:
            count += 1
        return count
