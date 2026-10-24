"""
Маршруты аутентификации и личного кабинета.
"""

from __future__ import annotations

from django.urls import path

from apps.accounts import views

app_name = "accounts"

urlpatterns = [
    # -- Аутентификация -----------------------------------------------------
    path("login/", views.ReservixLoginView.as_view(), name="login"),
    path("logout/", views.ReservixLogoutView.as_view(), name="logout"),
    path(
        "password/required/",
        views.PasswordChangeRequiredView.as_view(),
        name="password_change_required",
    ),
    path("password/change/", views.PasswordChangeView.as_view(), name="password_change"),
    # -- Восстановление доступа --------------------------------------------
    path("password/reset/", views.ReservixPasswordResetView.as_view(), name="password_reset"),
    path(
        "password/reset/sent/",
        views.ReservixPasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "password/reset/<uidb64>/<token>/",
        views.ReservixPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "password/reset/done/",
        views.ReservixPasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
    # -- Личный кабинет -----------------------------------------------------
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("preferences/", views.PreferencesView.as_view(), name="preferences"),
    # Переключатель темы в шапке сохраняет выбор сюда: иначе страница
    # настроек показывала бы одно оформление, а интерфейс — другое.
    path("appearance/", views.appearance_update, name="appearance"),
    path("security/", views.SecurityView.as_view(), name="security"),
]
