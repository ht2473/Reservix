"""Формы раздела отчётности."""

from __future__ import annotations

import datetime as dt
from typing import Any

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.reporting.models import ReportCategory, ReportFormat, ReportStatus
from core.constants import ReadinessLevel
from core.forms import BaseForm, FilterForm


class ParameterError(Exception):
    """Описание параметров шаблона составлено неверно."""


def _choices_from(source: str) -> list[tuple[str, str]]:
    """Возвращает варианты выбора для параметра, ссылающегося на справочник."""
    if source == "readiness":
        return [(level.value, str(level.label)) for level in ReadinessLevel]

    if source == "reserve_group":
        from apps.reserve.models import ReserveGroup

        return [
            (group.code, group.name)
            for group in ReserveGroup.objects.filter(is_active=True).order_by("sort_order", "name")
        ]

    if source == "plan_status":
        from apps.development.models import PlanStatus

        return [(status.value, str(status.label)) for status in PlanStatus]

    if source == "branch":
        from apps.org.models import Branch

        return [
            (str(branch.pk), branch.name)
            for branch in Branch.objects.filter(is_active=True).order_by("name")
        ]

    if source == "department":
        from apps.org.models import Department

        return [
            (str(department.pk), department.name)
            for department in Department.objects.filter(is_active=True).order_by("name")
        ]

    raise ParameterError(f"Неизвестный источник значений параметра: {source}")


class ReportRunForm(BaseForm):
    """Форма запуска отчёта по описанию параметров шаблона."""

    #: Типы параметров, поддерживаемые описанием шаблона.
    FIELD_TYPES = {"date", "choice", "boolean", "integer", "text"}

    def __init__(self, *args, schema: dict[str, Any] | None = None, **kwargs) -> None:
        self.schema = schema or {}
        super().__init__(*args, **kwargs)

        for name, spec in self.schema.items():
            self.fields[name] = self._build_field(name, spec)

        self._apply_styling()

    def _build_field(self, name: str, spec: Any) -> forms.Field:
        """Создаёт поле формы по описанию параметра."""
        if not isinstance(spec, dict):
            raise ParameterError(f"Описание параметра «{name}» должно быть объектом")

        kind = spec.get("type", "text")
        if kind not in self.FIELD_TYPES:
            raise ParameterError(f"Недопустимый тип параметра «{name}»: {kind}")

        label = spec.get("label", name)
        help_text = spec.get("help", "")

        if kind == "date":
            return forms.DateField(
                label=label,
                help_text=help_text,
                required=False,
                widget=forms.DateInput(attrs={"type": "date"}),
                initial=self._initial_date(spec.get("initial")),
            )

        if kind == "choice":
            source = spec.get("source")
            choices = _choices_from(source) if source else list(spec.get("choices", []))
            return forms.ChoiceField(
                label=label,
                help_text=help_text,
                required=False,
                choices=[("", _("— все —")), *choices],
            )

        if kind == "boolean":
            return forms.BooleanField(label=label, help_text=help_text, required=False)

        if kind == "integer":
            return forms.IntegerField(
                label=label,
                help_text=help_text,
                required=False,
                min_value=spec.get("min"),
                max_value=spec.get("max"),
            )

        return forms.CharField(label=label, help_text=help_text, required=False, max_length=200)

    @staticmethod
    def _initial_date(value: Any) -> dt.date | None:
        """Разбирает начальное значение даты из описания параметра."""
        if not value:
            return None
        today = timezone.localdate()
        if value == "today":
            return today
        if value == "year_start":
            return dt.date(today.year, 1, 1)
        try:
            return dt.date.fromisoformat(str(value))
        except ValueError:
            return None

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        start = cleaned.get("date_from")
        end = cleaned.get("date_to")
        if start and end and end < start:
            self.add_error("date_to", _("Дата окончания периода раньше даты начала"))
        return cleaned

    @property
    def parameters(self) -> dict[str, Any]:
        """Заполненные параметры отбора без пустых значений."""
        if not self.is_valid():
            return {}
        return {key: value for key, value in self.cleaned_data.items() if value not in (None, "")}


class ReportFilterForm(FilterForm):
    """Отбор в списке сформированных выгрузок."""

    status = forms.ChoiceField(
        label=_("Состояние"), choices=[("", _("Любое")), *ReportStatus.choices], required=False
    )
    category = forms.ChoiceField(
        label=_("Раздел"), choices=[("", _("Любой")), *ReportCategory.choices], required=False
    )
    output_format = forms.ChoiceField(
        label=_("Формат"), choices=[("", _("Любой")), *ReportFormat.choices], required=False
    )

    def filter(self, queryset):
        status = self.cleaned_data.get("status")
        if status:
            queryset = queryset.filter(status=status)

        category = self.cleaned_data.get("category")
        if category:
            queryset = queryset.filter(template__category=category)

        output_format = self.cleaned_data.get("output_format")
        if output_format:
            queryset = queryset.filter(template__output_format=output_format)

        return queryset
