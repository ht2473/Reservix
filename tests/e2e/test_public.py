"""Сквозные сценарии публичной части."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.e2e]

#: Пауза перед отправкой формы обратной связи, миллисекунды.
MIN_FILL_MS = 3500

#: Публичные страницы: адрес и фрагмент заголовка, по которому страница опознаётся.
PUBLIC_PAGES = [
    ("/", "кадров"),
    ("/about/", "О системе"),
    ("/methodology/", "Методология"),
    ("/competencies/", "Компетенции"),
    ("/readiness/", "готовности"),
    ("/nine-box/", "9-box"),
    ("/regulations/", "формы"),
    ("/knowledge/", "База знаний"),
    ("/glossary/", "Глоссарий"),
    ("/faq/", "Справка"),
    ("/api/", "интерфейс"),
    ("/contacts/", "Контакты"),
    ("/feedback/", "связь"),
]


class TestPublicPages:
    """Доступность и целостность публичных страниц."""

    @pytest.mark.parametrize(("path", "marker"), PUBLIC_PAGES)
    def test_page_opens_for_anonymous(self, page: Page, site: str, path: str, marker: str):
        """Страница открывается без авторизации и содержит ожидаемое содержание."""
        response = page.goto(f"{site}{path}")

        assert response is not None
        assert response.status == 200
        expect(page.locator("main#main-content")).to_contain_text(marker, ignore_case=True)

    def test_every_page_carries_breadcrumbs_and_footer(self, page: Page, site: str):
        """
        Цепочка навигации и подвал присутствуют на отрисованной странице, а не только в шаблоне.
        """
        page.goto(f"{site}/methodology/")

        expect(page.locator("nav.breadcrumbs")).to_be_visible()
        expect(page.locator(".site-footer")).to_contain_text("Reservix")

    def test_navigation_leads_where_it_says(self, page: Page, site: str):
        """Переход по пункту меню открывает соответствующий раздел."""
        page.goto(site)

        item = page.locator(".main-nav__link:has-text('Глоссарий')")
        if not item.is_visible():
            page.click("[data-nav-more] [data-dropdown-trigger]")
            item = page.locator("[data-dropdown-menu='nav-more'] >> text=Глоссарий")

        item.click()

        expect(page).to_have_url(f"{site}/glossary/")
        expect(page.locator("h1")).to_contain_text("Глоссарий")

    def test_skip_link_reaches_content(self, page: Page, site: str):
        """Ссылка «Перейти к содержимому» работает с клавиатуры."""
        page.goto(site)
        page.keyboard.press("Tab")

        skip = page.locator(".skip-link")
        expect(skip).to_be_focused()

        page.keyboard.press("Enter")
        expect(page.locator("#main-content")).to_be_focused()


class TestReadinessCalculator:
    """Интерактивный калькулятор индекса готовности."""

    def test_calculator_renders_criteria_from_scheme(self, page: Page, site: str):
        page.goto(f"{site}/readiness/")

        calculator = page.locator("[data-readiness-calculator]")
        expect(calculator).to_be_visible()
        assert calculator.locator("input[type=range]").count() >= 5

    def test_moving_slider_changes_result(self, page: Page, site: str):
        """Значение индекса пересчитывается при движении движка."""
        page.goto(f"{site}/readiness/")

        value = page.locator("[data-calculator-value]")
        before = value.inner_text()

        first = page.locator("[data-calculator-fields] input[type=range]").first
        first.fill("100")
        first.dispatch_event("input")

        expect(value).not_to_have_text(before)

    def test_result_uses_russian_decimal_separator(self, page: Page, site: str):
        """Итог выводится по русскому образцу: запятая, а не точка."""
        page.goto(f"{site}/readiness/")

        value = page.locator("[data-calculator-value]").inner_text()

        assert "," in value
        assert "." not in value


class TestGlossary:
    """Глоссарий и его указатель."""

    def test_terms_are_listed(self, page: Page, site: str):
        page.goto(f"{site}/glossary/")

        expect(page.locator("main")).to_contain_text("Кадровый резерв")

    def test_index_navigates_to_letter(self, page: Page, site: str):
        """Переход по букве указателя прокручивает к соответствующей группе."""
        page.goto(f"{site}/glossary/")

        letter = page.locator("[data-glossary-index] a").first
        if letter.count() == 0:  # pragma: no cover — указатель пуст на пустом словаре
            pytest.skip("Указатель глоссария пуст")

        letter.click()

        assert "#letter-" in page.url


class TestKnowledgeBase:
    """База знаний."""

    def test_article_opens_from_list(self, page: Page, site: str):
        """Статья открывается переходом из списка, а не по известному адресу."""
        page.goto(f"{site}/knowledge/")
        page.click("main a:has-text('Как формируется кадровый резерв')")

        expect(page.locator("h1")).to_contain_text("Как формируется кадровый резерв")
        expect(page.locator("nav.breadcrumbs")).to_contain_text("База знаний")


class TestNineBox:
    """Обезличенная матрица «результативность — потенциал»."""

    def test_grid_has_nine_cells(self, page: Page, site: str):
        page.goto(f"{site}/nine-box/")

        assert page.locator(".ninebox__cell").count() == 9

    def test_no_personal_data_exposed(self, page: Page, site: str):
        """Публичная матрица не раскрывает связку «человек — зона»."""
        page.goto(f"{site}/nine-box/")
        body = page.locator("body").inner_text()

        for surname in ("Абрамов", "Белов", "Гусев", "Дроздов", "Ершов"):
            assert surname not in body


class TestFeedbackForm:
    """Форма обратной связи — единственная форма публичной части."""

    def test_submits_and_confirms(self, page: Page, site: str):
        """Обращение принимается и подтверждается отдельной страницей."""
        page.goto(f"{site}/feedback/")

        page.fill("#id_name", "Проверкин Пётр Петрович")
        page.fill("#id_email", "proverkin@example.org")
        page.fill("#id_subject", "Вопрос по методике расчёта индекса")
        page.fill(
            "#id_message",
            "Подскажите, как пересчитывается индекс готовности после смены схемы весов.",
        )
        page.check("#id_consent_given")

        page.wait_for_timeout(MIN_FILL_MS)
        page.click("form.card button[type=submit]")

        expect(page).to_have_url(f"{site}/feedback/sent/")

    def test_missing_consent_is_rejected(self, page: Page, site: str):
        """Без согласия на обработку данных обращение не принимается."""
        page.goto(f"{site}/feedback/")

        page.fill("#id_name", "Проверкин Пётр Петрович")
        page.fill("#id_email", "proverkin@example.org")
        page.fill("#id_subject", "Обращение без согласия")
        page.fill("#id_message", "Текст обращения достаточной длины для прохождения проверки.")

        page.wait_for_timeout(MIN_FILL_MS)
        page.click("form.card button[type=submit]")

        expect(page).to_have_url(f"{site}/feedback/")
        expect(page.locator("form.card")).to_contain_text("согласи", ignore_case=True)


class TestThemeAndLanguage:
    """Переключатели оформления и языка."""

    def test_theme_switch_persists_across_pages(self, page: Page, site: str):
        """Выбранная тема сохраняется при переходе на другую страницу."""
        page.goto(site)
        # Выбор темы спрятан в выпадающем меню: сценарий открывает его так же,
        # как пользователь, а не обращается к скрытой кнопке напрямую.
        page.click("[data-dropdown-trigger='theme']")
        page.click("[data-theme-option='dark']")

        expect(page.locator("html")).to_have_attribute("data-theme", "dark")

        page.goto(f"{site}/about/")
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")

    def test_language_switch_changes_interface(self, page: Page, site: str):
        """Переключение языка меняет язык страницы, а не только её адрес."""
        page.goto(f"{site}/about/")
        page.click(".lang-switch__option[value='en']")

        expect(page.locator("html")).to_have_attribute("lang", "en")
