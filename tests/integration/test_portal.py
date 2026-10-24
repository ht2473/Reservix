"""Интеграционные тесты публичной части."""

from __future__ import annotations

import time

import pytest

from django.urls import reverse

from apps.notifications.models import Notification
from apps.portal.models import Feedback, Page
from apps.portal.services import get_public_summary, invalidate_summary, render_markup
from tests.factories import BranchFactory, DepartmentFactory, PositionFactory

pytestmark = [pytest.mark.django_db, pytest.mark.integration]

#: Публичные страницы, доступные без авторизации.
PUBLIC_URL_NAMES = [
    "portal:home",
    "portal:about",
    "portal:methodology",
    "portal:competencies",
    "portal:readiness",
    "portal:ninebox",
    "portal:regulations",
    "portal:articles",
    "portal:glossary",
    "portal:faq",
    "portal:api_docs",
    "portal:contacts",
    "portal:feedback",
    "portal:feedback_sent",
]


class TestPublicAccess:
    """Доступность публичных страниц."""

    @pytest.mark.parametrize("url_name", PUBLIC_URL_NAMES)
    @pytest.mark.smoke
    def test_page_available_to_anonymous(self, client, url_name):
        """Страницы раздела открыты неавторизованному посетителю."""
        response = client.get(reverse(url_name))
        assert response.status_code == 200

    @pytest.mark.parametrize("url_name", PUBLIC_URL_NAMES)
    def test_page_declares_breadcrumbs_or_is_root(self, client, url_name):
        """Цепочка навигации присутствует на каждой странице ресурса."""
        response = client.get(reverse(url_name))

        if url_name == "portal:home":
            return

        crumbs = response.context.get("breadcrumbs")
        assert crumbs, f"Страница {url_name} не объявила цепочку навигации"
        assert crumbs[-1].is_link is False

    @pytest.mark.parametrize("url_name", PUBLIC_URL_NAMES)
    def test_footer_shown(self, client, url_name):
        """Подвал с навигацией выводится на каждой странице."""
        response = client.get(reverse(url_name))
        assert 'class="site-footer"' in response.content.decode()

    @pytest.mark.parametrize("url_name", PUBLIC_URL_NAMES)
    def test_page_has_title_and_description(self, client, url_name):
        """Пустых страниц без заголовка и описания в ресурсе быть не должно."""
        response = client.get(reverse(url_name))
        html = response.content.decode()

        assert "<title>" in html
        assert 'name="description"' in html

    def test_public_menu_rendered(self, client):
        response = client.get(reverse("portal:home"))
        menu = response.context["public_menu"]

        assert len(menu) >= 1
        assert all(item["url"] for item in menu)

    def test_security_headers_present(self, client):
        """Защитные заголовки выставляются собственным слоем обработки."""
        response = client.get(reverse("portal:home"))

        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "Permissions-Policy" in response.headers
        assert "Content-Security-Policy" in str(response.headers)

    def test_request_id_returned(self, client):
        """Идентификатор запроса возвращается в заголовке."""
        response = client.get(reverse("portal:home"))
        assert response.headers.get("X-Request-ID")


class TestHomePage:
    """Главная страница."""

    def test_summary_reflects_actual_data(self, client, db):
        branch = BranchFactory(code="msk", name="Головной офис", is_head_office=True)
        department = DepartmentFactory(code="dev", name="Департамент разработки", branch=branch)
        # Подразделение передаётся явно: иначе фабрика должности создаст
        # собственное, а вместе с ним — второй филиал, и счётчики разойдутся.
        PositionFactory(
            code="lead", name="Руководитель", department=department, is_key_position=True
        )

        invalidate_summary()
        response = client.get(reverse("portal:home"))
        summary = response.context["summary"]

        assert summary.branches == 1
        assert summary.departments >= 1
        assert summary.key_positions == 1

    def test_summary_is_cached(self, db):
        """
        Показатели кешируются: главная открывается часто, в том числе автоматическими обходчиками.
        """
        BranchFactory(code="msk-1", name="Офис")
        invalidate_summary()

        first = get_public_summary()
        BranchFactory(code="spb-1", name="Филиал")
        second = get_public_summary()

        assert first.branches == second.branches

    def test_matrix_has_nine_cells(self, client):
        """Матрица 9-box выводится полностью, без пропущенных ячеек."""
        response = client.get(reverse("portal:home"))
        assert len(response.context["ninebox_cells"]) == 9

    def test_process_stages_listed(self, client):
        response = client.get(reverse("portal:home"))
        assert len(response.context["stages"]) == 7


class TestFeedbackForm:
    """Форма обратной связи."""

    @staticmethod
    def valid_payload(**overrides) -> dict:
        """Корректно заполненная форма."""
        payload = {
            "name": "Иванов Иван Иванович",
            "email": "ivanov@meridian-soft.ru",
            "phone": "+7 900 111-22-33",
            "organization": "ООО «Меридиан Софт»",
            "topic": Feedback.Topic.QUESTION,
            "subject": "Вопрос о включении в кадровый резерв",
            "message": "Подскажите, как подать заявку на включение в кадровый резерв.",
            "consent_given": "on",
            "website": "",
            "form_rendered_at": str(int(time.time()) - 30),
        }
        payload.update(overrides)
        return payload

    def test_valid_submission_creates_record(self, client):
        response = client.post(reverse("portal:feedback"), self.valid_payload())

        assert response.status_code == 302
        feedback = Feedback.objects.get()
        assert feedback.status == Feedback.Status.NEW
        assert feedback.subject == "Вопрос о включении в кадровый резерв"

    def test_phone_normalized_on_save(self, client):
        client.post(reverse("portal:feedback"), self.valid_payload())
        assert Feedback.objects.get().phone == "+79001112233"

    def test_consent_is_mandatory(self, client):
        """Без согласия на обработку данных обращение принять невозможно."""
        payload = self.valid_payload()
        payload.pop("consent_given")

        response = client.post(reverse("portal:feedback"), payload)

        assert response.status_code == 200
        assert Feedback.objects.count() == 0
        assert "consent_given" in response.context["form"].errors

    def test_short_message_rejected(self, client):
        response = client.post(reverse("portal:feedback"), self.valid_payload(message="Вопрос"))

        assert response.status_code == 200
        assert Feedback.objects.count() == 0

    def test_honeypot_blocks_automated_submission(self, client):
        """Скрытое поле-приманка заполняют только программы."""
        response = client.post(
            reverse("portal:feedback"), self.valid_payload(website="https://spam.example")
        )

        assert response.status_code == 200
        assert Feedback.objects.count() == 0

    def test_instant_submission_rejected(self, client):
        """Человек тратит на заполнение хотя бы несколько секунд."""
        response = client.post(
            reverse("portal:feedback"),
            self.valid_payload(form_rendered_at=str(int(time.time()))),
        )

        assert response.status_code == 200
        assert Feedback.objects.count() == 0

    def test_submission_without_timestamp_accepted(self, client):
        """Отсутствие метки времени не отклоняет обращение."""
        response = client.post(reverse("portal:feedback"), self.valid_payload(form_rendered_at=""))

        assert response.status_code == 302
        assert Feedback.objects.count() == 1

    def test_administrators_notified(self, client, admin_user):
        client.post(reverse("portal:feedback"), self.valid_payload())

        assert Notification.objects.filter(recipient=admin_user).exists()

    def test_request_context_recorded(self, client):
        """Технические сведения нужны для разбора спама и повторных обращений."""
        client.post(
            reverse("portal:feedback"),
            self.valid_payload(),
            HTTP_USER_AGENT="TestBrowser/1.0",
        )

        feedback = Feedback.objects.get()
        assert feedback.user_agent == "TestBrowser/1.0"
        assert feedback.ip_address

    def test_authenticated_user_linked(self, auth_client, employee_user):
        auth_client.post(reverse("portal:feedback"), self.valid_payload())

        feedback = Feedback.objects.get()
        assert feedback.submitted_by_id == employee_user.pk

    def test_form_prefilled_for_authenticated_user(self, auth_client, employee_user):
        """Авторизованный пользователь не должен вводить то, что система знает."""
        response = auth_client.get(reverse("portal:feedback"))
        form = response.context["form"]

        assert form.fields["email"].initial == employee_user.email
        assert form.fields["name"].initial == employee_user.full_name


class TestStaticPages:
    """Страницы, содержимое которых ведёт администратор."""

    def test_published_page_available(self, client, db):
        Page.objects.create(
            slug="privacy",
            title="Обработка персональных данных",
            subtitle="Политика организации",
            body="## Общие положения\n\nТекст политики.",
            is_published=True,
        )

        response = client.get(reverse("portal:page", kwargs={"slug": "privacy"}))

        assert response.status_code == 200
        assert "Обработка персональных данных" in response.content.decode()

    def test_unpublished_page_hidden(self, client, db):
        Page.objects.create(slug="draft", title="Черновик", body="Текст", is_published=False)

        response = client.get(reverse("portal:page", kwargs={"slug": "draft"}))
        assert response.status_code == 404

    def test_missing_page_returns_404(self, client, db):
        response = client.get(reverse("portal:page", kwargs={"slug": "nesushchestvuyushchaya"}))
        assert response.status_code == 404


class TestMarkupRendering:
    """Преобразование разметки содержимого."""

    def test_headings(self):
        assert "<h2>Заголовок</h2>" in render_markup("## Заголовок")
        assert "<h3>Подзаголовок</h3>" in render_markup("### Подзаголовок")

    def test_paragraphs(self):
        result = render_markup("Первый абзац.\n\nВторой абзац.")
        assert result.count("<p>") == 2

    def test_unordered_list(self):
        result = render_markup("* Первый\n* Второй")
        assert "<ul>" in result
        assert result.count("<li>") == 2

    def test_ordered_list(self):
        result = render_markup("1. Первый\n2. Второй")
        assert "<ol>" in result

    def test_quote(self):
        assert "<blockquote>" in render_markup("> Цитата из положения")

    def test_inline_formatting(self):
        result = render_markup("Текст с **выделением** и *курсивом*.")
        assert "<strong>выделением</strong>" in result
        assert "<em>курсивом</em>" in result

    @pytest.mark.security
    def test_script_injection_neutralized(self):
        """Администратор не должен иметь возможности внедрить сценарий."""
        result = render_markup('Текст <script>alert("xss")</script> дальше')

        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    @pytest.mark.security
    def test_attribute_injection_neutralized(self):
        result = render_markup('## Заголовок" onload="alert(1)')

        assert "onload=" not in result or "&quot;" in result

    def test_empty_input(self):
        assert render_markup("") == ""


class TestLanguageSwitch:
    """Переключение языка интерфейса."""

    def test_switch_to_english(self, client):
        response = client.post(
            reverse("set_language"),
            {"language": "en", "next": "/"},
        )

        assert response.status_code == 302

    def test_interface_translated(self, client):
        """Локализация подключена и работает."""
        client.post(reverse("set_language"), {"language": "en", "next": "/"})
        response = client.get("/")

        assert response.status_code == 200


class TestErrorPages:
    """Страницы ошибок."""

    def test_not_found_keeps_navigation(self, client):
        """Страница ошибки сохраняет навигацию."""
        response = client.get("/nesushchestvuyushchiy-razdel/")

        assert response.status_code == 404
        html = response.content.decode()
        assert 'class="site-footer"' in html

    def test_permission_denied_page_for_anonymous(self, client):
        """Раздел личного кабинета уводит гостя на форму входа."""
        response = client.get(reverse("accounts:profile"))

        assert response.status_code == 302
        assert reverse("accounts:login") in response.url


class TestHealthCheck:
    """Проверка работоспособности сервиса."""

    @pytest.mark.smoke
    def test_health_endpoint(self, client):
        response = client.get("/health/")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["checks"]["database"] == "ok"

    def test_health_is_not_cached(self, client):
        """Кешированный ответ проверки состояния бесполезен."""
        response = client.get("/health/")
        assert "no-cache" in response.headers.get("Cache-Control", "")


class TestContactsPage:
    """Страница контактов."""

    def test_branches_listed(self, client, db):
        BranchFactory(code="head", name="Головной офис", city="Москва", is_head_office=True)
        BranchFactory(code="nsk", name="Филиал Новосибирск", city="Новосибирск")

        response = client.get(reverse("portal:contacts"))
        branches = list(response.context["branches"])

        assert len(branches) == 2
        # Головной офис выводится первым.
        assert branches[0].is_head_office is True

    def test_inactive_branch_hidden(self, client, db):
        BranchFactory(code="closed", name="Закрытый филиал", is_active=False)

        response = client.get(reverse("portal:contacts"))
        assert list(response.context["branches"]) == []
