"""Формы планирования преемственности."""

from __future__ import annotations

from typing import Any

from django import forms
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.org.models import Department, Position
from apps.people.models import Employee
from apps.succession.models import (
    Appointment,
    AppointmentKind,
    CriticalityLevel,
    KeyPosition,
    PlanStatus,
    SuccessionPlan,
    VacancyRisk,
)
from core.forms import BaseForm, BaseModelForm, FilterForm, model_choice

#: Минимальная длина описания последствий ухода. Формулировка «должность
#: важна» ничего не добавляет к признаку критичности и отсекается порогом.
MIN_IMPACT_LENGTH = 40


# ---------------------------------------------------------------------------
# Критичные должности
# ---------------------------------------------------------------------------
class KeyPositionFilterForm(FilterForm):
    """Отбор критичных должностей."""

    q = forms.CharField(
        label=_("Поиск"),
        widget=forms.TextInput(attrs={"type": "search", "placeholder": _("Должность")}),
    )
    criticality = forms.ChoiceField(
        label=_("Критичность"),
        choices=[("", _("Любая")), *CriticalityLevel.choices],
    )
    vacancy_risk = forms.ChoiceField(
        label=_("Риск освобождения"),
        choices=[("", _("Любой")), *VacancyRisk.choices],
    )
    department = forms.ModelChoiceField(
        label=_("Подразделение"),
        queryset=Department.objects.filter(is_active=True),
        empty_label=_("Все подразделения"),
    )
    coverage = forms.ChoiceField(
        label=_("Обеспеченность"),
        choices=[
            ("", _("Любая")),
            ("uncovered", _("Без действующего плана")),
            ("no_ready", _("Без готового преемника")),
            ("vacant", _("Должность вакантна")),
        ],
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        """Применяет условия отбора к критичным должностям."""
        data = self.cleaned_data or {}

        if term := data.get("q"):
            queryset = queryset.filter(position__name__icontains=term)
        if criticality := data.get("criticality"):
            queryset = queryset.filter(criticality=criticality)
        if risk := data.get("vacancy_risk"):
            queryset = queryset.filter(vacancy_risk=risk)
        if department := data.get("department"):
            queryset = queryset.filter(
                position__department_id__in=Department.objects.descendant_ids(department.pk)
            )

        coverage = data.get("coverage")
        if coverage == "uncovered":
            # Отбор ведётся по аннотации, а не по свойству модели: свойство выполняет запрос на
            # каждой строке, а фильтровать по нему нельзя вовсе — оно вычисляется после выборки.
            queryset = queryset.filter(successors_total=0)
        elif coverage == "no_ready":
            queryset = queryset.filter(successors_ready=0)
        elif coverage == "vacant":
            queryset = queryset.filter(incumbent__isnull=True)

        return queryset


class KeyPositionForm(BaseModelForm):
    """Отнесение должности к критичным."""

    class Meta:
        model = KeyPosition
        fields = [
            "position",
            "incumbent",
            "criticality",
            "vacancy_risk",
            "impact_of_loss",
            "justification",
            "required_successors",
            "vacancy_horizon_months",
            "is_active",
        ]
        widgets = {
            "impact_of_loss": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": _(
                        "Какие процессы остановятся, какие знания будут утрачены, "
                        "сколько времени займёт восстановление"
                    ),
                }
            ),
            "justification": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        model_choice(self, "position").queryset = Position.objects.filter(
            is_active=True
        ).select_related("grade", "department")
        incumbent_field = model_choice(self, "incumbent")
        incumbent_field.queryset = Employee.objects.active().with_org().order_by("last_name")
        incumbent_field.empty_label = _("Должность вакантна")

    def clean_impact_of_loss(self) -> str:
        text = (self.cleaned_data.get("impact_of_loss") or "").strip()
        if len(text) < MIN_IMPACT_LENGTH:
            raise forms.ValidationError(
                _(
                    "Опишите последствия ухода содержательно: именно они "
                    "отличают критичную должность от руководящей"
                )
            )
        return text


# ---------------------------------------------------------------------------
# Планы преемственности
# ---------------------------------------------------------------------------
class SuccessionPlanFilterForm(FilterForm):
    """Отбор планов преемственности."""

    q = forms.CharField(
        label=_("Поиск"),
        widget=forms.TextInput(attrs={"type": "search", "placeholder": _("Должность или план")}),
    )
    status = forms.ChoiceField(
        label=_("Состояние"), choices=[("", _("Любое")), *PlanStatus.choices]
    )
    scope = forms.ChoiceField(
        label=_("Отбор"),
        choices=[
            ("", _("Все планы")),
            ("due", _("Требуют пересмотра")),
            ("insufficient", _("Преемников меньше требуемого")),
        ],
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        data = self.cleaned_data or {}
        if term := data.get("q"):
            queryset = queryset.filter(
                Q(title__icontains=term) | Q(key_position__position__name__icontains=term)
            )
        if status := data.get("status"):
            queryset = queryset.filter(status=status)

        scope = data.get("scope")
        if scope == "due":
            queryset = queryset.filter(
                status=PlanStatus.ACTIVE, review_date__lte=timezone.localdate()
            )
        elif scope == "insufficient":
            queryset = queryset.filter(successors_total__lt=1)
        return queryset


class SuccessionPlanForm(BaseModelForm):
    """Ведение плана преемственности."""

    class Meta:
        model = SuccessionPlan
        fields = ["title", "owner", "horizon_months", "start_date", "review_date", "risks", "notes"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "review_date": forms.DateInput(attrs={"type": "date"}),
            "risks": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def clean(self) -> dict[str, Any]:
        """Дата пересмотра должна попадать внутрь горизонта планирования."""
        cleaned = super().clean() or {}
        start = cleaned.get("start_date")
        review = cleaned.get("review_date")
        if start and review and review <= start:
            self.add_error("review_date", _("Пересмотр назначается позже начала плана"))
        return cleaned


class SlateForm(BaseForm):
    """Включение преемника в план."""

    employee = forms.ModelChoiceField(
        label=_("Преемник"),
        queryset=Employee.objects.none(),
        empty_label=_("Выберите сотрудника"),
    )
    is_emergency_successor = forms.BooleanField(
        label=_("Экстренная замена"),
        required=False,
        help_text=_("Способен принять должность немедленно и без подготовки"),
    )
    development_focus = forms.CharField(
        label=_("Фокус подготовки"),
        required=False,
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": _("Что предстоит развить до готовности")}
        ),
        max_length=1000,
    )

    def __init__(self, *args, plan: SuccessionPlan, **kwargs) -> None:
        self.plan = plan
        super().__init__(*args, **kwargs)
        taken = plan.slate.filter(removed_on__isnull=True).values_list("employee_id", flat=True)
        model_choice(self, "employee").queryset = (
            Employee.objects.active()
            .with_org()
            .exclude(pk__in=taken)
            .order_by("last_name", "first_name")
        )


class SlateOrderForm(BaseForm):
    """Порядок преемников в плане."""

    order = forms.CharField(widget=forms.HiddenInput)

    def clean_order(self) -> list[int]:
        raw = self.cleaned_data.get("order") or ""
        try:
            return [int(part) for part in raw.split(",") if part.strip()]
        except ValueError as error:
            raise forms.ValidationError(_("Некорректный порядок преемников")) from error


# ---------------------------------------------------------------------------
# Назначения
# ---------------------------------------------------------------------------
class AppointmentForm(BaseModelForm):
    """Регистрация назначения."""

    class Meta:
        model = Appointment
        fields = [
            "employee",
            "position",
            "kind",
            "appointment_date",
            "order_number",
            "probation_until",
            "comment",
        ]
        widgets = {
            "appointment_date": forms.DateInput(attrs={"type": "date"}),
            "probation_until": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        model_choice(self, "employee").queryset = (
            Employee.objects.active().with_org().order_by("last_name", "first_name")
        )
        model_choice(self, "position").queryset = Position.objects.filter(
            is_active=True
        ).select_related("grade")
        self.fields["appointment_date"].initial = timezone.localdate()

    def clean_appointment_date(self):
        """Не принимает назначение задним числом дальше года."""
        value = self.cleaned_data.get("appointment_date")
        if value and value < timezone.localdate().replace(year=timezone.localdate().year - 1):
            raise forms.ValidationError(
                _("Назначение старше года регистрируется через перенос кадровых данных")
            )
        return value

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        employee = cleaned.get("employee")
        position = cleaned.get("position")
        if employee and position and employee.position_id == position.pk:
            self.add_error("position", _("Сотрудник уже занимает эту должность"))
        return cleaned


class AppointmentFilterForm(FilterForm):
    """Отбор назначений."""

    q = forms.CharField(
        label=_("Поиск"),
        widget=forms.TextInput(attrs={"type": "search", "placeholder": _("Сотрудник, должность")}),
    )
    kind = forms.ChoiceField(
        label=_("Источник"), choices=[("", _("Любой")), *AppointmentKind.choices]
    )
    year = forms.IntegerField(
        label=_("Год"), min_value=2000, max_value=2100, widget=forms.NumberInput()
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        data = self.cleaned_data or {}
        if term := data.get("q"):
            queryset = queryset.filter(
                Q(employee__last_name__icontains=term) | Q(position__name__icontains=term)
            )
        if kind := data.get("kind"):
            queryset = queryset.filter(kind=kind)
        if year := data.get("year"):
            queryset = queryset.filter(appointment_date__year=year)
        return queryset


class SlateRemovalForm(BaseForm):
    """Исключение преемника из плана с обязательным основанием."""

    reason = forms.CharField(
        label=_("Основание"),
        max_length=255,
        widget=forms.TextInput(attrs={"placeholder": _("Почему кандидат выбывает из плана")}),
    )
