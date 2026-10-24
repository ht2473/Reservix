"""
Формы публичной части.
"""

from __future__ import annotations

import time

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.portal.models import Feedback
from core.forms import BaseModelForm
from core.validators import normalize_phone

#: Минимальное время заполнения формы. Автоматические отправители заполняют
#: и отправляют форму мгновенно; человек тратит хотя бы несколько секунд.
MIN_FILL_SECONDS = 3


class FeedbackForm(BaseModelForm):
    """Форма обратной связи."""

    #: Приманка. Поле скрыто стилями и помечено как недоступное для
    #: автозаполнения; заполненное значение однозначно указывает на робота.
    website = forms.CharField(
        required=False,
        label=_("Не заполняйте это поле"),
        widget=forms.TextInput(
            attrs={
                "class": "field-honeypot",
                "tabindex": "-1",
                "autocomplete": "off",
                "aria-hidden": "true",
            }
        ),
    )
    form_rendered_at = forms.CharField(required=False, widget=forms.HiddenInput)

    consent_given = forms.BooleanField(
        label=_("Согласен на обработку персональных данных"),
        required=True,
        error_messages={
            "required": _("Без согласия на обработку данных обращение принять невозможно")
        },
    )

    class Meta:
        model = Feedback
        fields = [
            "name",
            "email",
            "phone",
            "organization",
            "topic",
            "subject",
            "message",
            "consent_given",
        ]
        widgets = {
            "message": forms.Textarea(
                attrs={"rows": 6, "placeholder": _("Опишите вопрос или предложение подробно")}
            ),
            "subject": forms.TextInput(attrs={"placeholder": _("Кратко о чём обращение")}),
            "name": forms.TextInput(attrs={"placeholder": _("Иванов Иван Иванович")}),
        }
        help_texts = {
            "phone": _("Необязательно. Укажите, если удобнее ответить звонком"),
            "organization": _("Необязательно"),
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["form_rendered_at"].initial = str(int(time.time()))
        # Авторизованный пользователь не должен вводить то, что система знает.
        if self.user is not None and self.user.is_authenticated:
            self.fields["name"].initial = self.user.full_name
            self.fields["email"].initial = self.user.email
            self.fields["phone"].initial = self.user.phone

    def clean_website(self) -> str:
        """Проверяет приманку."""
        if self.cleaned_data.get("website"):
            raise forms.ValidationError(_("Обращение отклонено"), code="honeypot")
        return ""

    def clean_form_rendered_at(self) -> str:
        """Проверяет минимальное время заполнения формы."""
        raw = self.cleaned_data.get("form_rendered_at") or ""
        if not raw.isdigit():
            # Отсутствие метки означает изменённую форму: пропускаем проверку,
            # но не отклоняем обращение — пользователь мог отключить скрипты.
            return raw
        elapsed = int(time.time()) - int(raw)
        if elapsed < MIN_FILL_SECONDS:
            raise forms.ValidationError(
                _("Форма заполнена слишком быстро. Проверьте данные и отправьте ещё раз."),
                code="too_fast",
            )
        return raw

    def clean_phone(self) -> str:
        phone = self.cleaned_data.get("phone", "")
        return normalize_phone(phone) if phone else ""

    def clean_message(self) -> str:
        message = (self.cleaned_data.get("message") or "").strip()
        if len(message) < 20:
            raise forms.ValidationError(_("Опишите обращение подробнее — не менее 20 символов"))
        # Ссылки в теле обращения — характерный признак рассылки; сообщение
        # не отклоняется, но отмечается для последующей проверки.
        if message.lower().count("http") > 2:
            self.suspected_spam = True
        return message
