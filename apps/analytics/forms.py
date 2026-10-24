"""Формы аналитического ядра: веса, парные сравнения, размещение в матрице."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django import forms
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.analytics.models import (
    AggregationMethod,
    CalibrationSession,
    CalibrationStatus,
    NineBoxPlacement,
    ScoringCriterion,
    ScoringScheme,
    WeightingMethod,
)
from apps.org.models import Branch, Department
from apps.people.models import Employee
from core.constants import NineBoxCell
from core.forms import BaseForm, BaseModelForm, FilterForm, model_choice

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

#: Шкала Саати с расшифровкой каждого значения.
SAATY_CHOICES: list[tuple[str, StrOrPromise]] = [
    ("0.1111", _("В 9 раз важнее второй")),
    ("0.1429", _("В 7 раз важнее второй")),
    ("0.2", _("В 5 раз важнее второй")),
    ("0.3333", _("В 3 раза важнее второй")),
    ("0.5", _("Немного важнее второй")),
    ("1", _("Равнозначны")),
    ("2", _("Немного важнее первый")),
    ("3", _("В 3 раза важнее первый")),
    ("5", _("В 5 раз важнее первый")),
    ("7", _("В 7 раз важнее первый")),
    ("9", _("В 9 раз важнее первый")),
]

#: Минимальная длина обоснования корректировки на калибровке.
MIN_CALIBRATION_COMMENT = 30


# ---------------------------------------------------------------------------
# Схемы оценивания
# ---------------------------------------------------------------------------
class SchemeFilterForm(FilterForm):
    """Отбор схем оценивания."""

    method = forms.ChoiceField(
        label=_("Способ получения весов"),
        choices=[("", _("Любой")), *WeightingMethod.choices],
    )
    aggregation = forms.ChoiceField(
        label=_("Способ свёртки"),
        choices=[("", _("Любой")), *AggregationMethod.choices],
    )
    state = forms.ChoiceField(
        label=_("Состояние"),
        choices=[
            ("", _("Все")),
            ("usable", _("Пригодны к расчёту")),
            ("inconsistent", _("Суждения противоречивы")),
            ("inactive", _("Выведены из обращения")),
        ],
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        """Применяет условия отбора к схемам оценивания."""
        data = self.cleaned_data or {}

        if method := data.get("method"):
            queryset = queryset.filter(method=method)
        if aggregation := data.get("aggregation"):
            queryset = queryset.filter(aggregation=aggregation)

        state = data.get("state")
        if state == "usable":
            # Пригодность складывается из двух условий модели: схема действует и её суждения не
            # признаны противоречивыми.
            queryset = queryset.filter(is_active=True).exclude(
                method=WeightingMethod.AHP, is_consistent=False
            )
        elif state == "inconsistent":
            queryset = queryset.filter(is_consistent=False)
        elif state == "inactive":
            queryset = queryset.filter(is_active=False)
        return queryset


class SchemeForm(BaseModelForm):
    """Создание схемы оценивания."""

    class Meta:
        model = ScoringScheme
        fields = ["name", "code", "description", "method", "aggregation", "source_note"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "source_note": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "source_note": _("Кто и на каком основании назначает веса — материал для отчёта"),
        }


class ExpertWeightForm(BaseForm):
    """Экспертное назначение весов критериев."""

    def __init__(self, *args: Any, criteria: list[ScoringCriterion] | None = None, **kwargs: Any):
        self.criteria = criteria or list(ScoringCriterion.objects.filter(is_active=True))
        super().__init__(*args, **kwargs)

        for criterion in self.criteria:
            self.fields[f"weight_{criterion.pk}"] = forms.DecimalField(
                label=criterion.name,
                min_value=Decimal("0"),
                max_value=Decimal("100"),
                decimal_places=2,
                required=True,
                help_text=criterion.description,
            )
        self._apply_styling()

    def clean(self) -> dict[str, Any]:
        """Проверяет, что хотя бы один критерий получил ненулевую важность."""
        data = super().clean() or {}
        total = sum(
            (data.get(f"weight_{criterion.pk}") or Decimal("0") for criterion in self.criteria),
            Decimal("0"),
        )
        if total <= 0:
            raise forms.ValidationError(
                _("Хотя бы один критерий должен получить ненулевую важность")
            )
        return data

    def rows(self) -> list[tuple[ScoringCriterion, forms.BoundField]]:
        """Критерии вместе с их полями — для отрисовки формы."""
        return [(criterion, self[f"weight_{criterion.pk}"]) for criterion in self.criteria]

    def weights(self) -> dict[int, Decimal]:
        """Введённые значения важности, ключ — идентификатор критерия."""
        return {
            criterion.pk: self.cleaned_data.get(f"weight_{criterion.pk}") or Decimal("0")
            for criterion in self.criteria
        }


class AHPJudgementForm(BaseForm):
    """Парные сравнения критериев по шкале Саати."""

    def __init__(self, *args: Any, criteria: list[ScoringCriterion] | None = None, **kwargs: Any):
        self.criteria = criteria or list(
            ScoringCriterion.objects.filter(is_active=True).order_by("sort_order")
        )
        super().__init__(*args, **kwargs)

        for first in range(len(self.criteria)):
            for second in range(first + 1, len(self.criteria)):
                self.fields[f"pair_{first}_{second}"] = forms.ChoiceField(
                    label=_("%(first)s — %(second)s")
                    % {
                        "first": self.criteria[first].name,
                        "second": self.criteria[second].name,
                    },
                    choices=SAATY_CHOICES,
                    initial="1",
                    required=True,
                )
        self.fields["note"] = forms.CharField(
            label=_("Происхождение суждений"),
            widget=forms.Textarea(attrs={"rows": 2}),
            required=False,
            help_text=_("Кто выносил суждения и на каком основании"),
        )
        self._apply_styling()

    def pairs(self) -> list[tuple[ScoringCriterion, ScoringCriterion, forms.BoundField]]:
        """Пары критериев вместе с полем суждения — для отрисовки таблицы."""
        result = []
        for first in range(len(self.criteria)):
            for second in range(first + 1, len(self.criteria)):
                result.append(
                    (
                        self.criteria[first],
                        self.criteria[second],
                        self[f"pair_{first}_{second}"],
                    )
                )
        return result

    def judgements(self) -> dict[tuple[int, int], float]:
        """Суждения верхнего треугольника матрицы парных сравнений."""
        result: dict[tuple[int, int], float] = {}
        for first in range(len(self.criteria)):
            for second in range(first + 1, len(self.criteria)):
                value = self.cleaned_data.get(f"pair_{first}_{second}")
                if value:
                    result[(first, second)] = float(value)
        return result


class EntropyForm(BaseForm):
    """Построение энтропийной схемы по данным резерва."""

    scope = forms.ChoiceField(
        label=_("Выборка"),
        choices=[
            ("all", _("Весь действующий резерв")),
            ("ready", _("Только готовые к назначению")),
        ],
        initial="all",
    )
    branch = forms.ModelChoiceField(
        label=_("Филиал"),
        queryset=Branch.objects.all(),
        required=False,
        empty_label=_("Все филиалы"),
    )
    note = forms.CharField(
        label=_("Примечание к схеме"),
        widget=forms.Textarea(attrs={"rows": 2}),
        required=False,
    )


# ---------------------------------------------------------------------------
# Матрица 9-box
# ---------------------------------------------------------------------------
class NineBoxFilterForm(FilterForm):
    """Отбор для матрицы «результативность — потенциал»."""

    q = forms.CharField(
        label=_("Поиск"),
        widget=forms.TextInput(attrs={"type": "search", "placeholder": _("Фамилия")}),
    )
    branch = forms.ModelChoiceField(
        label=_("Филиал"),
        queryset=Branch.objects.all(),
        empty_label=_("Все филиалы"),
    )
    department = forms.ModelChoiceField(
        label=_("Подразделение"),
        queryset=Department.objects.filter(is_active=True),
        empty_label=_("Все подразделения"),
    )
    cell = forms.ChoiceField(
        label=_("Ячейка"),
        choices=[("", _("Любая")), *NineBoxCell.choices],
    )
    origin = forms.ChoiceField(
        label=_("Происхождение оценки"),
        choices=[
            ("", _("Любое")),
            ("adjusted", _("Скорректирована на калибровке")),
            ("manual", _("Выставлена руководителем")),
            ("auto", _("Предварительная, по данным системы")),
        ],
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        """Применяет условия отбора к размещениям."""
        data = self.cleaned_data or {}

        if term := data.get("q"):
            queryset = queryset.filter(employee__last_name__icontains=term)
        if branch := data.get("branch"):
            queryset = queryset.filter(employee__branch=branch)
        if department := data.get("department"):
            queryset = queryset.filter(
                employee__department_id__in=Department.objects.descendant_ids(department.pk)
            )
        if cell := data.get("cell"):
            queryset = queryset.filter(cell=cell)

        origin = data.get("origin")
        if origin == "adjusted":
            queryset = queryset.filter(is_adjusted=True)
        elif origin == "manual":
            queryset = queryset.filter(placed_by__isnull=False, is_adjusted=False)
        elif origin == "auto":
            queryset = queryset.filter(placed_by__isnull=True, is_adjusted=False)
        return queryset


class PlacementForm(BaseModelForm):
    """Размещение сотрудника в матрице вручную."""

    employee = forms.ModelChoiceField(
        label=_("Сотрудник"),
        queryset=Employee.objects.active().filter(is_external=False),
    )

    class Meta:
        model = NineBoxPlacement
        fields = ["employee", "performance", "potential", "period_start", "period_end", "comment"]
        widgets = {
            "period_start": forms.DateInput(attrs={"type": "date"}),
            "period_end": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "performance": _("Достижение целей за период, 0–100"),
            "potential": _("Способность к росту и освоению более сложных задач, 0–100"),
        }

    def clean(self) -> dict[str, Any]:
        """Проверяет порядок дат оцениваемого периода."""
        data = super().clean() or {}
        start = data.get("period_start")
        end = data.get("period_end")
        if start and end and end < start:
            self.add_error("period_end", _("Окончание периода раньше его начала"))
        return data


class CalibrationForm(BaseForm):
    """Корректировка размещения в ходе сессии калибровки."""

    performance = forms.DecimalField(
        label=_("Результативность"), min_value=0, max_value=100, decimal_places=2
    )
    potential = forms.DecimalField(
        label=_("Потенциал"), min_value=0, max_value=100, decimal_places=2
    )
    comment = forms.CharField(
        label=_("Обоснование изменения"),
        widget=forms.Textarea(attrs={"rows": 3}),
        min_length=MIN_CALIBRATION_COMMENT,
        help_text=_("Что именно в наблюдаемом поведении не совпало с исходной оценкой"),
    )


class CalibrationSessionForm(BaseModelForm):
    """Создание и правка сессии калибровки."""

    class Meta:
        model = CalibrationSession
        fields = [
            "name",
            "description",
            "held_on",
            "status",
            "branch",
            "department",
            "facilitator",
            "campaign",
            "agenda",
        ]
        widgets = {
            "held_on": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 2}),
            "agenda": forms.Textarea(attrs={"rows": 3}),
        }


class CalibrationCloseForm(BaseForm):
    """Завершение сессии калибровки с протоколом."""

    decisions = forms.CharField(
        label=_("Принятые решения"),
        widget=forms.Textarea(attrs={"rows": 4}),
        min_length=MIN_CALIBRATION_COMMENT,
        help_text=_("Кто перемещён между ячейками и на каком основании"),
    )


class CalibrationFilterForm(FilterForm):
    """Отбор сессий калибровки."""

    status = forms.ChoiceField(
        label=_("Состояние"),
        choices=[("", _("Любое")), *CalibrationStatus.choices],
    )
    branch = forms.ModelChoiceField(
        label=_("Филиал"),
        queryset=Branch.objects.all(),
        empty_label=_("Все филиалы"),
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        data = self.cleaned_data or {}
        if status := data.get("status"):
            queryset = queryset.filter(status=status)
        if branch := data.get("branch"):
            queryset = queryset.filter(branch=branch)
        return queryset


# ---------------------------------------------------------------------------
# Рейтинг и анализ
# ---------------------------------------------------------------------------
class RankingFilterForm(FilterForm):
    """Отбор для рейтинга резервистов по индексу готовности."""

    q = forms.CharField(
        label=_("Поиск"),
        widget=forms.TextInput(attrs={"type": "search", "placeholder": _("Фамилия")}),
    )
    branch = forms.ModelChoiceField(
        label=_("Филиал"),
        queryset=Branch.objects.all(),
        empty_label=_("Все филиалы"),
    )
    reliability = forms.ChoiceField(
        label=_("Достоверность"),
        choices=[
            ("", _("Любая")),
            ("reliable", _("Обеспечены данными")),
            ("partial", _("Обеспеченность ниже двух третей")),
        ],
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        """Применяет отбор к снимкам расчёта."""
        data = self.cleaned_data or {}
        if term := data.get("q"):
            queryset = queryset.filter(entry__employee__last_name__icontains=term)
        if branch := data.get("branch"):
            queryset = queryset.filter(entry__employee__branch=branch)
        return queryset


class SchemeComparisonForm(BaseForm):
    """Выбор схем для сравнения ранжирований."""

    schemes = forms.ModelMultipleChoiceField(
        label=_("Схемы оценивания"),
        queryset=ScoringScheme.objects.none(),
        widget=forms.CheckboxSelectMultiple,
    )
    top = forms.IntegerField(
        label=_("Размер головной части списка"),
        min_value=3,
        max_value=50,
        initial=10,
        help_text=_("Сколько первых мест сравнивать по составу"),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        model_choice(self, "schemes").queryset = ScoringScheme.objects.with_weights().usable()
        self._apply_styling()

    def clean_schemes(self) -> QuerySet:
        schemes = self.cleaned_data["schemes"]
        if len(schemes) < 2:
            raise forms.ValidationError(_("Выберите не менее двух схем для сравнения"))
        return schemes


class StabilityForm(BaseForm):
    """Параметры анализа устойчивости ранжирования."""

    scheme = forms.ModelChoiceField(
        label=_("Схема оценивания"),
        queryset=ScoringScheme.objects.none(),
        required=False,
        empty_label=_("Схема по умолчанию"),
    )
    magnitude = forms.ChoiceField(
        label=_("Возмущение весов"),
        choices=[
            ("standard", _("10, 20 и 30 процентов")),
            ("fine", _("5, 10 и 15 процентов")),
            ("coarse", _("20, 30 и 40 процентов")),
        ],
        initial="standard",
    )
    top = forms.IntegerField(
        label=_("Головная часть списка"), min_value=3, max_value=50, initial=10
    )

    #: Наборы возмущений.
    MAGNITUDES: dict[str, tuple[float, ...]] = {
        "standard": (0.10, 0.20, 0.30),
        "fine": (0.05, 0.10, 0.15),
        "coarse": (0.20, 0.30, 0.40),
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        model_choice(self, "scheme").queryset = ScoringScheme.objects.with_weights().usable()
        self._apply_styling()

    def deltas(self) -> tuple[float, ...]:
        """Набор возмущений по выбранному варианту."""
        return self.MAGNITUDES.get(self.cleaned_data.get("magnitude", "standard"), (0.1, 0.2, 0.3))


class RecalculationForm(BaseForm):
    """Запуск пересчёта индексов готовности."""

    scheme = forms.ModelChoiceField(
        label=_("Схема оценивания"),
        queryset=ScoringScheme.objects.none(),
        required=False,
        empty_label=_("Схема по умолчанию — обновит рабочие уровни готовности"),
    )
    notify = forms.BooleanField(
        label=_("Уведомить резервистов о смене уровня готовности"),
        required=False,
        initial=True,
        help_text=_("Действует только для схемы по умолчанию"),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        model_choice(self, "scheme").queryset = ScoringScheme.objects.with_weights().usable()
        self._apply_styling()
