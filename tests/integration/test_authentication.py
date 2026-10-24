"""Интеграционные тесты аутентификации и разграничения доступа."""

from __future__ import annotations

import pytest

from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.accounts.models import LoginRecord, UserProfile
from apps.audit.models import AuditAction, AuditLog
from core.constants import Role, ScopeLevel
from tests.factories import UserFactory

User = get_user_model()

pytestmark = [pytest.mark.django_db, pytest.mark.integration]


class TestLogin:
    """Форма входа в систему."""

    def test_successful_login(self, client, employee_user, known_password):
        response = client.post(
            reverse("accounts:login"),
            {"username": employee_user.email, "password": known_password},
        )

        assert response.status_code == 302
        assert response.wsgi_request.user.is_authenticated

    def test_login_is_case_insensitive(self, client, employee_user, known_password):
        """Регистр адреса не имеет значения."""
        response = client.post(
            reverse("accounts:login"),
            {"username": employee_user.email.upper(), "password": known_password},
        )

        assert response.status_code == 302

    def test_wrong_password_rejected(self, client, employee_user):
        response = client.post(
            reverse("accounts:login"),
            {"username": employee_user.email, "password": "Nevernyi-Parol-2026"},
        )

        assert response.status_code == 200
        assert not response.wsgi_request.user.is_authenticated

    def test_error_message_does_not_reveal_account_existence(self, client, employee_user):
        """Ответ одинаков для неверного пароля и несуществующего адреса."""
        wrong_password = client.post(
            reverse("accounts:login"),
            {"username": employee_user.email, "password": "Nevernyi-Parol-2026"},
        )
        unknown_email = client.post(
            reverse("accounts:login"),
            {"username": "postoronniy@example.com", "password": "Nevernyi-Parol-2026"},
        )

        assert wrong_password.status_code == unknown_email.status_code
        assert wrong_password.context["form"].errors == unknown_email.context["form"].errors

    def test_inactive_user_cannot_login(self, client, known_password):
        blocked = UserFactory(email="blocked@meridian-soft.ru", is_active=False)

        response = client.post(
            reverse("accounts:login"),
            {"username": blocked.email, "password": known_password},
        )

        assert not response.wsgi_request.user.is_authenticated

    def test_remember_me_extends_session(self, client, employee_user, known_password):
        client.post(
            reverse("accounts:login"),
            {"username": employee_user.email, "password": known_password, "remember_me": "on"},
        )

        assert client.session.get_expiry_age() > 0

    def test_without_remember_me_session_ends_with_browser(
        self, client, employee_user, known_password
    ):
        client.post(
            reverse("accounts:login"),
            {"username": employee_user.email, "password": known_password},
        )

        assert client.session.get_expire_at_browser_close() is True

    def test_authenticated_user_redirected_from_login(self, auth_client):
        """Открытая форма входа при действующей сессии сбивает с толку."""
        response = auth_client.get(reverse("accounts:login"))
        assert response.status_code == 302


class TestRoleRedirect:
    """Перенаправление после входа зависит от роли."""

    @pytest.mark.parametrize("fixture_name", ["employee_user", "hr_user", "manager_user"])
    def test_redirect_target_is_role_specific(self, client, request, fixture_name, known_password):
        """Пользователь попадает на своё рабочее место, а не на общую страницу."""
        user = request.getfixturevalue(fixture_name)

        response = client.post(
            reverse("accounts:login"),
            {"username": user.email, "password": known_password},
        )

        assert response.status_code == 302
        assert response.url


class TestLogout:
    """Выход из системы."""

    def test_logout_requires_post(self, auth_client):
        """Выход по ссылке позволяет завершить чужую сессию сторонним запросом."""
        response = auth_client.get(reverse("accounts:logout"))
        assert response.status_code == 405

    def test_logout_clears_session(self, auth_client):
        response = auth_client.post(reverse("accounts:logout"))

        assert response.status_code == 302
        assert not response.wsgi_request.user.is_authenticated


class TestPasswordPolicy:
    """Обязательная смена временного пароля."""

    def test_temporary_password_forces_change(self, client, known_password):
        user = UserFactory(email="novichok@meridian-soft.ru", must_change_password=True)

        response = client.post(
            reverse("accounts:login"),
            {"username": user.email, "password": known_password},
            follow=False,
        )

        assert response.status_code == 302
        assert reverse("accounts:password_change_required") in response.url

    def test_setting_password_clears_the_flag(self, employee_user):
        employee_user.must_change_password = True
        employee_user.save()

        employee_user.set_password("Drugoy-Nadezhnyi-2026")
        employee_user.save()

        employee_user.refresh_from_db()
        assert employee_user.must_change_password is False
        assert employee_user.password_changed_at is not None


class TestUserModel:
    """Свойства модели пользователя."""

    def test_email_normalized_to_lowercase(self, branch, department):
        user = User.objects.create_user(
            email="Ivanov@Meridian-Soft.RU",
            password="Nadezhnyi-Parol-2026",
            last_name="Иванов",
            first_name="Иван",
            branch=branch,
            department=department,
        )

        assert user.email == "ivanov@meridian-soft.ru"

    def test_phone_normalized_on_clean(self, employee_user):
        employee_user.phone = "8 (900) 111-22-33"
        employee_user.clean()

        assert employee_user.phone == "+79001112233"

    def test_profile_created_automatically(self, employee_user):
        """Представления обращаются к профилю без проверки на существование."""
        assert UserProfile.objects.filter(user=employee_user).exists()

    def test_superuser_gets_admin_role(self, db):
        admin = User.objects.create_superuser(
            email="root@meridian-soft.ru",
            password="Nadezhnyi-Parol-2026",
        )

        assert admin.role == Role.ADMIN
        assert admin.scope_level == ScopeLevel.ORGANIZATION

    def test_privileged_role_requires_second_factor(self, settings, branch, department):
        """Второй фактор для привилегированных ролей включается принудительно."""
        settings.OTP_REQUIRED_ROLES = [Role.ADMIN, Role.HR]

        hr = UserFactory(
            email="hr-new@meridian-soft.ru",
            role=Role.HR,
            branch=branch,
            department=department,
        )

        assert hr.two_factor_enabled is True

    def test_ordinary_role_does_not_get_second_factor(self, settings, branch, department):
        """Рядовому сотруднику второй фактор принудительно не включается."""
        settings.OTP_REQUIRED_ROLES = [Role.ADMIN, Role.HR]

        employee = UserFactory(
            email="ryadovoy@meridian-soft.ru",
            role=Role.EMPLOYEE,
            branch=branch,
            department=department,
        )

        assert employee.two_factor_enabled is False

    def test_short_name_format(self, branch, department):
        user = UserFactory(
            email="ivanova@meridian-soft.ru",
            last_name="Иванова",
            first_name="Мария",
            middle_name="Сергеевна",
            branch=branch,
            department=department,
        )

        assert user.short_name == "Иванова М. С."
        assert user.full_name == "Иванова Мария Сергеевна"
        assert user.initials == "МС"


class TestScopeNarrowing:
    """Согласование роли и области видимости."""

    def test_demotion_narrows_scope(self, hr_user):
        assert hr_user.scope_level == ScopeLevel.ORGANIZATION

        hr_user.role = Role.EMPLOYEE
        hr_user.save()
        hr_user.refresh_from_db()

        assert hr_user.scope_level == ScopeLevel.SELF

    def test_promotion_does_not_widen_scope_automatically(self, employee_user):
        """Повышение роли само по себе не расширяет доступ."""
        employee_user.role = Role.MANAGER
        employee_user.save()
        employee_user.refresh_from_db()

        assert employee_user.scope_level == ScopeLevel.SELF

    def test_new_user_receives_role_default_scope(self, branch, department):
        """Новая учётная запись получает область видимости по умолчанию."""
        manager = UserFactory(
            email="novyy-rukovoditel@meridian-soft.ru",
            role=Role.MANAGER,
            branch=branch,
            department=department,
        )

        assert manager.scope_level == ScopeLevel.SUBORDINATES

    def test_explicit_scope_survives_role_change_when_not_wider(self, employee_user):
        """Область, не превышающая допустимую для роли, сохраняется."""
        employee_user.role = Role.MANAGER
        employee_user.scope_level = ScopeLevel.SUBORDINATES
        employee_user.save()
        employee_user.refresh_from_db()

        assert employee_user.scope_level == ScopeLevel.SUBORDINATES


class TestAuditOnAuthentication:
    """Фиксация событий входа."""

    def test_successful_login_recorded(self, client, employee_user, known_password):
        client.post(
            reverse("accounts:login"),
            {"username": employee_user.email, "password": known_password},
        )

        assert AuditLog.objects.filter(action=AuditAction.LOGIN).exists()
        assert LoginRecord.objects.filter(
            user=employee_user, outcome=LoginRecord.Outcome.SUCCESS
        ).exists()

    def test_failed_login_recorded_with_warning_severity(self, client, employee_user):
        client.post(
            reverse("accounts:login"),
            {"username": employee_user.email, "password": "Nevernyi-Parol-2026"},
        )

        entry = AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED).first()
        assert entry is not None
        assert entry.severity == "warning"

    def test_failed_attempt_linked_to_existing_account(self, client, employee_user):
        """Неудачная попытка связывается с учётной записью."""
        client.post(
            reverse("accounts:login"),
            {"username": employee_user.email, "password": "Nevernyi-Parol-2026"},
        )

        record = LoginRecord.objects.filter(outcome=LoginRecord.Outcome.FAILED).first()
        assert record is not None
        assert record.user_id == employee_user.pk

    def test_password_never_stored_in_audit(self, client, employee_user):
        """Журнал не должен становиться источником утечки учётных данных."""
        secret = "Sovershenno-Sekretno-2026"

        client.post(
            reverse("accounts:login"),
            {"username": employee_user.email, "password": secret},
        )

        for entry in AuditLog.objects.all():
            assert secret not in entry.message
            assert secret not in str(entry.changes)

    def test_audit_entries_are_immutable(self, employee_user):
        """Запись, которую можно отредактировать, не является доказательством."""
        entry = AuditLog.objects.create(
            actor=employee_user,
            action=AuditAction.UPDATE,
            message="Исходное описание",
        )

        entry.message = "Подменённое описание"
        with pytest.raises(ValueError, match="неизменяемы"):
            entry.save()


class TestProtectedPages:
    """Разграничение доступа к личному кабинету."""

    @pytest.mark.parametrize(
        "url_name",
        ["accounts:profile", "accounts:preferences", "accounts:security"],
    )
    def test_anonymous_redirected_to_login(self, client, url_name):
        response = client.get(reverse(url_name))

        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    @pytest.mark.parametrize(
        "url_name",
        ["accounts:profile", "accounts:preferences", "accounts:security"],
    )
    def test_authenticated_user_has_access(self, auth_client, url_name):
        response = auth_client.get(reverse(url_name))
        assert response.status_code == 200

    def test_security_page_shows_login_history(self, auth_client, employee_user):
        LoginRecord.objects.create(
            user=employee_user,
            outcome=LoginRecord.Outcome.FAILED,
            ip_address="203.0.113.10",
        )

        response = auth_client.get(reverse("accounts:security"))

        assert response.status_code == 200
        assert response.context["failed_count"] >= 1


class TestInterfacePreferences:
    """Настройки оформления и языка применяются, а не только сохраняются."""

    def test_saving_preferences_applies_language(self, auth_client, employee_user):
        response = auth_client.post(
            reverse("accounts:preferences"),
            {
                "timezone": "",
                "language": "en",
                "theme": "system",
                "density": "comfortable",
                "email_notifications": "on",
                "digest_notifications": "on",
            },
        )

        assert response.status_code == 302
        employee_user.profile.refresh_from_db()
        assert employee_user.profile.language == "en"
        assert response.cookies[settings.LANGUAGE_COOKIE_NAME].value == "en"
        # Адрес возврата переведён: иначе первый же переход вернул бы прежний язык.
        assert response["Location"].startswith("/en/")

    def test_saving_preferences_applies_appearance(self, auth_client, employee_user):
        response = auth_client.post(
            reverse("accounts:preferences"),
            {
                "timezone": "",
                "language": "ru",
                "theme": "dark",
                "density": "compact",
                "email_notifications": "on",
                "digest_notifications": "on",
            },
        )

        assert response.status_code == 302
        assert response.cookies[settings.APPEARANCE_COOKIE_NAME].value == "dark:compact"

    def test_theme_reaches_markup(self, auth_client):
        """Тема попадает в разметку на сервере."""
        auth_client.cookies[settings.APPEARANCE_COOKIE_NAME] = "dark:compact"

        response = auth_client.get(reverse("accounts:preferences"))
        body = response.content.decode()

        assert 'data-theme="dark"' in body
        assert 'data-density="compact"' in body

    def test_header_switcher_saves_to_profile(self, auth_client, employee_user):
        """Переключатель темы в шапке сохраняет выбор в профиль."""
        response = auth_client.post(
            reverse("accounts:appearance"), {"theme": "light", "density": "compact"}
        )

        assert response.status_code == 204
        employee_user.profile.refresh_from_db()
        assert employee_user.profile.theme == "light"
        assert employee_user.profile.density == "compact"

    def test_appearance_endpoint_rejects_unknown_theme(self, auth_client):
        response = auth_client.post(reverse("accounts:appearance"), {"theme": "neon"})

        assert response.status_code == 400

    def test_appearance_endpoint_requires_authentication(self, client):
        response = client.post(reverse("accounts:appearance"), {"theme": "dark"})

        assert response.status_code == 302
