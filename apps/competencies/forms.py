"""Формы справочника компетенций и моделей требований."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django import forms
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.competencies.models import (
    WEIGHT_SUM_TOLERANCE,
    Competency,
    CompetencyCategory,
    CompetencyKind,
    CompetencyModel,
    CompetencyRequirement,
    CompetencyScaleLevel,
)
from apps.org.models import Department, PositionGrade
from core.forms import BaseModelForm, FilterForm
from core.settings_types import get_reservix_settings


class CompetencyFilterForm(FilterForm):
    """Отбор компетенций справочника."""

    q = forms.CharField(
        label=_("Поиск"),
        widget=forms.TextInput(
            attrs={
                "type": "search",
                "placeholder": _("Наименование, код или определение"),
                "autocomplete": "off",
            }
        ),
    )
    category = forms.ModelChoiceField(
        label=_("Категория"),
        queryset=CompetencyCategory.objects.filter(is_active=True),
        empty_label=_("Все категории"),
    )
    kind = forms.ChoiceField(label=_("Тип"), choices=[("", _("Любой")), *CompetencyKind.choices])
    scope = forms.ChoiceField(
        label=_("Отбор"),
        choices=[
            ("", _("Все компетенции")),
            ("core", _("Сквозные")),
            ("testable", _("Проверяемые тестом")),
            ("incomplete", _("С неполной шкалой")),
        ],
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        """Применяет условия отбора."""
        data = self.cleaned_data or {}

        if term := data.get("q"):
            queryset = queryset.filter(
                Q(name__icontains=term) | Q(code__icontains=term) | Q(definition__icontains=term)
            )
        if category := data.get("category"):
            queryset = queryset.filter(category=category)
        if kind := data.get("kind"):
            queryset = queryset.filter(kind=kind)

        scope = data.get("scope")
        if scope == "core":
            queryset = queryset.filter(is_core=True)
        elif scope == "testable":
            queryset = queryset.filter(is_testable=True)
        elif scope == "incomplete":
            # Отбор выполняется по числу уровней, а не перебором свойства `has_full_scale`:
            # обращение к нему на каждой записи дало бы по запросу на компетенцию.
            queryset = queryset.exclude(scale_count=Competency.expected_scale_size())

        return queryset


class CompetencyForm(BaseModelForm):
    """Создание и изменение компетенции справочника."""

    class Meta:
        model = Competency
        fields = [
            "code",
            "name",
            "category",
            "kind",
            "definition",
            "development_hint",
            "is_core",
            "is_testable",
            "sort_order",
            "is_active",
        ]
        widgets = {
            "definition": forms.Textarea(attrs={"rows": 3}),
            "development_hint": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_definition(self) -> str:
        """Требует содержательного определения."""
        definition = (self.cleaned_data.get("definition") or "").strip()
        if len(definition) < 40:
            raise forms.ValidationError(
                _(
                    "Определение должно описывать наблюдаемое поведение "
                    "и содержать не менее 40 символов"
                )
            )
        return definition


class CompetencyScaleLevelForm(BaseModelForm):
    """Уровень шкалы с поведенческими индикаторами."""

    class Meta:
        model = CompetencyScaleLevel
        fields = ["level", "name", "indicators"]
        widgets = {"indicators": forms.Textarea(attrs={"rows": 2})}


class BaseScaleLevelFormSet(forms.BaseInlineFormSet):
    """Набор форм уровней шкалы с проверкой её полноты."""

    def clean(self) -> None:
        super().clean()
        if any(self.errors):
            return

        options = get_reservix_settings()
        expected = set(range(options["COMPETENCY_SCALE_MIN"], options["COMPETENCY_SCALE_MAX"] + 1))
        present = {
            form.cleaned_data.get("level")
            for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE")
        }
        present.discard(None)

        if present and present != expected:
            missing = sorted(expected - present)
            if missing:
                raise forms.ValidationError(
                    _("Шкала неполна: не описаны уровни %(levels)s")
                    % {"levels": ", ".join(str(level) for level in missing)}
                )


ScaleLevelFormSet = forms.inlineformset_factory(
    Competency,
    CompetencyScaleLevel,
    form=CompetencyScaleLevelForm,
    formset=BaseScaleLevelFormSet,
    extra=0,
    can_delete=True,
)


class CompetencyModelForm(BaseModelForm):
    """Создание и изменение модели компетенций."""

    class Meta:
        model = CompetencyModel
        fields = [
            "code",
            "name",
            "description",
            "version",
            "grade",
            "positions",
            "valid_from",
            "is_published",
            "is_active",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "valid_from": forms.DateInput(attrs={"type": "date"}),
            "positions": forms.SelectMultiple(attrs={"size": 12}),
        }

    # Набор значений задан в объявлении поля, а не присвоением в конструкторе: оформление полей
    # выполняется базовой формой до возврата из конструктора и уже обращается к списку вариантов.
    grade = forms.ModelChoiceField(
        label=_("Типовой грейд"),
        queryset=PositionGrade.objects.filter(is_active=True),
        required=False,
        empty_label=_("— не задан —"),
    )

    def clean(self) -> dict[str, Any]:
        """Не даёт опубликовать модель с несбалансированными весами."""
        cleaned = super().clean() or {}

        if cleaned.get("is_published") and self.instance.pk:
            if not self.instance.is_weight_balanced:
                self.add_error(
                    "is_published",
                    _(
                        "Сумма весов требований равна %(sum)s вместо 1,000. "
                        "Опубликовать модель с такими весами нельзя."
                    )
                    % {"sum": self.instance.weight_sum},
                )
            if not self.instance.requirements.exists():
                self.add_error("is_published", _("В модели нет ни одного требования"))

        return cleaned


class CompetencyRequirementForm(BaseModelForm):
    """Требование модели: компетенция, целевой уровень, вес."""

    class Meta:
        model = CompetencyRequirement
        fields = [
            "competency",
            "target_level",
            "minimum_level",
            "weight",
            "is_critical",
            "comment",
        ]

    competency = forms.ModelChoiceField(
        label=_("Компетенция"),
        queryset=Competency.objects.filter(is_active=True).select_related("category"),
    )

    def clean(self) -> dict[str, Any]:
        """Проверяет согласованность минимального и целевого уровня."""
        cleaned = super().clean() or {}
        target = cleaned.get("target_level")
        minimum = cleaned.get("minimum_level")

        if target is not None and minimum is not None and minimum > target:
            self.add_error(
                "minimum_level",
                _("Минимально допустимый уровень не может превышать целевой"),
            )
        return cleaned


class BaseRequirementFormSet(forms.BaseInlineFormSet):
    """Набор требований с проверкой суммы весов."""

    def clean(self) -> None:
        super().clean()
        if any(self.errors):
            return

        total = sum(
            (
                form.cleaned_data.get("weight") or Decimal("0")
                for form in self.forms
                if form.cleaned_data and not form.cleaned_data.get("DELETE")
            ),
            Decimal("0"),
        )

        if total > 0 and abs(total - Decimal("1")) > WEIGHT_SUM_TOLERANCE:
            raise forms.ValidationError(
                _(
                    "Сумма весов равна %(total)s. Она должна быть равна 1,000 — "
                    "иначе балл соответствия нельзя трактовать как долю."
                )
                % {"total": total}
            )


RequirementFormSet = forms.inlineformset_factory(
    CompetencyModel,
    CompetencyRequirement,
    form=CompetencyRequirementForm,
    formset=BaseRequirementFormSet,
    extra=3,
    can_delete=True,
)


class GapMatrixFilterForm(FilterForm):
    """Отбор для матрицы разрывов подразделения."""

    department = forms.ModelChoiceField(
        label=_("Подразделение"),
        queryset=Department.objects.filter(is_active=True).select_related("branch"),
        empty_label=_("Все доступные"),
    )
    competency_model = forms.ModelChoiceField(
        label=_("Модель требований"),
        queryset=CompetencyModel.objects.filter(is_active=True, is_published=True),
        empty_label=None,
        required=True,
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Поле модели обязательно по смыслу, но форма фильтров делает все поля необязательными: без
        # модели страница показывает подсказку, а не ошибку — пустой экран с красной надписью
        # выглядит как сбой.
        self.fields["competency_model"].required = False
