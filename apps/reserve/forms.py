"""Формы кадрового резерва: выдвижение, решения согласующих, ведение записей."""

from __future__ import annotations

from typing import Any, cast

from django import forms
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.org.models import Department, Position
from apps.people.models import Employee
from apps.reserve.models import (
    NominationKind,
    NominationStatus,
    ReserveEntry,
    ReserveEntryQuerySet,
    ReserveGroup,
    ReserveNomination,
    ReserveStatus,
)
from core.constants import ReadinessLevel, Role
from core.forms import BaseForm, BaseModelForm, FilterForm, model_choice
from core.permissions import apply_employee_scope

#: Минимальная длина обоснования выдвижения.
MIN_JUSTIFICATION_LENGTH = 60

#: Минимальная длина комментария к решению согласующего.
MIN_DECISION_LENGTH = 10


# ---------------------------------------------------------------------------
# Отбор
# ---------------------------------------------------------------------------
class ReserveFilterForm(FilterForm):
    """Отбор записей кадрового резерва."""

    q = forms.CharField(
        label=_("Поиск"),
        widget=forms.TextInput(
            attrs={
                "type": "search",
                "placeholder": _("Фамилия резервиста или целевая должность"),
                "autocomplete": "off",
            }
        ),
    )
    status = forms.ChoiceField(
        label=_("Статус"),
        choices=[("", _("Любой")), *ReserveStatus.choices],
    )
    readiness_level = forms.ChoiceField(
        label=_("Готовность"),
        choices=[("", _("Любая")), *ReadinessLevel.choices],
    )
    reserve_group = forms.ModelChoiceField(
        label=_("Группа резерва"),
        queryset=ReserveGroup.objects.filter(is_active=True),
        empty_label=_("Все группы"),
    )
    department = forms.ModelChoiceField(
        label=_("Подразделение"),
        queryset=Department.objects.filter(is_active=True),
        empty_label=_("Все подразделения"),
    )
    scope = forms.ChoiceField(
        label=_("Отбор"),
        choices=[
            ("", _("Все записи")),
            ("active", _("Состоящие в резерве")),
            ("ready", _("Готовы к назначению")),
            ("overdue", _("Просрочен пересмотр")),
            ("unranked", _("Без индекса готовности")),
        ],
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        """Применяет условия отбора к записям резерва."""
        data = self.cleaned_data or {}

        if term := data.get("q"):
            queryset = queryset.filter(
                Q(employee__last_name__icontains=term)
                | Q(employee__first_name__icontains=term)
                | Q(target_position__name__icontains=term)
            )
        if status := data.get("status"):
            queryset = queryset.filter(status=status)
        if level := data.get("readiness_level"):
            queryset = queryset.filter(readiness_level=level)
        if group := data.get("reserve_group"):
            queryset = queryset.filter(reserve_group=group)
        if department := data.get("department"):
            # Подразделения вложены друг в друга: отбор по департаменту должен захватывать его
            # отделы, иначе фильтр по верхнему уровню даёт пустой список при непустом резерве.
            queryset = queryset.filter(
                employee__department_id__in=Department.objects.descendant_ids(department.pk)
            )

        # Выборки `in_reserve`, `ready_now` и `due_for_review` объявлены в менеджере записей
        # резерва, а форма отбора получает выборку обезличенной: приведение возвращает ей
        # собственный тип.
        entries = cast(ReserveEntryQuerySet, queryset)

        scope = data.get("scope")
        if scope == "active":
            return entries.in_reserve()
        if scope == "ready":
            return entries.ready_now()
        if scope == "overdue":
            return entries.due_for_review()
        if scope == "unranked":
            return entries.filter(readiness_index__isnull=True)

        return entries


class NominationFilterForm(FilterForm):
    """Отбор заявок на выдвижение."""

    q = forms.CharField(
        label=_("Поиск"),
        widget=forms.TextInput(
            attrs={"type": "search", "placeholder": _("Кандидат или должность")}
        ),
    )
    status = forms.ChoiceField(
        label=_("Состояние"),
        choices=[("", _("Любое")), *NominationStatus.choices],
    )
    kind = forms.ChoiceField(
        label=_("Вид"),
        choices=[("", _("Любой")), *NominationKind.choices],
    )

    def filter(self, queryset: QuerySet) -> QuerySet:
        data = self.cleaned_data or {}
        if term := data.get("q"):
            queryset = queryset.filter(
                Q(employee__last_name__icontains=term) | Q(target_position__name__icontains=term)
            )
        if status := data.get("status"):
            queryset = queryset.filter(status=status)
        if kind := data.get("kind"):
            queryset = queryset.filter(kind=kind)
        return queryset


class CandidateSearchForm(FilterForm):
    """Параметры подбора кандидатов на должность."""

    position = forms.ModelChoiceField(
        label=_("Целевая должность"),
        queryset=Position.objects.filter(is_active=True).select_related("grade"),
        empty_label=_("Выберите должность"),
        required=True,
    )
    department = forms.ModelChoiceField(
        label=_("Подразделение"),
        queryset=Department.objects.filter(is_active=True),
        empty_label=_("Вся организация"),
    )
    min_match = forms.IntegerField(
        label=_("Соответствие не ниже, %"),
        min_value=0,
        max_value=100,
        initial=50,
        widget=forms.NumberInput(attrs={"step": 5}),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Поле обязательно вопреки правилу формы отбора: подбор без должности
        # лишён смысла — сравнивать сотрудника не с чем.
        self.fields["position"].required = True


# ---------------------------------------------------------------------------
# Выдвижение
# ---------------------------------------------------------------------------
class NominationForm(BaseModelForm):
    """Заявка на включение сотрудника в кадровый резерв."""

    #: Замечания, не препятствующие подаче.
    warnings: tuple = ()

    class Meta:
        model = ReserveNomination
        fields = [
            "employee",
            "target_position",
            "reserve_group",
            "justification",
            "achievements",
        ]
        widgets = {
            "justification": forms.Textarea(
                attrs={
                    "rows": 5,
                    "placeholder": _(
                        "Почему кандидат готов к подготовке на эту должность: "
                        "результаты, проявленные качества, опыт замещения"
                    ),
                }
            ),
            "achievements": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        model_choice(self, "target_position").queryset = Position.objects.filter(
            is_active=True
        ).select_related("grade")
        group_field = model_choice(self, "reserve_group")
        group_field.queryset = ReserveGroup.objects.filter(is_active=True)
        group_field.empty_label = _("Без программы подготовки")

    def limit_choices_to_scope(self) -> None:
        """Ограничивает список кандидатов областью видимости пользователя."""
        user = self.user
        if user is None:
            return

        # Себя выдвигают через самовыдвижение, а не заявкой от имени руководителя.
        candidates = Employee.objects.reserve_eligible().with_org().exclude(user_id=user.pk)
        if user.role not in {Role.ADMIN, Role.HR}:
            condition = Q(manager__user_id=user.pk)
            if user.is_department_head and user.department_id:
                condition |= Q(
                    department_id__in=Department.objects.descendant_ids(user.department_id)
                )
            candidates = candidates.filter(condition)
        else:
            candidates = apply_employee_scope(candidates, user)

        model_choice(self, "employee").queryset = candidates.order_by("last_name", "first_name")

    def clean_justification(self) -> str:
        """Требует содержательного обоснования выдвижения."""
        text = (self.cleaned_data.get("justification") or "").strip()
        if len(text) < MIN_JUSTIFICATION_LENGTH:
            raise forms.ValidationError(
                _(
                    "Обоснование должно содержать не менее %(count)s символов: "
                    "именно оно рассматривается согласующими"
                )
                % {"count": MIN_JUSTIFICATION_LENGTH}
            )
        return text

    def clean(self) -> dict[str, Any]:
        """Проверяет пригодность кандидата средствами предметного слоя."""
        cleaned = super().clean() or {}
        employee = cleaned.get("employee")
        position = cleaned.get("target_position")
        if employee is None or position is None:
            return cleaned

        from apps.reserve.services import check_eligibility

        eligibility = check_eligibility(
            employee, position, group=cleaned.get("reserve_group"), with_profile=False
        )
        for issue in eligibility.blockers:
            self.add_error(None, issue.message)

        # Замечания, не препятствующие подаче, сохраняются для показа рядом с формой: инициатор
        # должен видеть их до отправки, а согласующий — в карточке заявки.
        self.warnings = eligibility.warnings
        return cleaned


class SelfNominationForm(NominationForm):
    """Самовыдвижение сотрудника."""

    class Meta(NominationForm.Meta):
        fields = ["target_position", "reserve_group", "justification", "achievements"]

    def __init__(self, *args, employee: Employee | None = None, **kwargs) -> None:
        self.employee = employee
        super().__init__(*args, **kwargs)
        self.fields["justification"].label = _("Почему вы готовы к этой должности")
        self.fields["achievements"].label = _("Ваши результаты за последний год")

    def limit_choices_to_scope(self) -> None:
        """Список кандидатов в самовыдвижении не используется."""

    def clean(self) -> dict[str, Any]:
        cleaned = forms.ModelForm.clean(self) or {}
        position = cleaned.get("target_position")
        if self.employee is None or position is None:
            return cleaned

        from apps.reserve.services import check_eligibility

        eligibility = check_eligibility(
            self.employee, position, group=cleaned.get("reserve_group"), with_profile=False
        )
        for issue in eligibility.blockers:
            self.add_error(None, issue.message)
        self.warnings = eligibility.warnings
        return cleaned

    def save(self, commit: bool = True) -> ReserveNomination:
        nomination = super().save(commit=False)
        if self.employee is not None:
            nomination.employee = self.employee
        nomination.kind = NominationKind.SELF
        if commit:
            nomination.save()
        return nomination


class NominationDecisionForm(BaseForm):
    """Решение согласующего по заявке."""

    decision = forms.ChoiceField(
        label=_("Решение"),
        choices=[("approve", _("Согласовать")), ("reject", _("Отклонить"))],
        widget=forms.RadioSelect,
    )
    comment = forms.CharField(
        label=_("Комментарий"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": _("Основание решения")}),
        max_length=1000,
    )

    @property
    def approved(self) -> bool:
        return self.cleaned_data.get("decision") == "approve"

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        comment = (cleaned.get("comment") or "").strip()
        if cleaned.get("decision") == "reject" and len(comment) < MIN_DECISION_LENGTH:
            self.add_error(
                "comment",
                _("При отказе комментарий обязателен: кандидат увидит именно его"),
            )
        cleaned["comment"] = comment
        return cleaned


class HrDecisionForm(NominationDecisionForm):
    """Решение кадровой службы с выбором программы подготовки."""

    reserve_group = forms.ModelChoiceField(
        label=_("Группа резерва"),
        queryset=ReserveGroup.objects.filter(is_active=True),
        required=False,
        empty_label=_("Без программы подготовки"),
    )
    review_months = forms.IntegerField(
        label=_("Пересмотр через, месяцев"),
        min_value=6,
        max_value=60,
        initial=24,
        help_text=_("Срок, через который запись выносится на очередной пересмотр"),
    )


# ---------------------------------------------------------------------------
# Ведение записей резерва
# ---------------------------------------------------------------------------
class ReserveEntryForm(BaseModelForm):
    """Изменение записи резерва кадровой службой."""

    class Meta:
        model = ReserveEntry
        fields = [
            "reserve_group",
            "expected_ready_date",
            "review_date",
            "justification",
            "notes",
        ]
        widgets = {
            "expected_ready_date": forms.DateInput(attrs={"type": "date"}),
            "review_date": forms.DateInput(attrs={"type": "date"}),
            "justification": forms.Textarea(attrs={"rows": 4}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        group_field = model_choice(self, "reserve_group")
        group_field.queryset = ReserveGroup.objects.filter(is_active=True)
        group_field.empty_label = _("Без программы подготовки")

    def clean_review_date(self):
        """Дата пересмотра в прошлом делает запись просроченной с рождения."""
        value = self.cleaned_data.get("review_date")
        if value and value < timezone.localdate():
            raise forms.ValidationError(_("Дата пересмотра не может быть в прошлом"))
        return value


class StatusChangeForm(BaseForm):
    """Перевод записи резерва в другое состояние."""

    status = forms.ChoiceField(label=_("Новое состояние"), choices=[])
    reason = forms.CharField(
        label=_("Основание"),
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=1000,
    )

    def __init__(self, *args, entry: ReserveEntry, **kwargs) -> None:
        self.entry = entry
        super().__init__(*args, **kwargs)

        labels = dict(ReserveStatus.choices)
        allowed = sorted(
            status for status in ReserveStatus.values if entry.can_transition_to(status)
        )
        status_field = self.fields["status"]
        status_field.choices = [(status, labels[status]) for status in allowed]  # type: ignore[attr-defined]

        if not allowed:
            self.fields["status"].help_text = _(
                "Запись находится в завершающем состоянии: переходы из него не предусмотрены"
            )

    def clean_reason(self) -> str:
        reason = (self.cleaned_data.get("reason") or "").strip()
        if len(reason) < MIN_DECISION_LENGTH:
            raise forms.ValidationError(
                _("Основание сохраняется в истории решений и должно быть содержательным")
            )
        return reason


class ReserveGroupForm(BaseModelForm):
    """Ведение программы подготовки резерва."""

    class Meta:
        model = ReserveGroup
        fields = [
            "code",
            "name",
            "kind",
            "description",
            "horizon_months",
            "max_age",
            "min_tenure_months",
            "min_readiness_index",
            "capacity",
            "curator",
            "sort_order",
            "is_active",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def clean(self) -> dict[str, Any]:
        """Не даёт задать предел численности ниже фактического состава."""
        cleaned = super().clean() or {}
        capacity = cleaned.get("capacity")
        if capacity and self.instance.pk:
            members = self.instance.member_count
            if capacity < members:
                self.add_error(
                    "capacity",
                    _("В группе уже состоит %(count)s человек") % {"count": members},
                )
        return cleaned
