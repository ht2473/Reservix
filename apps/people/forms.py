"""Формы раздела сотрудников."""

from __future__ import annotations

from typing import cast

from django import forms
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.org.models import Branch, Department, PositionGrade
from apps.people.models import (
    AttritionRisk,
    EducationLevel,
    Employee,
    EmployeeQuerySet,
    EmployeeStatus,
)
from core.forms import FilterForm, model_choice
from core.permissions import is_privileged


class EmployeeFilterForm(FilterForm):
    """Отбор сотрудников."""

    q = forms.CharField(
        label=_("Поиск"),
        widget=forms.TextInput(
            attrs={
                "type": "search",
                "placeholder": _("Фамилия, табельный номер или почта"),
                "autocomplete": "off",
            }
        ),
    )
    branch = forms.ModelChoiceField(
        label=_("Филиал"),
        queryset=Branch.objects.filter(is_active=True),
        empty_label=_("Все филиалы"),
    )
    department = forms.ModelChoiceField(
        label=_("Подразделение"),
        queryset=Department.objects.filter(is_active=True),
        empty_label=_("Все подразделения"),
    )
    grade = forms.ModelChoiceField(
        label=_("Грейд"),
        queryset=PositionGrade.objects.all(),
        empty_label=_("Любой грейд"),
    )
    status = forms.ChoiceField(
        label=_("Статус"),
        choices=[("", _("Любой")), *EmployeeStatus.choices],
    )
    education_level = forms.ChoiceField(
        label=_("Образование"),
        choices=[("", _("Любое")), *EducationLevel.choices],
    )
    scope = forms.ChoiceField(
        label=_("Отбор"),
        choices=[
            ("", _("Все сотрудники")),
            ("employed", _("Работают")),
            ("reserve", _("Состоят в резерве")),
            ("no_reserve", _("Не состоят в резерве")),
            ("key_talent", _("Ключевые сотрудники")),
            ("at_risk", _("Высокий риск ухода")),
            ("external", _("Внешние кандидаты")),
        ],
    )

    def __init__(self, *args, user=None, **kwargs) -> None:
        super().__init__(*args, user=user, **kwargs)

        # Отбор по филиалу осмыслен только тому, кто видит больше одного.
        if not is_privileged(user):
            self.fields.pop("branch", None)
            if user is not None and getattr(user, "department_id", None):
                model_choice(self, "department").queryset = Department.objects.filter(
                    pk__in=Department.objects.descendant_ids(user.department_id)
                )

    def filter(self, queryset: QuerySet) -> QuerySet:
        """Применяет условия отбора к списку сотрудников."""
        employees = cast(EmployeeQuerySet, queryset)
        data = self.cleaned_data or {}

        if term := data.get("q"):
            employees = employees.search(term)
        if branch := data.get("branch"):
            employees = employees.filter(branch=branch)
        if department := data.get("department"):
            # Подразделения вложены: отбор по департаменту обязан захватывать его отделы, иначе
            # фильтр по верхнему уровню даёт пустой список при непустом штате.
            employees = employees.filter(
                department_id__in=Department.objects.descendant_ids(department.pk)
            )
        if grade := data.get("grade"):
            employees = employees.filter(position__grade=grade)
        if status := data.get("status"):
            employees = employees.filter(status=status)
        if education := data.get("education_level"):
            employees = employees.filter(education_level=education)

        match data.get("scope"):
            case "employed":
                employees = employees.active()
            case "reserve":
                employees = employees.filter(reserve_entries__isnull=False).distinct()
            case "no_reserve":
                employees = employees.active().filter(reserve_entries__isnull=True)
            case "key_talent":
                employees = employees.key_talent()
            case "at_risk":
                employees = employees.at_risk()
            case "external":
                employees = employees.external()

        return employees


class EmployeeCardForm(forms.ModelForm):
    """Правка кадровых сведений, участвующих в расчёте готовности."""

    class Meta:
        model = Employee
        fields = [
            "mobility",
            "attrition_risk",
            "is_key_talent",
            "is_excluded_from_reserve",
            "exclusion_note",
            "notes",
        ]
        widgets = {
            "exclusion_note": forms.TextInput(attrs={"placeholder": _("Основание исключения")}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def clean(self) -> dict:
        super().clean()
        data = self.cleaned_data

        # Исключение из резерва без основания невозможно восстановить: через год никто не вспомнит,
        # почему человек не рассматривается, и запись либо висит вечно, либо снимается наугад.
        if data.get("is_excluded_from_reserve") and not (data.get("exclusion_note") or "").strip():
            self.add_error("exclusion_note", _("Укажите основание исключения из резерва"))

        return data

    @property
    def raises_succession_concern(self) -> bool:
        """Признак сочетания «ключевой сотрудник — высокий риск ухода»."""
        data = self.cleaned_data or {}
        return bool(data.get("is_key_talent")) and data.get("attrition_risk") == AttritionRisk.HIGH
