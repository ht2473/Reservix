"""Проверка адаптивности и доступности интерфейса."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.e2e]

#: Разрешение и его название для сообщения об отказе.
VIEWPORTS = [
    (360, 740, "телефон"),
    (768, 1024, "планшет"),
    (1280, 800, "ноутбук"),
    (1920, 1080, "рабочее место"),
]

#: Страницы, проверяемые на всех разрешениях: главная как самая насыщенная
#: публичная, матрица как самая широкая, глоссарий как самый длинный.
RESPONSIVE_PAGES = ["/", "/nine-box/", "/glossary/", "/readiness/"]


def horizontal_overflow(page: Page) -> int:
    """Возвращает величину выхода содержимого за ширину окна, пиксели."""
    return page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )


class TestResponsiveLayout:
    """Поведение вёрстки на разных разрешениях."""

    @pytest.mark.parametrize(("width", "height", "label"), VIEWPORTS)
    @pytest.mark.parametrize("path", RESPONSIVE_PAGES)
    def test_no_horizontal_scroll(
        self, page: Page, site: str, width: int, height: int, label: str, path: str
    ):
        """Страница не прокручивается вбок ни на одном из разрешений."""
        page.set_viewport_size({"width": width, "height": height})
        page.goto(f"{site}{path}")

        overflow = horizontal_overflow(page)

        assert overflow <= 1, (
            f"Страница {path} на разрешении {width}×{height} ({label}) "
            f"шире окна на {overflow} пикселей"
        )

    def test_menu_collapses_on_narrow_screen(self, page: Page, site: str):
        """На узком экране главное меню сворачивается в панель, а не исчезает."""
        page.set_viewport_size({"width": 360, "height": 740})
        page.goto(site)

        expect(page.locator(".main-nav")).not_to_be_visible()
        expect(page.locator(".nav-toggle")).to_be_visible()

    def test_mobile_menu_opens_and_navigates(self, page: Page, site: str):
        """Свёрнутое меню открывается и ведёт в раздел."""
        page.set_viewport_size({"width": 360, "height": 740})
        page.goto(site)

        page.click(".nav-toggle")
        page.click(".mobile-nav a:has-text('Глоссарий')")

        expect(page).to_have_url(f"{site}/glossary/")

    def test_wide_table_scrolls_inside_its_area(self, page: Page, site: str, login):
        """Широкая таблица прокручивается внутри своей области."""
        page.set_viewport_size({"width": 360, "height": 740})
        login("hr")
        page.goto(f"{site}/workspace/people/")

        assert horizontal_overflow(page) <= 1
        wrap = page.locator(".table-wrap").first
        expect(wrap).to_be_visible()

    def test_body_text_stays_readable(self, page: Page, site: str):
        """Размер основного текста не опускается ниже читаемого."""
        page.set_viewport_size({"width": 360, "height": 740})
        page.goto(f"{site}/methodology/")

        size = page.evaluate(
            "() => parseFloat(getComputedStyle(document.querySelector('main')).fontSize)"
        )

        assert size >= 14, f"Размер основного текста {size} пикселей — ниже читаемого"


class TestAccessibility:
    """Проверки доступности, выполнимые без внешних библиотек."""

    def test_page_declares_language(self, page: Page, site: str):
        page.goto(site)

        expect(page.locator("html")).to_have_attribute("lang", "ru")

    def test_single_first_level_heading(self, page: Page, site: str):
        """На странице ровно один заголовок первого уровня."""
        page.goto(f"{site}/methodology/")

        assert page.locator("h1").count() == 1

    def test_images_have_alternative_text(self, page: Page, site: str):
        """У каждого изображения есть текстовая замена — пусть и пустая для декоративных."""
        page.goto(site)

        missing = page.evaluate(
            "() => Array.from(document.images)"
            ".filter(img => !img.hasAttribute('alt'))"
            ".map(img => img.currentSrc || img.src)"
        )

        assert not missing, f"Изображения без атрибута alt: {missing}"

    def test_interactive_controls_are_labelled(self, page: Page, site: str):
        """У каждой кнопки есть доступное имя."""
        page.goto(site)

        unlabelled = page.evaluate(
            "() => Array.from(document.querySelectorAll('button'))"
            ".filter(node => !(node.innerText || '').trim()"
            " && !node.getAttribute('aria-label')"
            " && !node.getAttribute('title'))"
            ".map(node => node.outerHTML.slice(0, 80))"
        )

        assert not unlabelled, f"Кнопки без доступного имени: {unlabelled}"

    def test_focus_is_visible_on_keyboard_navigation(self, page: Page, site: str):
        """Фокус клавиатуры виден."""
        page.goto(site)
        page.keyboard.press("Tab")
        page.keyboard.press("Tab")

        outline = page.evaluate(
            "() => {"
            " const node = document.activeElement;"
            " const style = getComputedStyle(node);"
            " return style.outlineStyle + '|' + style.outlineWidth + '|' + style.boxShadow;"
            "}"
        )

        assert outline.split("|")[0] != "none" or outline.split("|")[2] != "none", (
            "У элемента в фокусе не видно ни обводки, ни тени"
        )


class TestDarkTheme:
    """Тёмное оформление."""

    def test_dark_theme_applies_to_workspace(self, page: Page, site: str, login):
        """Выбранная тема действует и в рабочем месте, а не только на витрине."""
        login("hr")
        page.click("[data-dropdown-trigger='theme']")
        page.click("[data-theme-option='dark']")

        expect(page.locator("html")).to_have_attribute("data-theme", "dark")

        page.goto(f"{site}/workspace/people/")
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")

    def test_dark_theme_changes_background(self, page: Page, site: str):
        """Тёмная тема меняет фон страницы, а не только значение атрибута."""
        page.goto(site)
        light = page.evaluate("() => getComputedStyle(document.body).backgroundColor")

        page.click("[data-dropdown-trigger='theme']")
        page.click("[data-theme-option='dark']")
        dark = page.evaluate("() => getComputedStyle(document.body).backgroundColor")

        assert light != dark
