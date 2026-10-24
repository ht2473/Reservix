"""
Нагрузочное тестирование Reservix.

Запуск::

    # Веб-интерфейс Locust: профиль задаётся в браузере
    uv run locust -f tests/load/locustfile.py --host http://127.0.0.1:8000
"""

from __future__ import annotations

import os
import random
import re

from locust import HttpUser, LoadTestShape, between, events, task

# ---------------------------------------------------------------------------
# Учётные данные стенда
# ---------------------------------------------------------------------------
HR_EMAIL = os.environ.get("LOAD_HR_EMAIL", "hr@reservix.local")
MANAGER_EMAIL = os.environ.get("LOAD_MANAGER_EMAIL", "manager@reservix.local")
EMPLOYEE_EMAIL = os.environ.get("LOAD_EMPLOYEE_EMAIL", "employee@reservix.local")
PASSWORD = os.environ.get("LOAD_PASSWORD", "")

#: Порог приемлемого времени ответа, миллисекунды.
LATENCY_BUDGET_MS = 1500

#: Образец для извлечения токена защиты от подделки запроса из разметки формы.
CSRF_PATTERN = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')


class AuthenticatedUser(HttpUser):
    """Общее поведение авторизованного посетителя."""

    abstract = True
    email: str = ""

    def on_start(self) -> None:
        self.logged_in = False
        if not PASSWORD:
            # Без пароля роль работает как анонимная: сценарий не падает,
            # но и не притворяется, будто проверил закрытые разделы.
            return

        with self.client.get("/account/login/", catch_response=True) as response:
            token = CSRF_PATTERN.search(response.text)
            if token is None:
                response.failure("На странице входа нет токена защиты")
                return

        result = self.client.post(
            "/account/login/",
            data={
                "csrfmiddlewaretoken": token.group(1),
                "username": self.email,
                "password": PASSWORD,
            },
            headers={"Referer": f"{self.host}/account/login/"},
            name="/account/login/ [POST]",
        )
        self.logged_in = result.status_code in (200, 302)

    def visit(self, path: str, name: str | None = None) -> None:
        """Открывает страницу и помечает отказ доступа как ошибку сценария."""
        with self.client.get(path, name=name or path, catch_response=True) as response:
            if response.status_code == 403:
                response.failure("Отказ в доступе: вход не выполнен")
            elif response.status_code >= 500:
                response.failure(f"Ошибка сервера: {response.status_code}")


class PortalVisitor(HttpUser):
    """Посетитель публичной части."""

    weight = 12
    wait_time = between(2, 8)

    @task(10)
    def home(self) -> None:
        self.client.get("/")

    @task(5)
    def methodology(self) -> None:
        self.client.get("/methodology/")

    @task(5)
    def readiness(self) -> None:
        self.client.get("/readiness/")

    @task(4)
    def nine_box(self) -> None:
        self.client.get("/nine-box/")

    @task(3)
    def competencies(self) -> None:
        self.client.get("/competencies/")

    @task(3)
    def knowledge(self) -> None:
        self.client.get("/knowledge/")

    @task(2)
    def glossary(self) -> None:
        self.client.get("/glossary/")

    @task(2)
    def faq(self) -> None:
        self.client.get("/faq/")

    @task(1)
    def about(self) -> None:
        self.client.get("/about/")

    @task(1)
    def static_assets(self) -> None:
        """Запрос статики."""
        self.client.get("/static/css/app.css", name="/static/[bundle]")
        self.client.get("/static/js/app.js", name="/static/[bundle]")


class HrSpecialist(AuthenticatedUser):
    """Кадровая служба — самая дорогая роль."""

    weight = 4
    wait_time = between(3, 12)
    email = HR_EMAIL

    @task(6)
    def dashboard(self) -> None:
        self.visit("/workspace/hr/")

    @task(5)
    def reserve_list(self) -> None:
        self.visit("/workspace/reserve/")

    @task(4)
    def people_directory(self) -> None:
        """Справочник с постраничной выдачей: проверяется стоимость страницы, а не первой."""
        page = random.randint(1, 5)
        self.visit(f"/workspace/people/?page={page}", name="/workspace/people/?page=N")

    @task(4)
    def ranking(self) -> None:
        self.visit("/workspace/analytics/ranking/")

    @task(3)
    def nine_box(self) -> None:
        self.visit("/workspace/analytics/nine-box/")

    @task(3)
    def development(self) -> None:
        self.visit("/workspace/development/")

    @task(2)
    def succession(self) -> None:
        self.visit("/workspace/succession/coverage/")

    @task(2)
    def reports(self) -> None:
        self.visit("/workspace/reports/")

    @task(2)
    def search(self) -> None:
        """Поиск по фрагменту фамилии — запрос без индекса по подстроке."""
        term = random.choice(["Ив", "Кузн", "Смир", "ов", "Пет"])
        self.visit(f"/workspace/people/?q={term}", name="/workspace/people/?q=…")


class LineManager(AuthenticatedUser):
    """Руководитель подразделения."""

    weight = 6
    wait_time = between(4, 15)
    email = MANAGER_EMAIL

    @task(6)
    def dashboard(self) -> None:
        self.visit("/workspace/manager/")

    @task(4)
    def team(self) -> None:
        self.visit("/workspace/people/team/")

    @task(3)
    def team_reserve(self) -> None:
        self.visit("/workspace/reserve/team/")

    @task(3)
    def approvals(self) -> None:
        self.visit("/workspace/reserve/approvals/")

    @task(2)
    def assessment_inbox(self) -> None:
        self.visit("/workspace/assessment/inbox/")

    @task(2)
    def gap_matrix(self) -> None:
        self.visit("/workspace/competencies/matrix/")

    @task(1)
    def notifications(self) -> None:
        self.visit("/workspace/notifications/")


class ApiConsumer(AuthenticatedUser):
    """Потребитель программного интерфейса."""

    weight = 2
    wait_time = between(1, 4)
    email = HR_EMAIL

    @task(5)
    def employees(self) -> None:
        self.visit("/api/v1/people/employees?limit=50", name="/api/v1/people/employees")

    @task(4)
    def reserve_entries(self) -> None:
        self.visit("/api/v1/reserve/entries?limit=50", name="/api/v1/reserve/entries")

    @task(3)
    def metrics(self) -> None:
        self.visit("/api/v1/analytics/metrics")

    @task(2)
    def nine_box(self) -> None:
        self.visit("/api/v1/analytics/nine-box")

    @task(2)
    def coverage(self) -> None:
        self.visit("/api/v1/succession/coverage")

    @task(1)
    def health(self) -> None:
        self.client.get("/health/")


class StagedLoad(LoadTestShape):
    """Ступенчатый профиль 50 → 200 → 500 пользователей."""

    #: (секунда окончания ступени, число пользователей, темп появления)
    stages: list[tuple[int, int, int]] = [
        (60, 50, 10),  # разогрев: заполняются кеши и пул соединений
        (180, 50, 10),  # удержание малой нагрузки — эталон времени ответа
        (240, 200, 25),  # подъём до расчётной нагрузки
        (420, 200, 25),  # удержание расчётной нагрузки
        (480, 500, 50),  # подъём до пиковой
        (660, 500, 50),  # удержание пиковой
        (720, 50, 50),  # спад: проверка восстановления
    ]

    def tick(self) -> tuple[int, float] | None:
        elapsed = self.get_run_time()
        for end, users, spawn_rate in self.stages:
            if elapsed < end:
                return users, spawn_rate
        return None


# ---------------------------------------------------------------------------
# Итоговая оценка
# ---------------------------------------------------------------------------
@events.quitting.add_listener
def _assess_results(environment, **_kwargs) -> None:
    """Оценивает прогон по трём условиям и задаёт код возврата."""
    stats = environment.stats.total
    failures = []

    if stats.num_requests and stats.fail_ratio > 0.01:
        failures.append(f"доля отказов {stats.fail_ratio:.1%} превышает 1 %")

    percentile = stats.get_response_time_percentile(0.95) or 0
    if percentile > LATENCY_BUDGET_MS:
        failures.append(f"95-й процентиль {percentile:.0f} мс превышает {LATENCY_BUDGET_MS} мс")

    median = stats.median_response_time or 0
    if median > LATENCY_BUDGET_MS / 3:
        failures.append(f"медиана {median:.0f} мс превышает {LATENCY_BUDGET_MS // 3} мс")

    if failures:
        environment.process_exit_code = 1
        environment.runner.quit_message = "; ".join(failures)
    else:
        environment.process_exit_code = 0
