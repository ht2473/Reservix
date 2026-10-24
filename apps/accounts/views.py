"""
Представления аутентификации и личного кабинета пользователя.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import (
    LoginView,
    LogoutView,
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.translation import activate, get_language
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST
from django.views.generic import FormView, TemplateView, UpdateView

from apps.accounts.forms import (
    ForcedPasswordChangeForm,
    LoginForm,
    PreferencesForm,
    ProfileForm,
    StyledPasswordChangeForm,
    StyledPasswordResetForm,
    StyledSetPasswordForm,
)
from apps.accounts.models import LoginRecord
from core import appearance as appearance_module
from core.breadcrumbs import BreadcrumbTrail
from core.mixins import BreadcrumbMixin, PageMetaMixin, RedirectToRoleHomeMixin
from core.navigation import get_role_home_url
from core.views import set_language_cookie

#: Срок сессии при отказе от запоминания устройства — до закрытия браузера.
SESSION_UNTIL_BROWSER_CLOSE = 0


def apply_user_preferences(request: HttpRequest, response: HttpResponse, user) -> HttpResponse:
    """Переносит язык и оформление из профиля в ответ."""
    profile = getattr(user, "profile", None)
    if profile is None:
        return response

    language = profile.language
    supported = {code for code, _label in settings.LANGUAGES}
    if language in supported and language != get_language():
        # Адрес перевода вычисляется до переключения языка.
        location = response.headers.get("Location")
        if location:
            from django.urls import translate_url

            response.headers["Location"] = translate_url(location, language)

        activate(language)
        request.LANGUAGE_CODE = language
        set_language_cookie(response, language)

    appearance_module.remember(
        response,
        appearance_module.Appearance(theme=profile.theme, density=profile.density),
    )
    return response


class ReservixLoginView(RedirectToRoleHomeMixin, PageMetaMixin, BreadcrumbMixin, LoginView):
    """Вход в систему."""

    template_name = "account/login.html"
    form_class = LoginForm
    redirect_authenticated_user = False  # обработано примесью, с корректным адресом
    page_title = _("Вход в систему")
    page_description = _("Авторизация пользователей системы кадрового резерва")

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Вход в систему"))

    def form_valid(self, form: AuthenticationForm) -> HttpResponse:
        response = super().form_valid(form)

        if not form.cleaned_data.get("remember_me"):
            self.request.session.set_expiry(SESSION_UNTIL_BROWSER_CLOSE)

        # Временный пароль, выданный администратором, должен быть заменён до
        # начала работы: иначе учётные записи месяцами живут с общим паролем.
        if self.request.user.must_change_password:
            messages.warning(
                self.request,
                _("Пароль выдан администратором. Установите собственный пароль."),
            )
            return redirect("accounts:password_change_required")

        messages.success(
            self.request,
            _("Добро пожаловать, %(name)s") % {"name": self.request.user.first_name},
        )
        return apply_user_preferences(self.request, response, self.request.user)

    def get_success_url(self) -> str:
        redirect_to = self.get_redirect_url()
        return redirect_to or get_role_home_url(self.request.user)


class ReservixLogoutView(LogoutView):
    """Выход из системы."""

    # Ленивое разрешение адреса: маршруты ещё не загружены в момент
    # объявления класса. Базовый класс объявляет поле как строку.
    next_page = reverse_lazy("portal:home")  # type: ignore[assignment]

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        response = super().post(request, *args, **kwargs)
        messages.info(request, _("Вы вышли из системы"))
        return response


class PasswordChangeRequiredView(LoginRequiredMixin, PageMetaMixin, BreadcrumbMixin, FormView):
    """Обязательная смена временного пароля."""

    template_name = "account/password_change_required.html"
    form_class = ForcedPasswordChangeForm
    page_title = _("Смена пароля")
    page_subtitle = _("Установите собственный пароль для продолжения работы")

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponseBase:
        if request.user.is_authenticated and not request.user.must_change_password:
            return redirect(get_role_home_url(request.user))
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Смена пароля"))

    def form_valid(self, form: ForcedPasswordChangeForm) -> HttpResponse:
        user = form.save()
        # Смена пароля инвалидирует сессию: без обновления хеша пользователь
        # был бы разлогинен сразу после успешной смены.
        update_session_auth_hash(self.request, user)
        messages.success(self.request, _("Пароль изменён"))
        return redirect(get_role_home_url(user))


class ProfileView(LoginRequiredMixin, PageMetaMixin, BreadcrumbMixin, UpdateView):
    """Личные данные пользователя."""

    template_name = "account/profile.html"
    form_class = ProfileForm
    success_url = reverse_lazy("accounts:profile")
    page_title = _("Мой профиль")
    page_subtitle = _("Личные и контактные данные")
    page_section = "account"

    def get_object(self, queryset=None):
        return self.request.user

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Мой профиль"))

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["employee_card"] = self.request.user.employee
        return context

    def form_valid(self, form) -> HttpResponse:
        messages.success(self.request, _("Данные профиля сохранены"))
        return super().form_valid(form)


class PreferencesView(LoginRequiredMixin, PageMetaMixin, BreadcrumbMixin, UpdateView):
    """Настройки интерфейса и уведомлений."""

    template_name = "account/preferences.html"
    form_class = PreferencesForm
    success_url = reverse_lazy("accounts:preferences")
    page_title = _("Настройки")
    page_subtitle = _("Оформление, язык и уведомления")
    page_section = "account"

    def get_object(self, queryset=None):
        return self.request.user.profile

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Мой профиль"), "accounts:profile").add(_("Настройки"))

    def form_valid(self, form) -> HttpResponse:
        response = super().form_valid(form)
        messages.success(self.request, _("Настройки сохранены"))
        return apply_user_preferences(self.request, response, self.request.user)


@require_POST
@login_required
def appearance_update(request: HttpRequest) -> HttpResponse:
    """Сохраняет выбор темы и плотности, сделанный переключателем в шапке."""
    theme = request.POST.get("theme", "")
    density = request.POST.get("density", "")
    profile = getattr(request.user, "profile", None)

    if profile is None or theme not in appearance_module.THEMES:
        return HttpResponse(status=400)

    profile.theme = theme
    if density in appearance_module.DENSITIES:
        profile.density = density
    profile.save(update_fields=["theme", "density"])

    response = HttpResponse(status=204)
    return appearance_module.remember(
        response, appearance_module.Appearance(theme=profile.theme, density=profile.density)
    )


class SecurityView(LoginRequiredMixin, PageMetaMixin, BreadcrumbMixin, TemplateView):
    """Безопасность учётной записи."""

    template_name = "account/security.html"
    page_title = _("Безопасность")
    page_subtitle = _("История входов и защита учётной записи")
    page_section = "account"

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Мой профиль"), "accounts:profile").add(_("Безопасность"))

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        records = LoginRecord.objects.filter(user=self.request.user)[:50]

        context["login_records"] = records
        context["failed_count"] = sum(
            1 for record in records if record.outcome == LoginRecord.Outcome.FAILED
        )
        context["password_changed_at"] = self.request.user.password_changed_at
        context["two_factor_enabled"] = self.request.user.two_factor_enabled
        return context


class PasswordChangeView(LoginRequiredMixin, PageMetaMixin, BreadcrumbMixin, FormView):
    """Смена пароля по инициативе пользователя."""

    template_name = "account/password_change.html"
    form_class = StyledPasswordChangeForm
    success_url = reverse_lazy("accounts:security")
    page_title = _("Смена пароля")
    page_section = "account"

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return (
            trail.add(_("Мой профиль"), "accounts:profile")
            .add(_("Безопасность"), "accounts:security")
            .add(_("Смена пароля"))
        )

    def form_valid(self, form: StyledPasswordChangeForm) -> HttpResponse:
        user = form.save()
        update_session_auth_hash(self.request, user)
        messages.success(self.request, _("Пароль изменён"))
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Восстановление доступа
# ---------------------------------------------------------------------------
class ReservixPasswordResetView(PageMetaMixin, BreadcrumbMixin, PasswordResetView):
    """Запрос ссылки восстановления доступа."""

    template_name = "account/password_reset.html"
    email_template_name = "account/email/password_reset.txt"
    subject_template_name = "account/email/password_reset_subject.txt"
    form_class = StyledPasswordResetForm
    success_url = reverse_lazy("accounts:password_reset_done")
    page_title = _("Восстановление доступа")
    page_subtitle = _("Отправим ссылку для установки нового пароля")

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Вход в систему"), "accounts:login").add(_("Восстановление доступа"))


class ReservixPasswordResetDoneView(PageMetaMixin, BreadcrumbMixin, PasswordResetDoneView):
    """Подтверждение отправки ссылки."""

    template_name = "account/password_reset_done.html"
    page_title = _("Письмо отправлено")

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Вход в систему"), "accounts:login").add(_("Письмо отправлено"))


class ReservixPasswordResetConfirmView(PageMetaMixin, BreadcrumbMixin, PasswordResetConfirmView):
    """Установка нового пароля по ссылке из письма."""

    template_name = "account/password_reset_confirm.html"
    form_class = StyledSetPasswordForm
    success_url = reverse_lazy("accounts:password_reset_complete")
    page_title = _("Новый пароль")

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Восстановление доступа")).add(_("Новый пароль"))


class ReservixPasswordResetCompleteView(PageMetaMixin, BreadcrumbMixin, PasswordResetCompleteView):
    """Сообщение об успешной установке пароля."""

    template_name = "account/password_reset_complete.html"
    page_title = _("Пароль изменён")

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Восстановление доступа")).add(_("Пароль изменён"))


def lockout(request: HttpRequest, credentials: dict | None = None, *args, **kwargs) -> HttpResponse:
    """Страница блокировки после серии неудачных попыток входа."""
    trail = BreadcrumbTrail(request).add(_("Доступ временно ограничен"))
    return render(
        request,
        "account/lockout.html",
        {
            "page_title": _("Доступ временно ограничен"),
            "breadcrumbs": trail.resolve(),
            "cooloff_hours": 1,
            "reset_url": reverse("accounts:password_reset"),
        },
        status=429,
    )
