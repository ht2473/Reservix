"""Формы процедур оценки."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from django import forms
from django.core.validators import MaxValueValidator
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.assessment.models import (
    AssessmentCampaign,
    AssessmentMethod,
    AssessmentSession,
    CampaignStatus,
    QuestionKind,
    SessionStatus,
    TestQuestion,
)
from apps.competencies.models import Competency, CompetencyModel, CompetencyRequirement
from apps.org.models import Branch, Department
from core.forms import BaseForm, BaseModelForm, DateRangeMixin, FilterForm
from core.settings_types import get_reservix_settings


# ---------------------------------------------------------------------------
# Кампании
# ---------------------------------------------------------------------------
class CampaignFilterForm(FilterForm):
    """Отбор кампаний оценки."""

    q = forms.CharField(
        label=_("Поиск"),
        widget=forms.TextInput(
            attrs={"type": "search", "placeholder": _("Наименование или код кампании")}
        ),
    )
    status = forms.ChoiceField(
        label=_("Состояние"), choices=[("", _("Любое")), *CampaignStatus.choices]
    )
    year = forms.IntegerField(label=_("Год"), min_value=2000, max_value=2100)

    def filter(self, queryset: QuerySet) -> QuerySet:
        data = self.cleaned_data or {}
        if term := data.get("q"):
            queryset = queryset.filter(Q(name__icontains=term) | Q(code__icontains=term))
        if status := data.get("status"):
            queryset = queryset.filter(status=status)
        if year := data.get("year"):
            # Условие повторяет `AssessmentCampaignQuerySet.for_year`, но применяется к обобщённой
            # выборке: форма фильтров не знает, какой именно набор записей ей передадут, и
            # опираться на метод конкретного набора не вправе.
            queryset = queryset.filter(period_start__year=year)
        return queryset


class CampaignForm(DateRangeMixin, BaseModelForm):
    """Создание и изменение кампании оценки."""

    start_field = "period_start"
    end_field = "period_end"

    class Meta:
        model = AssessmentCampaign
        fields = [
            "code",
            "name",
            "description",
            "competency_model",
            "period_start",
            "period_end",
            "deadline",
            "branch",
            "department",
            "is_self_assessment_required",
            "is_360_enabled",
            "is_anonymous_360",
            "instructions",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "instructions": forms.Textarea(attrs={"rows": 4}),
            "period_start": forms.DateInput(attrs={"type": "date"}),
            "period_end": forms.DateInput(attrs={"type": "date"}),
            "deadline": forms.DateInput(attrs={"type": "date"}),
        }

    # Наборы значений объявлены полями формы, а не присвоены в конструкторе: оформление полей
    # выполняется базовой формой до возврата из конструктора и уже обращается к спискам вариантов.
    competency_model = forms.ModelChoiceField(
        label=_("Модель компетенций"),
        queryset=CompetencyModel.objects.filter(is_active=True, is_published=True),
        help_text=_("Набор требований по умолчанию для участников кампании"),
    )
    branch = forms.ModelChoiceField(
        label=_("Филиал"),
        queryset=Branch.objects.filter(is_active=True),
        required=False,
        empty_label=_("— вся организация —"),
    )
    department = forms.ModelChoiceField(
        label=_("Подразделение"),
        queryset=Department.objects.filter(is_active=True).select_related("branch"),
        required=False,
        empty_label=_("— без ограничения —"),
    )

    def clean(self) -> dict[str, Any]:
        """Проверяет согласованность периода, срока и области охвата."""
        cleaned = super().clean() or {}
        period_start = cleaned.get("period_start")
        period_end = cleaned.get("period_end")
        deadline = cleaned.get("deadline")

        if period_start and deadline and deadline < period_start:
            self.add_error("deadline", _("Срок не может наступать раньше начала периода"))

        if period_end and deadline and deadline > period_end + dt.timedelta(days=90):
            # Срок, оторванный от периода более чем на квартал, почти всегда означает опечатку в
            # годе: кампания за прошлый год со сроком в следующем не бывает.
            self.add_error(
                "deadline",
                _("Срок отстоит от окончания периода более чем на квартал — проверьте дату"),
            )

        branch = cleaned.get("branch")
        department = cleaned.get("department")
        if branch and department and department.branch_id != branch.pk:
            self.add_error(
                "department",
                _("Подразделение относится к другому филиалу"),
            )

        return cleaned


class CampaignActionForm(BaseForm):
    """Подтверждение запуска либо завершения кампании."""

    confirm = forms.BooleanField(label=_("Подтверждаю выполнение действия"), required=True)


# ---------------------------------------------------------------------------
# Заполнение сессии оценки
# ---------------------------------------------------------------------------
class ScoreEntryForm(BaseForm):
    """Форма выставления баллов по требованиям модели компетенций."""

    #: Расхождение с целевым уровнем, начиная с которого нужно обоснование.
    EXPLANATION_THRESHOLD = 1.5

    field_prefix = "level"
    comment_prefix = "comment"

    def __init__(self, *args, requirements: list[CompetencyRequirement], **kwargs) -> None:
        self.requirements = requirements
        super().__init__(*args, **kwargs)

        options = get_reservix_settings()
        choices: list[tuple[str, Any]] = [("", _("— не оценивалось —"))]
        # Шаг в половину уровня: эксперт различает «уверенно» и «скорее уверенно», но не сотые доли
        # балла.
        step = 0.5
        value = float(options["COMPETENCY_SCALE_MIN"])
        while value <= options["COMPETENCY_SCALE_MAX"]:
            label = f"{value:.1f}".replace(".", ",")
            choices.append((f"{value:.1f}", label))
            value += step

        for requirement in requirements:
            competency = requirement.competency
            self.fields[self._level_name(requirement)] = forms.ChoiceField(
                label=competency.name,
                choices=choices,
                required=False,
                widget=forms.Select(attrs={"class": "field-select field-select--score"}),
                help_text=competency.definition,
            )
            self.fields[self._comment_name(requirement)] = forms.CharField(
                label=_("Обоснование"),
                required=False,
                max_length=2000,
                widget=forms.Textarea(
                    attrs={
                        "rows": 2,
                        "placeholder": _("Наблюдаемое поведение, подтверждающее уровень"),
                    }
                ),
            )

        self._apply_styling()

    def _level_name(self, requirement: CompetencyRequirement) -> str:
        return f"{self.field_prefix}_{requirement.competency_id}"

    def _comment_name(self, requirement: CompetencyRequirement) -> str:
        return f"{self.comment_prefix}_{requirement.competency_id}"

    def rows(self) -> list[dict]:
        """Строки формы: требование, поля и поведенческие индикаторы."""
        result = []
        for requirement in self.requirements:
            competency = requirement.competency
            result.append(
                {
                    "requirement": requirement,
                    "competency": competency,
                    "level_field": self[self._level_name(requirement)],
                    "comment_field": self[self._comment_name(requirement)],
                    "scale": list(competency.scale_levels.all()),
                }
            )
        return result

    def clean(self) -> dict[str, Any]:
        """Требует обоснования при существенном расхождении с целевым уровнем."""
        cleaned = super().clean() or {}

        for requirement in self.requirements:
            raw = cleaned.get(self._level_name(requirement))
            if not raw:
                continue

            level = float(raw)
            comment = (cleaned.get(self._comment_name(requirement)) or "").strip()
            deviation = abs(level - requirement.target_level)

            if deviation >= self.EXPLANATION_THRESHOLD and len(comment) < 20:
                self.add_error(
                    self._comment_name(requirement),
                    _(
                        "Оценка существенно расходится с целевым уровнем "
                        "%(target)s — укажите наблюдаемое поведение, "
                        "подтверждающее её"
                    )
                    % {"target": requirement.target_level},
                )

        return cleaned

    @property
    def levels(self) -> dict[int, Decimal | None]:
        """Выставленные уровни в виде «компетенция — уровень»."""
        result: dict[int, Decimal | None] = {}
        for requirement in self.requirements:
            raw = self.cleaned_data.get(self._level_name(requirement))
            result[requirement.competency_id] = Decimal(raw) if raw else None
        return result

    @property
    def comments(self) -> dict[int, str]:
        """Обоснования в виде «компетенция — текст»."""
        return {
            requirement.competency_id: self.cleaned_data.get(self._comment_name(requirement), "")
            for requirement in self.requirements
        }


class SessionSummaryForm(BaseModelForm):
    """Общее заключение по результатам сессии оценки."""

    class Meta:
        model = AssessmentSession
        fields = ["summary"]
        widgets = {
            "summary": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": _(
                        "Итог оценки: сильные стороны, зоны развития, договорённости с сотрудником"
                    ),
                }
            )
        }


class SessionRejectForm(BaseForm):
    """Возврат сессии оценивающему с указанием причины."""

    reason = forms.CharField(
        label=_("Причина возврата"),
        max_length=255,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Текст виден оценивающему и сохраняется в истории сессии"),
    )

    def clean_reason(self) -> str:
        reason = (self.cleaned_data.get("reason") or "").strip()
        if len(reason) < 15:
            raise forms.ValidationError(
                _("Причина возврата должна объяснять, что именно требуется исправить")
            )
        return reason


class SessionFilterForm(FilterForm):
    """Отбор сессий оценки."""

    status = forms.ChoiceField(
        label=_("Состояние"), choices=[("", _("Любое")), *SessionStatus.choices]
    )
    method = forms.ChoiceField(
        label=_("Метод"), choices=[("", _("Любой")), *AssessmentMethod.choices]
    )
    campaign = forms.ModelChoiceField(
        label=_("Кампания"),
        queryset=AssessmentCampaign.objects.all(),
        empty_label=_("Все кампании"),
    )
    q = forms.CharField(
        label=_("Сотрудник"),
        widget=forms.TextInput(attrs={"type": "search", "placeholder": _("Фамилия сотрудника")}),
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        data = self.cleaned_data or {}
        if status := data.get("status"):
            queryset = queryset.filter(status=status)
        if method := data.get("method"):
            queryset = queryset.filter(method=method)
        if campaign := data.get("campaign"):
            queryset = queryset.filter(campaign=campaign)
        if term := data.get("q"):
            queryset = queryset.filter(
                Q(employee__last_name__icontains=term) | Q(employee__first_name__icontains=term)
            )
        return queryset


# ---------------------------------------------------------------------------
# Тестирование
# ---------------------------------------------------------------------------
class AnswerForm(BaseForm):
    """Ответ на одно задание теста."""

    def __init__(self, *args, question: TestQuestion, options: list, **kwargs) -> None:
        self.question = question
        super().__init__(*args, **kwargs)

        choices = [(option.pk, option.text) for option in options]

        if question.kind == QuestionKind.MULTIPLE:
            self.fields["answer"] = forms.MultipleChoiceField(
                label=question.text,
                choices=choices,
                required=False,
                widget=forms.CheckboxSelectMultiple,
            )
        elif question.kind == QuestionKind.OPEN:
            self.fields["answer"] = forms.CharField(
                label=question.text,
                required=False,
                max_length=4000,
                widget=forms.Textarea(attrs={"rows": 5}),
            )
        else:
            self.fields["answer"] = forms.ChoiceField(
                label=question.text,
                choices=choices,
                required=False,
                widget=forms.RadioSelect,
            )

        self._apply_styling()

    @property
    def selected_option_ids(self) -> list[int]:
        """Идентификаторы выбранных вариантов."""
        value = self.cleaned_data.get("answer")
        if not value or self.question.kind == QuestionKind.OPEN:
            return []
        if isinstance(value, list):
            return [int(item) for item in value]
        return [int(value)]

    @property
    def text_answer(self) -> str:
        """Текст развёрнутого ответа."""
        if self.question.kind != QuestionKind.OPEN:
            return ""
        return self.cleaned_data.get("answer") or ""


class TestFilterForm(FilterForm):
    """Отбор тестов."""

    q = forms.CharField(
        label=_("Поиск"),
        widget=forms.TextInput(attrs={"type": "search", "placeholder": _("Наименование теста")}),
    )
    # Набор значений задан в объявлении поля, а не в `__init__`.
    competency = forms.ModelChoiceField(
        label=_("Компетенция"),
        queryset=Competency.objects.filter(is_testable=True),
        empty_label=_("Все компетенции"),
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        data = self.cleaned_data or {}
        if term := data.get("q"):
            queryset = queryset.filter(Q(name__icontains=term) | Q(code__icontains=term))
        if competency := data.get("competency"):
            queryset = queryset.filter(competency=competency)
        return queryset


class AnswerCheckForm(BaseForm):
    """Проверка развёрнутого ответа экспертом."""

    awarded_score = forms.DecimalField(
        label=_("Начислено баллов"),
        min_value=Decimal("0"),
        max_digits=5,
        decimal_places=2,
    )
    is_correct = forms.BooleanField(label=_("Ответ засчитан"), required=False)
    remark = forms.CharField(
        label=_("Замечание"),
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, max_score: int = 1, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.max_score = max_score
        # Верхняя граница задаётся проверяющей функцией, а не атрибутом поля: атрибут `max_value`
        # читается только при построении поля, и его изменение после создания формы на проверку уже
        # не влияет.
        self.fields["awarded_score"].validators.append(MaxValueValidator(Decimal(max_score)))
        self.fields["awarded_score"].help_text = _("Не более %(max)s") % {"max": max_score}

    def clean_awarded_score(self) -> Decimal:
        """Не даёт начислить больше веса задания."""
        score = self.cleaned_data["awarded_score"]
        if score > Decimal(self.max_score):
            raise forms.ValidationError(
                _("Задание весит %(max)s балла — начислить больше нельзя") % {"max": self.max_score}
            )
        return score
