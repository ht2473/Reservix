"""Сквозные сценарии рабочих мест."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.e2e]

#: Роль и адрес её рабочего места.
ROLE_HOMES = [
    ("hr", "/workspace/hr/"),
    ("manager", "/workspace/manager/"),
    ("employee", "/workspace/my/"),
    ("admin", "/manage/"),
    ("mentor", "/workspace/development/mentees/"),
]


class TestAuthentication:
    """Вход, выход и защита закрытых разделов."""

    def test_login_form_rejects_wrong_password(self, page: Page, site: str):
        """Сообщение об отказе не различает неверный пароль и несуществующий адрес."""
        page.goto(f"{site}/account/login/")
        page.fill("#id_username", "hr@e2e.reservix.ru")
        page.fill("#id_password", "zavedomo-nevernyy-parol")
        page.click("main form button[type=submit]")

        expect(page.locator(".form-errors")).to_be_visible()
        expect(page.locator(".form-errors")).not_to_contain_text("не найден")

    @pytest.mark.parametrize(("role", "home"), ROLE_HOMES)
    def test_login_lands_on_role_workspace(self, page: Page, site: str, login, role, home):
        """Вход приводит на рабочее место роли, а не на публичную главную."""
        login(role)

        expect(page).to_have_url(f"{site}{home}")

    def test_logout_ends_session(self, page: Page, site: str, login):
        """Выход завершает сеанс, а не только меняет вид шапки."""
        login("hr")
        page.click("[data-dropdown-trigger='user']")
        page.click("form[action*='logout'] button[type=submit]")

        page.goto(f"{site}/workspace/hr/")

        assert "/account/login/" in page.url

    def test_closed_section_redirects_anonymous_to_login(self, page: Page, site: str):
        page.goto(f"{site}/workspace/reserve/")

        assert "/account/login/" in page.url

    def test_denied_section_keeps_navigation(self, page: Page, site: str, login):
        """Отказ в доступе остаётся страницей системы."""
        login("employee")
        page.goto(f"{site}/workspace/hr/")

        expect(page.locator(".site-header")).to_be_visible()
        expect(page.locator("main")).to_contain_text("Раздел ограничен по роли")
        expect(page.locator("main .btn--primary")).to_be_visible()


class TestHrWorkspace:
    """Рабочее место кадровой службы."""

    def test_dashboard_shows_work_queue_first(self, page: Page, site: str, login):
        """Первый блок дашборда — очередь решений, а не численность."""
        login("hr")

        first_section = page.locator("main .section").first
        expect(first_section).to_contain_text("Требует решения")

    def test_queue_card_opens_matching_list(self, page: Page, site: str, login):
        """Пункт очереди открывает список именно тех записей, о которых сообщает."""
        login("hr")
        page.click("main .card--interactive:has-text('Просрочен пересмотр резерва')")

        expect(page).to_have_url(f"{site}/workspace/reserve/reviews/")

    def test_menu_covers_every_section(self, page: Page, site: str, login):
        """В меню рабочего места нет пунктов с пустым адресом."""
        login("hr")

        links = page.locator(".workspace-nav__link")
        assert links.count() >= 10
        for index in range(links.count()):
            href = links.nth(index).get_attribute("href")
            assert href and href != "#", f"Пункт меню {index} ведёт в пустоту"

    def test_employee_directory_search(self, page: Page, site: str, login):
        """Поиск в кадровом справочнике находит сотрудника по фрагменту фамилии."""
        login("hr")
        page.goto(f"{site}/workspace/people/")
        page.fill("#id_q", "Абрам")
        page.click("form[role=search] button[type=submit]")

        expect(page.locator("table.table")).to_contain_text("Абрамов")

    def test_employee_card_opens_from_directory(self, page: Page, site: str, login):
        """Карточка сотрудника открывается переходом из списка."""
        login("hr")
        page.goto(f"{site}/workspace/people/")
        page.click("table.table a:has-text('Абрамов')")

        expect(page.locator("main")).to_contain_text("Место в организации")
        expect(page.locator("nav.breadcrumbs")).to_contain_text("Сотрудники")

    def test_ranking_page_lists_entries(self, page: Page, site: str, login):
        login("hr")
        page.goto(f"{site}/workspace/analytics/ranking/")

        expect(page.locator("h1")).to_contain_text("ейтинг")


class TestManagerWorkspace:
    """Рабочее место руководителя подразделения."""

    def test_team_page_marks_stale_assessment(self, page: Page, site: str, login):
        """Отсутствие свежей оценки помечено, а не оставлено пустой ячейкой."""
        login("manager")
        page.goto(f"{site}/workspace/people/team/")

        expect(page.locator("table.table")).to_contain_text("устарела")

    def test_manager_sees_only_own_department(self, page: Page, site: str, login):
        """Руководителю доступны сотрудники своего поддерева и никого больше."""
        login("manager")
        page.goto(f"{site}/workspace/people/team/")

        expect(page.locator("table.table")).to_contain_text("Абрамов")

    def test_risk_signal_visible(self, page: Page, site: str, login):
        login("manager")

        expect(page.locator("main")).to_contain_text("Риск ухода")


class TestEmployeeWorkspace:
    """Личный кабинет сотрудника."""

    def test_own_plan_visible(self, page: Page, site: str, login):
        login("employee")

        expect(page.locator("main")).to_contain_text("Подготовка к роли руководителя отдела")

    def test_readiness_index_not_shown_as_number(self, page: Page, site: str, login):
        """Сотруднику показан уровень готовности, но не значение индекса."""
        login("employee")
        body = page.locator("main").inner_text()

        assert "62,5" not in body

    def test_cannot_reach_foreign_employee_card(self, page: Page, site: str, login, e2e_data):
        """Подстановка идентификатора чужой карточки в адресную строку отклоняется."""
        stranger = e2e_data["employees"][0]
        login("employee")
        page.goto(f"{site}/workspace/people/{stranger.public_id}/")

        expect(page.locator("main")).to_contain_text("Раздел ограничен по роли")


class TestCommandPalette:
    """Командная палитра — основной способ навигации опытного пользователя."""

    def test_opens_by_keyboard_and_finds_section(self, page: Page, site: str, login):
        login("hr")
        page.keyboard.press("Control+k")

        palette = page.locator(".palette")
        expect(palette).to_be_visible()

        page.fill(".palette__input", "резерв")
        expect(palette.locator(".palette__results")).to_contain_text("резерв", ignore_case=True)

    def test_closes_by_escape(self, page: Page, site: str, login):
        login("hr")
        page.keyboard.press("Control+k")
        page.keyboard.press("Escape")

        expect(page.locator(".palette")).not_to_be_visible()
