"""Формы раздела развития."""

from __future__ import annotations

import datetime as dt
from typing import Any

from django import forms
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.development.models import (
    ActivityEvidence,
    ActivityStatus,
    ActivityType,
    DevelopmentActivity,
    DevelopmentPlan,
    EvidenceKind,
    LearningResource,
    Mentorship,
    PlanStatus,
    ResourceFormat,
)
from apps.development.services import ACTIVITY_WEIGHTS
from core.constants import Role
from core.forms import BaseForm, BaseModelForm, DateRangeMixin, FilterForm, model_choice


def _employees_for(user) -> QuerySet:
    """Сотрудники, доступные пользователю для выбора в форме."""
    from apps.people.models import Employee
    from core.permissions import apply_employee_scope

    queryset = Employee.objects.active().select_related("position", "department")
    if user is None:
        return queryset.order_by("last_name")
    return apply_employee_scope(queryset, user).order_by("last_name")


class DevelopmentPlanForm(DateRangeMixin, BaseModelForm):
    """Индивидуальный план развития."""

    class Meta:
        model = DevelopmentPlan
        fields = [
            "employee",
            "entry",
            "title",
            "goal",
            "start_date",
            "end_date",
            "supervisor",
        ]
        widgets = {
            "goal": forms.Textarea(attrs={"rows": 3}),
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }
        help_texts = {
            "title": _("Например: «Подготовка к должности руководителя отдела»"),
            "supervisor": _("Руководитель, утверждающий план и принимающий итог"),
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        from apps.accounts.models import User
        from apps.reserve.models import ReserveEntry

        model_choice(self, "employee").queryset = _employees_for(self.user)
        model_choice(self, "supervisor").queryset = User.objects.filter(
            is_active=True, role__in=[Role.MANAGER, Role.HR, Role.ADMIN]
        ).order_by("last_name")

        entries = ReserveEntry.objects.in_reserve().with_related()
        if self.instance.pk and self.instance.employee_id:
            entries = entries.for_employee(self.instance.employee_id)
        model_choice(self, "entry").queryset = entries
        self.fields["entry"].required = False
        self.fields["entry"].help_text = _(
            "Запись резерва, под целевую должность которой ведётся развитие"
        )

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        employee = cleaned.get("employee")
        entry = cleaned.get("entry")
        if employee and entry and entry.employee_id != employee.pk:
            self.add_error("entry", _("Запись резерва принадлежит другому сотруднику"))
        return cleaned


class DevelopmentActivityForm(BaseModelForm):
    """Мероприятие плана развития."""

    class Meta:
        model = DevelopmentActivity
        fields = [
            "title",
            "activity_type",
            "description",
            "competency",
            "resource",
            "start_date",
            "due_date",
            "responsible",
            "mentor",
            "weight",
            "planned_hours",
            "cost",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, plan: DevelopmentPlan | None = None, **kwargs) -> None:
        self.plan = plan
        super().__init__(*args, **kwargs)

        from apps.accounts.models import User
        from apps.competencies.models import Competency

        model_choice(self, "competency").queryset = Competency.objects.filter(
            is_active=True
        ).order_by("name")
        model_choice(self, "resource").queryset = LearningResource.objects.filter(
            is_active=True
        ).order_by("name")
        model_choice(self, "mentor").queryset = User.objects.filter(
            is_active=True, role__in=[Role.MENTOR, Role.MANAGER, Role.HR]
        ).order_by("last_name")

        # Ответственный и наставник — учётные записи, а не кадровые карточки:
        # мероприятие выполняет тот, кто входит в систему и отчитывается о нём.
        model_choice(self, "responsible").queryset = User.objects.filter(is_active=True).order_by(
            "last_name"
        )

        if plan is not None:
            self.fields["responsible"].initial = plan.employee.user_id
            self.fields["start_date"].initial = max(plan.start_date, timezone.localdate())
            self.fields["due_date"].initial = plan.end_date

        self.fields["weight"].help_text = _(
            "Вклад в выполнение плана. Предзаполняется по виду мероприятия: "
            "стажировка и ротация весомее вебинара"
        )

    def clean_due_date(self) -> dt.date:
        due = self.cleaned_data["due_date"]
        if self.plan is not None and due > self.plan.end_date:
            raise forms.ValidationError(
                _("Срок мероприятия выходит за пределы плана: план заканчивается %(date)s")
                % {"date": self.plan.end_date.strftime("%d.%m.%Y")}
            )
        return due

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        start = cleaned.get("start_date")
        due = cleaned.get("due_date")
        if start and due and due < start:
            self.add_error("due_date", _("Срок выполнения раньше даты начала"))

        # Вес по умолчанию выводится из вида мероприятия, если пользователь
        # оставил значение поля нетронутым.
        activity_type = cleaned.get("activity_type")
        if activity_type and not cleaned.get("weight"):
            cleaned["weight"] = ACTIVITY_WEIGHTS.get(activity_type, 1)
        return cleaned


class ActivityCompletionForm(BaseForm):
    """Отметка о выполнении мероприятия."""

    completion_date = forms.DateField(
        label=_("Дата выполнения"),
        widget=forms.DateInput(attrs={"type": "date"}),
        initial=timezone.localdate,
    )
    result = forms.CharField(
        label=_("Достигнутый результат"),
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": _("Что изменилось в работе")}),
        max_length=2000,
        help_text=_("Формулировка результата, а не факта участия"),
    )

    def clean_completion_date(self) -> dt.date:
        value = self.cleaned_data["completion_date"]
        if value > timezone.localdate():
            raise forms.ValidationError(_("Дата выполнения не может быть в будущем"))
        return value

    def clean_result(self) -> str:
        result = (self.cleaned_data.get("result") or "").strip()
        if len(result) < 10:
            raise forms.ValidationError(_("Опишите результат подробнее — не менее 10 символов"))
        return result


class EvidenceForm(BaseModelForm):
    """Подтверждение выполнения мероприятия."""

    class Meta:
        model = ActivityEvidence
        fields = ["kind", "title", "file", "external_url", "comment"]
        widgets = {"comment": forms.Textarea(attrs={"rows": 2})}
        help_texts = {
            "external_url": _("Ссылка на внешний сертификат или запись в системе обучения"),
        }

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if not cleaned.get("file") and not (cleaned.get("external_url") or "").strip():
            raise forms.ValidationError(
                _("Приложите файл или укажите ссылку на подтверждение"), code="empty_evidence"
            )
        return cleaned


class PlanCompletionForm(BaseForm):
    """Подведение итога плана развития."""

    summary = forms.CharField(
        label=_("Итог выполнения плана"),
        widget=forms.Textarea(attrs={"rows": 4}),
        max_length=4000,
        help_text=_(
            "Какие разрывы закрыты, какие сохранились и что рекомендуется в следующем цикле"
        ),
    )

    def clean_summary(self) -> str:
        summary = (self.cleaned_data.get("summary") or "").strip()
        if len(summary) < 20:
            raise forms.ValidationError(_("Итог должен содержать не менее 20 символов"))
        return summary


class MentorshipForm(BaseModelForm):
    """Наставническая пара."""

    class Meta:
        model = Mentorship
        fields = ["mentor", "mentee", "plan", "goal", "end_date", "meeting_frequency_days"]
        widgets = {
            "goal": forms.Textarea(attrs={"rows": 3}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }
        help_texts = {
            "goal": _("Какого результата должна достичь пара к окончанию срока"),
            "meeting_frequency_days": _("Периодичность встреч в днях: основа напоминаний"),
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        from apps.accounts.models import User

        model_choice(self, "mentor").queryset = User.objects.filter(
            is_active=True, role__in=[Role.MENTOR, Role.MANAGER, Role.HR]
        ).order_by("last_name")
        model_choice(self, "mentee").queryset = _employees_for(self.user)
        model_choice(self, "plan").queryset = DevelopmentPlan.objects.open().with_related()
        self.fields["plan"].required = False

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        mentor = cleaned.get("mentor")
        mentee = cleaned.get("mentee")
        if mentor and mentee and mentee.user_id == mentor.pk:
            self.add_error("mentee", _("Наставник и подопечный не могут быть одним человеком"))

        plan = cleaned.get("plan")
        if plan and mentee and plan.employee_id != mentee.pk:
            self.add_error("plan", _("План развития принадлежит другому сотруднику"))
        return cleaned


class MeetingForm(BaseForm):
    """Отметка о проведённой встрече наставника с подопечным."""

    meeting_date = forms.DateField(
        label=_("Дата встречи"),
        widget=forms.DateInput(attrs={"type": "date"}),
        initial=timezone.localdate,
    )
    note = forms.CharField(
        label=_("Краткая запись"),
        required=False,
        widget=forms.Textarea(
            attrs={"rows": 2, "placeholder": _("Что обсуждали, о чём условились")}
        ),
        max_length=1000,
    )

    def clean_meeting_date(self) -> dt.date:
        value = self.cleaned_data["meeting_date"]
        if value > timezone.localdate():
            raise forms.ValidationError(_("Дата встречи не может быть в будущем"))
        return value


class MentorshipCompletionForm(BaseForm):
    """Завершение наставничества."""

    result = forms.CharField(
        label=_("Результат наставничества"),
        widget=forms.Textarea(attrs={"rows": 4}),
        max_length=4000,
    )
    effectiveness = forms.TypedChoiceField(
        label=_("Оценка эффективности"),
        choices=[("", _("— не оценивать —")), *[(str(n), str(n)) for n in range(1, 6)]],
        required=False,
        coerce=int,
        empty_value=None,
        help_text=_("Оценка кадровой службы по пятибалльной шкале"),
    )

    def clean_result(self) -> str:
        result = (self.cleaned_data.get("result") or "").strip()
        if len(result) < 20:
            raise forms.ValidationError(_("Опишите результат подробнее — не менее 20 символов"))
        return result


# ---------------------------------------------------------------------------
# Фильтры
# ---------------------------------------------------------------------------
class PlanFilterForm(FilterForm):
    """Отбор планов развития."""

    q = forms.CharField(
        label=_("Поиск"),
        required=False,
        widget=forms.TextInput(
            attrs={"type": "search", "placeholder": _("Фамилия сотрудника или наименование плана")}
        ),
    )
    status = forms.ChoiceField(
        label=_("Состояние"), choices=[("", _("Любое")), *PlanStatus.choices], required=False
    )
    progress = forms.ChoiceField(
        label=_("Выполнение"),
        choices=[
            ("", _("Любое")),
            ("overdue", _("Есть просроченные мероприятия")),
            ("behind", _("Выполнение ниже половины")),
            ("done", _("Выполнение выше 80 %")),
        ],
        required=False,
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        query = self.cleaned_data.get("q")
        if query:
            queryset = queryset.filter(
                Q(title__icontains=query)
                | Q(employee__last_name__icontains=query)
                | Q(goal__icontains=query)
            )

        status = self.cleaned_data.get("status")
        if status:
            queryset = queryset.filter(status=status)

        progress = self.cleaned_data.get("progress")
        if progress == "overdue":
            queryset = queryset.filter(
                activities__due_date__lt=timezone.localdate(),
                activities__status__in=[ActivityStatus.PENDING, ActivityStatus.IN_PROGRESS],
            ).distinct()
        elif progress == "behind":
            queryset = queryset.filter(completion_rate__lt=50)
        elif progress == "done":
            queryset = queryset.filter(completion_rate__gte=80)

        return queryset


class ActivityFilterForm(FilterForm):
    """Отбор мероприятий развития."""

    status = forms.ChoiceField(
        label=_("Состояние"), choices=[("", _("Любое")), *ActivityStatus.choices], required=False
    )
    activity_type = forms.ChoiceField(
        label=_("Вид"), choices=[("", _("Любой")), *ActivityType.choices], required=False
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        for name in ("status", "activity_type"):
            value = self.cleaned_data.get(name)
            if value:
                queryset = queryset.filter(**{name: value})
        return queryset


class ResourceFilterForm(FilterForm):
    """Отбор учебных ресурсов библиотеки развития."""

    q = forms.CharField(
        label=_("Поиск"),
        required=False,
        widget=forms.TextInput(
            attrs={"type": "search", "placeholder": _("Наименование, поставщик, описание")}
        ),
    )
    resource_format = forms.ChoiceField(
        label=_("Формат"), choices=[("", _("Любой")), *ResourceFormat.choices], required=False
    )
    is_internal = forms.NullBooleanField(label=_("Внутренний ресурс"), required=False)

    def filter(self, queryset: QuerySet) -> QuerySet:
        query = self.cleaned_data.get("q")
        if query:
            queryset = queryset.filter(
                Q(name__icontains=query)
                | Q(description__icontains=query)
                | Q(provider__icontains=query)
            )

        resource_format = self.cleaned_data.get("resource_format")
        if resource_format:
            queryset = queryset.filter(resource_format=resource_format)

        is_internal = self.cleaned_data.get("is_internal")
        if is_internal is not None:
            queryset = queryset.filter(is_internal=is_internal)

        return queryset


#: Виды подтверждений, предлагаемые по умолчанию для каждого вида мероприятия.
DEFAULT_EVIDENCE_KIND: dict[str, str] = {
    ActivityType.TRAINING: EvidenceKind.CERTIFICATE,
    ActivityType.COURSE: EvidenceKind.CERTIFICATE,
    ActivityType.CONFERENCE: EvidenceKind.CERTIFICATE,
    ActivityType.MENTORING: EvidenceKind.FEEDBACK,
    ActivityType.PROJECT: EvidenceKind.ARTIFACT,
    ActivityType.TASK: EvidenceKind.REPORT,
}
