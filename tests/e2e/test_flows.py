"""Сквозные сценарии критического пути."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


class TestNominationFlow:
    """Выдвижение кандидата и решение по нему."""

    def test_manager_submits_nomination(self, page: Page, site: str, login, e2e_data):
        candidate = e2e_data["employees"][1]

        login("manager")
        page.goto(f"{site}/workspace/reserve/nominations/new/")

        page.select_option("#id_employee", label=candidate.short_name)
        page.select_option("#id_target_position", index=1)
        page.fill(
            "#id_justification",
            "Кандидат второй год ведёт проектную группу, закрыл план развития "
            "и заменял руководителя отдела на время отпуска.",
        )
        page.click("main form button[type=submit]")

        expect(page.locator("main")).to_contain_text(candidate.last_name)

    def test_short_justification_is_rejected(self, page: Page, site: str, login, e2e_data):
        """Обоснование короче полутора строк не принимается."""
        candidate = e2e_data["employees"][2]

        login("manager")
        page.goto(f"{site}/workspace/reserve/nominations/new/")

        page.select_option("#id_employee", label=candidate.short_name)
        page.select_option("#id_target_position", index=1)
        page.fill("#id_justification", "Хороший сотрудник")
        page.click("main form button[type=submit]")

        expect(page.locator(".field-errors, .field__errors, .form-errors")).to_be_visible()

    def test_hr_sees_nomination_after_manager_decision(
        self, page: Page, site: str, login, e2e_data
    ):
        """Заявка, согласованная руководителем, появляется в очереди кадровой службы."""
        candidate = e2e_data["employees"][3]

        login("manager")
        page.goto(f"{site}/workspace/reserve/nominations/new/")
        page.select_option("#id_employee", label=candidate.short_name)
        page.select_option("#id_target_position", index=1)
        page.fill(
            "#id_justification",
            "Кандидат руководит группой из четырёх инженеров, прошёл оценку "
            "и закрыл разрывы по управленческим компетенциям.",
        )
        page.click("main form button[type=submit]")

        login("hr")
        page.goto(f"{site}/workspace/reserve/nominations/")

        expect(page.locator("main")).to_contain_text(candidate.last_name)


class TestSelfApplication:
    """Самовыдвижение сотрудника."""

    def test_employee_applies_to_reserve(self, page: Page, site: str, login):
        login("employee")
        page.goto(f"{site}/workspace/reserve/apply/")

        if page.locator("#id_target_position").count() == 0:
            pytest.skip("Сотруднику недоступна ни одна целевая должность")

        page.select_option("#id_target_position", index=1)
        page.fill(
            "#id_justification",
            "Второй год выполняю задачи ведущего инженера, готов к переходу "
            "на руководящую должность и прошёл обучение по управлению людьми.",
        )
        page.click("main form button[type=submit]")

        expect(page.locator("main")).not_to_contain_text("Ошибка сервера")


class TestDevelopmentFlow:
    """Работа с планом развития."""

    def test_employee_opens_own_plan_from_dashboard(self, page: Page, site: str, login):
        """Переход «кабинет → план развития» ведёт на собственный план."""
        login("employee")
        page.click("main a:has-text('открыть план')")

        expect(page).to_have_url(f"{site}/workspace/development/my/")
        expect(page.locator("main")).to_contain_text("Подготовка к роли руководителя отдела")

    def test_activity_completion_requires_evidence(self, page: Page, site: str, login, e2e_data):
        """Обучение закрывается документом, а не кнопкой."""
        login("hr")
        page.goto(f"{site}/workspace/development/activities/")

        link = page.locator("main a:has-text('Курс управления командой')").first
        if link.count() == 0:
            pytest.skip("Мероприятие не показано в общем списке")

        link.click()
        expect(page.locator("main")).to_contain_text("подтвержд", ignore_case=True)


class TestReporting:
    """Каталог отчётов и выгрузка."""

    def test_catalog_lists_all_four_formats(self, page: Page, site: str, login):
        """В каталоге представлены все четыре формата выгрузки."""
        login("hr")
        page.goto(f"{site}/workspace/reports/")

        catalog = page.locator("main").inner_text()
        if "Отчёты не настроены" in catalog:
            pytest.skip("Каталог отчётов не наполнен на этом стенде")

        for label in ("DOCX", "XLSX", "PDF", "CSV"):
            assert label.lower() in catalog.lower(), f"Формат {label} не представлен в каталоге"

    def test_pdf_unavailability_is_announced_on_page(self, page: Page, site: str, login):
        """Недоступность PDF сообщается на самой странице каталога."""
        login("hr")
        page.goto(f"{site}/workspace/reports/")

        alert = page.locator(".alert--warning:has-text('Выгрузка в PDF недоступна')")
        if alert.count() == 0:
            pytest.skip("Шрифт найден: предупреждение не выводится")

        expect(alert).to_contain_text(".docx")


class TestNotifications:
    """Лента уведомлений."""

    def test_unread_filter_is_default(self, page: Page, site: str, login):
        """Лента открывается на непрочитанных."""
        login("hr")
        page.goto(f"{site}/workspace/notifications/")

        state = page.locator("#id_state")
        expect(state).to_be_visible()

        assert state.input_value() == "unread", (
            "Лента открылась не на непрочитанных: умолчание формы не применено"
        )
