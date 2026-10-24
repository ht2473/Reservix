"""
Формы аутентификации и личных настроек.
"""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    PasswordResetForm,
    SetPasswordForm,
)
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import UserProfile
from core.forms import BaseModelForm, StyledFormMixin
from core.validators import normalize_phone

User = get_user_model()


class LoginForm(StyledFormMixin, AuthenticationForm):
    """Форма входа в систему."""

    username = forms.EmailField(
        label=_("Электронная почта"),
        widget=forms.EmailInput(
            attrs={
                "autofocus": True,
                "autocomplete": "username",
                "placeholder": "ivanov@meridian-soft.ru",
            }
        ),
    )
    password = forms.CharField(
        label=_("Пароль"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )
    remember_me = forms.BooleanField(
        label=_("Запомнить меня на этом устройстве"),
        required=False,
        help_text=_("Не используйте на общедоступных компьютерах"),
    )

    error_messages = {
        # Формулировка намеренно не разделяет «нет такого пользователя» и «неверный пароль»: иначе
        # перебором адресов можно установить, кто работает в организации.
        "invalid_login": _("Неверный адрес электронной почты или пароль"),
        "inactive": _("Учётная запись заблокирована. Обратитесь к администратору."),
    }

    def clean_username(self) -> str:
        return (self.cleaned_data.get("username") or "").strip().lower()


class TwoFactorForm(StyledFormMixin, forms.Form):
    """Ввод одноразового кода при двухфакторной аутентификации."""

    token = forms.CharField(
        label=_("Код подтверждения"),
        max_length=6,
        min_length=6,
        widget=forms.TextInput(
            attrs={
                "autofocus": True,
                "autocomplete": "one-time-code",
                "inputmode": "numeric",
                "pattern": "[0-9]*",
                "placeholder": "000000",
                "class": "field-input--otp",
            }
        ),
        help_text=_("Шестизначный код из приложения-аутентификатора"),
    )

    def clean_token(self) -> str:
        token = (self.cleaned_data.get("token") or "").strip()
        if not token.isdigit():
            raise forms.ValidationError(_("Код состоит только из цифр"))
        return token


class ForcedPasswordChangeForm(StyledFormMixin, PasswordChangeForm):
    """Смена пароля, выданного администратором."""

    def clean_new_password1(self) -> str:
        password = self.cleaned_data.get("new_password1") or ""
        if self.user.check_password(password):
            raise forms.ValidationError(_("Новый пароль должен отличаться от текущего"))
        return password


class StyledPasswordChangeForm(StyledFormMixin, PasswordChangeForm):
    """Смена пароля в личном кабинете."""


class StyledPasswordResetForm(StyledFormMixin, PasswordResetForm):
    """Запрос восстановления доступа."""

    email = forms.EmailField(
        label=_("Электронная почта"),
        max_length=255,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
        help_text=_("Укажите адрес, с которым вы входите в систему"),
    )


class StyledSetPasswordForm(StyledFormMixin, SetPasswordForm):
    """Установка нового пароля по ссылке восстановления."""


class ProfileForm(BaseModelForm):
    """Личные данные пользователя."""

    class Meta:
        model = User
        fields = ["phone"]
        labels = {"phone": _("Контактный телефон")}
        help_texts = {"phone": _("Формат: +7 900 111-22-33")}

    def clean_phone(self) -> str:
        return normalize_phone(self.cleaned_data.get("phone", ""))


class PreferencesForm(BaseModelForm):
    """Настройки интерфейса и уведомлений."""

    class Meta:
        model = UserProfile
        fields = [
            "photo",
            "position_title",
            "timezone",
            "language",
            "theme",
            "density",
            "email_notifications",
            "digest_notifications",
        ]
        widgets = {
            "theme": forms.RadioSelect,
            "density": forms.RadioSelect,
        }

    def clean_timezone(self) -> str:
        """Проверяет, что указан существующий часовой пояс."""
        import zoneinfo

        value = (self.cleaned_data.get("timezone") or "").strip()
        if not value:
            return ""
        if value not in zoneinfo.available_timezones():
            raise forms.ValidationError(
                _("Часовой пояс «%(value)s» не найден. Пример: Asia/Novosibirsk"),
                params={"value": value},
            )
        return value


class UserAdminForm(BaseModelForm):
    """Форма управления учётной записью в панели администратора."""

    class Meta:
        model = User
        fields = [
            "last_name",
            "first_name",
            "middle_name",
            "email",
            "phone",
            "role",
            "scope_level",
            "branch",
            "department",
            "is_department_head",
            "is_active",
            "two_factor_enabled",
        ]

    def clean(self) -> dict:
        # Базовая реализация возвращает None, если форма не проходила предварительную очистку;
        # подстановка пустого словаря избавляет от проверки на каждое обращение.
        cleaned = super().clean() or {}
        department = cleaned.get("department")
        branch = cleaned.get("branch")
        is_head = cleaned.get("is_department_head")

        if is_head and not department:
            self.add_error(
                "department",
                _("Укажите подразделение: признак руководителя без него не имеет смысла"),
            )

        # Подразделение обязано принадлежать выбранному филиалу, иначе область
        # видимости пользователя окажется противоречивой.
        if department and branch and department.branch_id != branch.pk:
            self.add_error(
                "department",
                _("Подразделение относится к другому филиалу"),
            )

        return cleaned


class UserInviteForm(BaseModelForm):
    """Создание учётной записи администратором."""

    send_invitation = forms.BooleanField(
        label=_("Отправить приглашение по электронной почте"),
        required=False,
        initial=True,
    )

    class Meta:
        model = User
        fields = [
            "last_name",
            "first_name",
            "middle_name",
            "email",
            "phone",
            "role",
            "branch",
            "department",
            "is_department_head",
        ]

    def clean_email(self) -> str:
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(_("Учётная запись с таким адресом уже существует"))
        return email
