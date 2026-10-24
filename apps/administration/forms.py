"""Формы панели администрирования."""

from __future__ import annotations

from typing import Any

from django import forms
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import User
from apps.org.models import Branch, Department
from apps.portal.models import Feedback
from core.constants import Role, ScopeLevel
from core.forms import BaseModelForm, FilterForm


class UserFilterForm(FilterForm):
    """Отбор учётных записей по роли, состоянию и подразделению."""

    q = forms.CharField(
        label=_("Поиск"),
        widget=forms.TextInput(
            attrs={
                "type": "search",
                "placeholder": _("Фамилия, имя или адрес почты"),
                "autocomplete": "off",
            }
        ),
    )
    role = forms.ChoiceField(label=_("Роль"), choices=[("", _("Все роли")), *Role.choices])
    branch = forms.ModelChoiceField(
        label=_("Филиал"),
        queryset=Branch.objects.all(),
        empty_label=_("Все филиалы"),
    )
    is_active = forms.ChoiceField(
        label=_("Состояние"),
        choices=[("", _("Любое")), ("1", _("Активные")), ("0", _("Заблокированные"))],
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        """Применяет условия отбора к набору учётных записей."""
        data = self.cleaned_data or {}

        if term := data.get("q"):
            queryset = queryset.filter(
                Q(last_name__icontains=term)
                | Q(first_name__icontains=term)
                | Q(email__icontains=term)
            )
        if role := data.get("role"):
            queryset = queryset.filter(role=role)
        if branch := data.get("branch"):
            queryset = queryset.filter(branch=branch)
        state = data.get("is_active")
        if state in {"0", "1"}:
            queryset = queryset.filter(is_active=state == "1")

        return queryset


class UserForm(BaseModelForm):
    """Создание и изменение учётной записи."""

    class Meta:
        model = User
        fields = [
            "email",
            "last_name",
            "first_name",
            "middle_name",
            "phone",
            "role",
            "scope_level",
            "branch",
            "department",
            "is_department_head",
            "is_active",
        ]
        widgets = {
            "email": forms.EmailInput(attrs={"autocomplete": "off"}),
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        department_field = self.fields["department"]
        assert isinstance(department_field, forms.ModelChoiceField)
        department_field.queryset = Department.objects.select_related("branch")
        self.fields["scope_level"].help_text = _(
            "Роль определяет доступные операции, область видимости — чьи данные доступны"
        )

    def clean(self) -> dict[str, Any]:
        """Проверяет согласованность роли, области видимости и подразделения."""
        cleaned = super().clean() or {}
        role = cleaned.get("role")
        scope = cleaned.get("scope_level")
        department = cleaned.get("department")
        is_head = cleaned.get("is_department_head")

        if is_head and department is None:
            self.add_error(
                "department",
                _("Руководителю подразделения необходимо указать подразделение"),
            )

        if scope == ScopeLevel.DEPARTMENT and department is None:
            self.add_error(
                "department",
                _("Область видимости «Подразделение» требует указания подразделения"),
            )

        if scope == ScopeLevel.BRANCH and cleaned.get("branch") is None:
            self.add_error("branch", _("Область видимости «Филиал» требует указания филиала"))

        if role == Role.EMPLOYEE and scope == ScopeLevel.ORGANIZATION:
            self.add_error(
                "scope_level",
                _("Рядовому сотруднику недопустима область видимости всей организации"),
            )

        return cleaned


class FeedbackAnswerForm(BaseModelForm):
    """Ответ на обращение с публичной части сайта."""

    class Meta:
        model = Feedback
        fields = ["status", "answer", "internal_note"]
        widgets = {
            "answer": forms.Textarea(attrs={"rows": 6}),
            "internal_note": forms.Textarea(attrs={"rows": 3}),
        }

    def clean(self) -> dict[str, Any]:
        """Требует текст ответа при переводе обращения в состояние «отвечено»."""
        cleaned = super().clean() or {}
        if cleaned.get("status") == Feedback.Status.ANSWERED and not cleaned.get("answer"):
            self.add_error("answer", _("Укажите текст ответа заявителю"))
        return cleaned
