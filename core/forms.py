"""Базовые формы и виджеты."""

from __future__ import annotations

from typing import Any, cast

from django import forms
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

#: Соответствие типа виджета и класса оформления из дизайн-системы.
WIDGET_CLASSES: dict[type[forms.Widget], str] = {
    forms.TextInput: "field-input",
    forms.EmailInput: "field-input",
    forms.URLInput: "field-input",
    forms.NumberInput: "field-input field-input--numeric",
    forms.PasswordInput: "field-input",
    forms.DateInput: "field-input field-input--date",
    forms.DateTimeInput: "field-input field-input--date",
    forms.TimeInput: "field-input field-input--date",
    forms.Textarea: "field-textarea",
    forms.Select: "field-select",
    forms.SelectMultiple: "field-select field-select--multiple",
    forms.NullBooleanSelect: "field-select",
    forms.CheckboxInput: "field-checkbox",
    forms.CheckboxSelectMultiple: "field-checkgroup",
    forms.RadioSelect: "field-radiogroup",
    forms.FileInput: "field-file",
    forms.ClearableFileInput: "field-file",
}


def model_choice(form: forms.BaseForm, name: str) -> forms.ModelChoiceField:
    """Поле выбора объекта по имени — с типом, а не с обещанием типа."""
    return cast(forms.ModelChoiceField, form.fields[name])


class StyledFormMixin:
    """Автоматическое оформление полей формы."""

    #: Подсказки браузеру для автозаполнения по имени поля.
    AUTOCOMPLETE_HINTS = {
        "email": "email",
        "phone": "tel",
        "first_name": "given-name",
        "last_name": "family-name",
        # Значения — подсказки браузеру для автозаполнения, а не пароли.
        "password": "current-password",  # nosec B105
        "new_password1": "new-password",
        "new_password2": "new-password",
    }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._apply_styling()

    def _apply_styling(self) -> None:
        for name, field in self.fields.items():
            widget = field.widget
            css = WIDGET_CLASSES.get(type(widget), "field-input")

            existing = widget.attrs.get("class", "")
            widget.attrs["class"] = f"{existing} {css}".strip()

            if field.required:
                widget.attrs.setdefault("required", "required")

            if name in self.AUTOCOMPLETE_HINTS:
                widget.attrs.setdefault("autocomplete", self.AUTOCOMPLETE_HINTS[name])

            # Пустая подпись у списка выбора заменяется осмысленной:
            # значение «---------» по умолчанию ничего не сообщает пользователю.
            is_single_select = isinstance(widget, forms.Select) and not isinstance(
                widget, forms.SelectMultiple
            )
            # У списка объектов меняется только подпись: варианты остаются связаны с
            # выборкой, которую форма сужает позже по области видимости.
            if isinstance(field, forms.ModelChoiceField):
                if is_single_select and field.empty_label == "---------":
                    field.empty_label = _("— не выбрано —")
            elif is_single_select and getattr(field, "choices", None):
                choices = list(field.choices)
                if choices and choices[0][0] == "":
                    field.choices = [("", _("— не выбрано —")), *choices[1:]]

    def add_error(self, field: str | None, error) -> None:
        """Дополняет виджет атрибутами доступности при появлении ошибки."""
        super().add_error(field, error)
        if field and field in self.fields:
            widget = self.fields[field].widget
            widget.attrs["aria-invalid"] = "true"
            widget.attrs["aria-describedby"] = f"error-{self.add_prefix(field)}"
            widget.attrs["class"] = f"{widget.attrs.get('class', '')} is-invalid".strip()


class BaseForm(StyledFormMixin, forms.Form):
    """Оформленная форма, не связанная с моделью."""


class BaseModelForm(StyledFormMixin, forms.ModelForm):
    """Оформленная форма модели."""

    def __init__(self, *args, user=None, **kwargs) -> None:
        self.user = user
        super().__init__(*args, **kwargs)
        if user is not None:
            self.limit_choices_to_scope()

    def limit_choices_to_scope(self) -> None:
        """Ограничивает варианты выбора областью видимости пользователя."""


class FilterForm(StyledFormMixin, forms.Form):
    """Форма фильтрации списка."""

    def __init__(self, *args, user=None, **kwargs) -> None:
        self.user = user
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.required = False
            field.widget.attrs.pop("required", None)

    def filter(self, queryset: QuerySet) -> QuerySet:
        """Применяет фильтры к выборке."""
        for name, value in self.cleaned_data.items():
            if value in (None, "", [], ()):
                continue
            queryset = queryset.filter(**{name: value})
        return queryset

    @property
    def active_count(self) -> int:
        """Число применённых фильтров — выводится значком рядом с кнопкой."""
        if not self.is_valid():
            return 0
        return sum(1 for value in self.cleaned_data.values() if value not in (None, "", [], ()))


class SearchForm(FilterForm):
    """Строка поиска с единым оформлением и подсказкой."""

    q = forms.CharField(
        label=_("Поиск"),
        required=False,
        widget=forms.TextInput(
            attrs={
                "type": "search",
                "placeholder": _("Фамилия, должность, подразделение…"),
                "autocomplete": "off",
                "spellcheck": "false",
            }
        ),
    )


class ConfirmForm(BaseForm):
    """Подтверждение необратимого действия."""

    reason = forms.CharField(
        label=_("Основание"),
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": _("Укажите причину решения")}),
        max_length=1000,
        help_text=_("Комментарий сохраняется в истории и виден проверяющим"),
    )
    confirm = forms.BooleanField(
        label=_("Подтверждаю выполнение действия"),
        required=True,
    )

    def clean_reason(self) -> str:
        reason = (self.cleaned_data.get("reason") or "").strip()
        if len(reason) < 10:
            raise forms.ValidationError(_("Основание должно содержать не менее 10 символов"))
        return reason


class DateRangeMixin:
    """Проверка согласованности периода в формах с датами начала и окончания."""

    start_field = "start_date"
    end_field = "end_date"

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        start = cleaned.get(self.start_field)
        end = cleaned.get(self.end_field)
        if start and end and end < start:
            self.add_error(
                self.end_field,
                _("Дата окончания не может быть раньше даты начала"),
            )
        return cleaned
