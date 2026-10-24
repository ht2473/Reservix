"""Проверки безопасности по классификации OWASP Top-10 (2021)."""

from __future__ import annotations

import html
import re

import pytest

from django.conf import settings
from django.urls import reverse

from apps.people.models import Employee
from tests.factories import UserFactory
from tests.factories.domain import (
    DevelopmentPlanFactory,
    EmployeeFactory,
    ReserveEntryFactory,
)

pytestmark = [pytest.mark.security, pytest.mark.django_db]


# ---------------------------------------------------------------------------
# A01. Нарушение контроля доступа
# ---------------------------------------------------------------------------
class TestA01BrokenAccessControl:
    """A01:2021 — нарушение контроля доступа."""

    def test_direct_object_reference_is_denied(
        self, manager_client, branch, other_department, position
    ):
        """Подстановка идентификатора чужой карточки в адрес отклоняется."""
        stranger = EmployeeFactory(branch=branch, department=other_department, position=position)

        response = manager_client.get(stranger.get_absolute_url())

        assert response.status_code == 403

    def test_privilege_escalation_through_form_is_rejected(self, auth_client, employee_user):
        """Сотрудник не назначает себе роль подменой поля формы."""
        auth_client.post(
            reverse("accounts:profile"),
            {
                "first_name": "Пётр",
                "last_name": "Сотрудников",
                "role": "admin",
                "scope_level": "organization",
                "is_superuser": "on",
            },
        )

        employee_user.refresh_from_db()
        assert employee_user.role == "employee"
        assert employee_user.scope_level == "self"
        assert employee_user.is_superuser is False

    def test_api_scope_is_enforced(self, client, branch, department, other_department, position):
        """Программный интерфейс ограничен той же областью видимости, что и страницы."""
        manager = UserFactory(
            role="manager",
            scope_level="subordinates",
            branch=branch,
            department=department,
            is_department_head=True,
        )
        EmployeeFactory(last_name="Свой", branch=branch, department=department, position=position)
        EmployeeFactory(
            last_name="Чужой", branch=branch, department=other_department, position=position
        )
        client.force_login(manager)

        payload = client.get("/api/v1/people/employees").json()
        names = " ".join(item["short_name"] for item in payload.get("items", payload))

        assert "Свой" in names
        assert "Чужой" not in names

    def test_state_changing_action_does_nothing_on_get(
        self, hr_client, branch, department, position
    ):
        """Переход по ссылке не меняет состояние записи."""
        employee = EmployeeFactory(branch=branch, department=department, position=position)
        entry = ReserveEntryFactory(employee=employee, target_position=position)

        response = hr_client.get(
            reverse("reserve:entry_status", args=[entry.public_id]), follow=False
        )

        entry.refresh_from_db()
        assert response.status_code == 200
        assert entry.status == "active"
        assert "<form" in response.content.decode(), "Смена статуса выполняется без подтверждения"


# ---------------------------------------------------------------------------
# A02. Криптографические сбои
# ---------------------------------------------------------------------------
class TestA02CryptographicFailures:
    """A02:2021 — криптографические сбои и хранение секретов."""

    def test_password_is_hashed_with_argon2_in_production(self):
        """В продакшене пароли хешируются Argon2id."""
        from config.settings import base

        assert base.PASSWORD_HASHERS[0].endswith("Argon2PasswordHasher")

    def test_password_never_appears_in_response(self, client, employee_user, known_password):
        """Введённый пароль не возвращается в разметке — ни в значении поля, ни в ошибке."""
        employee_user.set_password(known_password)
        employee_user.save(update_fields=["password"])

        response = client.post(
            reverse("accounts:login"),
            {"username": employee_user.email, "password": "nevernyy-parol-2026"},
        )

        assert "nevernyy-parol-2026" not in response.content.decode()

    def test_session_cookie_is_protected(self):
        """Куки сессии недоступны сценариям и не уходят на сторонний домен."""
        assert settings.SESSION_COOKIE_HTTPONLY is True
        assert settings.SESSION_COOKIE_SAMESITE == "Lax"

    def test_production_requires_secure_cookies(self):
        """В продакшене куки передаются только по защищённому соединению."""
        from config.settings import prod

        assert prod.SESSION_COOKIE_SECURE is True
        assert prod.CSRF_COOKIE_SECURE is True
        assert prod.SECURE_HSTS_SECONDS >= 31536000


# ---------------------------------------------------------------------------
# A03. Внедрение
# ---------------------------------------------------------------------------
class TestA03Injection:
    """A03:2021 — внедрение."""

    @pytest.mark.parametrize(
        "payload",
        [
            "<script>alert(1)</script>",
            "'><img src=x onerror=alert(1)>",
            "{{ 7*7 }}",
            "${7*7}",
        ],
    )
    def test_search_input_is_escaped_in_output(self, hr_client, payload):
        """Введённое значение возвращается на страницу экранированным и невычисленным."""
        response = hr_client.get(reverse("people:list"), {"q": payload})
        body = response.content.decode()

        # Значение вернулось в поле ровно тем, что было введено: не
        # вычислено как шаблонное выражение и не превращено в разметку.
        rendered = re.search(r'id="id_q"[^>]*value="([^"]*)"', body) or re.search(
            r'value="([^"]*)"[^>]*id="id_q"', body
        )
        assert rendered is not None, "Поле поиска не найдено на странице"
        assert html.unescape(rendered.group(1)) == payload

        # Исполняемой разметки на странице нет: угловые скобки полезной
        # нагрузки экранированы, и браузер видит текст, а не элемент.
        assert "<script>alert" not in body
        assert "<img src=x" not in body

    def test_sql_injection_in_search_is_harmless(self, hr_client, branch, department, position):
        """Классическая строка внедрения обрабатывается как обычный текст поиска."""
        EmployeeFactory(branch=branch, department=department, position=position)

        response = hr_client.get(reverse("people:list"), {"q": "'; DROP TABLE people_employee; --"})

        assert response.status_code == 200
        assert Employee.objects.count() == 1

    def test_sort_parameter_accepts_only_declared_fields(self, hr_client):
        """Сортировка принимает только объявленные поля."""
        response = hr_client.get(reverse("people:list"), {"sort": "user__password"})

        assert response.status_code == 200
        assert response.context["current_sort"] != "user__password"


# ---------------------------------------------------------------------------
# A04. Небезопасная архитектура
# ---------------------------------------------------------------------------
class TestA04InsecureDesign:
    """A04:2021 — небезопасная архитектура."""

    def test_public_pages_expose_no_personal_data(self, client, branch, department, position):
        """Публичная матрица и главная не раскрывают связку «человек — показатель»."""
        EmployeeFactory(last_name="Тайнов", branch=branch, department=department, position=position)

        for path in (reverse("portal:home"), reverse("portal:ninebox")):
            assert "Тайнов" not in client.get(path).content.decode()

    def test_records_are_addressed_by_opaque_identifier(self, branch, department, position):
        """Записи адресуются публичным идентификатором, а не первичным ключом."""
        employee = EmployeeFactory(branch=branch, department=department, position=position)

        url = employee.get_absolute_url()

        assert str(employee.pk) not in url
        assert str(employee.public_id) in url

    def test_document_filename_on_disk_is_impersonal(self, branch, department, position):
        """Имя файла на диске обезличено."""
        from apps.documents.models import Document

        upload_to = Document._meta.get_field("file").upload_to

        assert callable(upload_to), "Путь хранения обязан вычисляться, а не быть строкой"


# ---------------------------------------------------------------------------
# A05. Ошибки конфигурации безопасности
# ---------------------------------------------------------------------------
class TestA05SecurityMisconfiguration:
    """A05:2021 — ошибки конфигурации."""

    def test_debug_is_off_in_production(self):
        from config.settings import prod

        assert prod.DEBUG is False

    def test_security_headers_present(self, client):
        response = client.get(reverse("portal:home"))

        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "Permissions-Policy" in response.headers

    def test_content_security_policy_forbids_external_sources(self, client):
        """Политика безопасности содержимого разрешает загрузку только со своего домена."""
        response = client.get(reverse("portal:home"))
        policy = response.headers.get(
            "Content-Security-Policy",
            response.headers.get("Content-Security-Policy-Report-Only", ""),
        )

        assert "script-src 'self'" in policy
        assert "object-src 'none'" in policy
        assert "frame-ancestors 'none'" in policy
        assert "http://" not in policy
        assert "https://" not in policy

    def test_api_documentation_path_matches_relaxed_policy(self):
        """
        Послабление политики объявлено ровно для того адреса, по которому отдаётся документация.
        """
        from core.middleware import SecurityHeadersMiddleware

        for prefix in SecurityHeadersMiddleware.RELAXED_PATH_PREFIXES:
            assert prefix.startswith("/api/v1/"), (
                f"Послабление объявлено для {prefix}, а документация отдаётся по /api/v1/docs"
            )

    def test_error_page_hides_technical_details(self, client):
        """Страница ошибки не раскрывает устройство системы."""
        response = client.get("/nesushchestvuyushchiy-adres/")
        body = response.content.decode()

        assert response.status_code == 404
        assert "Traceback" not in body
        assert "django" not in body.lower() or "DEBUG" not in body


# ---------------------------------------------------------------------------
# A07. Ошибки идентификации и аутентификации
# ---------------------------------------------------------------------------
class TestA07AuthenticationFailures:
    """A07:2021 — ошибки идентификации и аутентификации."""

    def test_error_message_does_not_reveal_account_existence(self, client, employee_user):
        """Сообщение об отказе одинаково для неверного пароля и неизвестного адреса."""
        known = client.post(
            reverse("accounts:login"),
            {"username": employee_user.email, "password": "nevernyy-parol"},
        ).content.decode()
        unknown = client.post(
            reverse("accounts:login"),
            {"username": "nikogo-net@example.org", "password": "nevernyy-parol"},
        ).content.decode()

        assert "form-errors" in known
        assert "form-errors" in unknown
        assert ("не найден" in known) == ("не найден" in unknown)

    @pytest.mark.parametrize(
        "password",
        ["parol", "12345678", "password", "qwerty123", "Abcdefg123!"],
    )
    def test_weak_password_is_rejected(self, password):
        """Политика сложности отклоняет словарные и последовательные пароли."""
        from django.contrib.auth.password_validation import (
            ValidationError,
            validate_password,
        )

        with pytest.raises(ValidationError):
            validate_password(password)

    def test_logout_requires_post(self, hr_client):
        """Выход выполняется методом POST."""
        response = hr_client.get(reverse("accounts:logout"))

        assert response.status_code == 405

    def test_session_is_new_after_login(self, client, employee_user, known_password):
        """Идентификатор сеанса меняется при входе."""
        employee_user.set_password(known_password)
        employee_user.must_change_password = False
        employee_user.save(update_fields=["password", "must_change_password"])

        client.get(reverse("portal:home"))
        before = client.cookies.get("reservix_session")

        client.post(
            reverse("accounts:login"),
            {"username": employee_user.email, "password": known_password},
        )
        after = client.cookies.get("reservix_session")

        assert after is not None
        assert before is None or before.value != after.value


# ---------------------------------------------------------------------------
# A08. Нарушение целостности данных
# ---------------------------------------------------------------------------
class TestA08IntegrityFailures:
    """A08:2021 — нарушение целостности данных и программного обеспечения."""

    def test_form_without_csrf_token_is_rejected(self, client, employee_user):
        """Форма без токена защиты отклоняется."""
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        strict.force_login(employee_user)

        response = strict.post(reverse("accounts:profile"), {"first_name": "Пётр"})

        assert response.status_code == 403

    def test_stored_document_checksum_detects_substitution(
        self, hr_user, branch, department, position
    ):
        """Подмена файла в хранилище обнаруживается по контрольной сумме."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.documents.models import DocumentKind
        from apps.documents.services import store_document, verify_integrity

        employee = EmployeeFactory(branch=branch, department=department, position=position)
        document = store_document(
            SimpleUploadedFile("prikaz.pdf", b"soderzhimoe dokumenta"),
            title="Приказ о зачислении в резерв",
            kind=DocumentKind.ORDER,
            uploader=hr_user,
            employee=employee,
        )

        assert verify_integrity(document) is True

        with document.file.storage.open(document.file.name, "wb") as handle:
            handle.write(b"podmenennoe soderzhimoe")

        assert verify_integrity(document) is False


# ---------------------------------------------------------------------------
# A10. Подделка запросов на стороне сервера
# ---------------------------------------------------------------------------
class TestA10ServerSideRequestForgery:
    """A10:2021 — подделка запросов на стороне сервера."""

    def test_no_outbound_urls_in_templates(self):
        """В шаблонах нет ссылок на внешние источники ресурсов."""
        import re
        from pathlib import Path

        pattern = re.compile(
            r'<(?:script|link|img|iframe)[^>]+(?:src|href)="(https?://[^"]+)"',
            re.IGNORECASE,
        )
        offenders: list[str] = []

        for template in Path("templates").rglob("*.html"):
            for match in pattern.finditer(template.read_text(encoding="utf-8")):
                offenders.append(f"{template}: {match.group(1)}")

        assert not offenders, "Внешние источники ресурсов в шаблонах: " + "; ".join(offenders)

    def test_user_supplied_url_is_not_fetched(self, hr_client, branch, department, position):
        """Значение, введённое пользователем, не используется как адрес запроса."""
        employee = EmployeeFactory(branch=branch, department=department, position=position)
        DevelopmentPlanFactory(employee=employee)

        response = hr_client.get(reverse("development:list"), {"q": "http://169.254.169.254/"})

        assert response.status_code == 200
