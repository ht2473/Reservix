"""Проверка раздела отчётности: каталог, запуск, выдача файла."""

from __future__ import annotations

import pytest

from django.urls import reverse

from apps.reporting.datasets import BUILDERS, DatasetError, get_builder
from apps.reporting.forms import ParameterError, ReportRunForm
from apps.reporting.models import (
    GeneratedReport,
    ReportCategory,
    ReportFormat,
    ReportStatus,
    ReportTemplate,
)
from apps.reporting.services import (
    ReportError,
    available_templates,
    build_document,
    generate,
    reports_for,
)
from core.constants import Role
from tests.factories.domain import EmployeeFactory

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def template(db) -> ReportTemplate:
    """Шаблон сводки по подразделению: выгружается в CSV и не требует оценок."""
    return ReportTemplate.objects.create(
        code="unit-summary",
        name="Сводка по подразделению",
        description="Состав подразделения и участие сотрудников в резерве.",
        category=ReportCategory.PERSONNEL,
        output_format=ReportFormat.CSV,
        builder="unit_summary",
        allowed_roles=[Role.ADMIN, Role.HR, Role.MANAGER],
    )


@pytest.fixture
def hr_only_template(db) -> ReportTemplate:
    """Шаблон, доступный только кадровой службе."""
    return ReportTemplate.objects.create(
        code="reserve-health",
        name="Здоровье кадрового резерва",
        description="Сводные показатели состояния резерва.",
        category=ReportCategory.ANALYTICS,
        output_format=ReportFormat.CSV,
        builder="reserve_health",
        allowed_roles=[Role.ADMIN, Role.HR],
    )


# ---------------------------------------------------------------------------
# Каталог
# ---------------------------------------------------------------------------
class TestCatalog:
    """Состав каталога определяется правами на шаблоны."""

    def test_каталог_открывается_кадровой_службе(self, hr_client, template):
        response = hr_client.get(reverse("reporting:index"))
        assert response.status_code == 200
        assert template.name in response.content.decode()

    def test_сотруднику_раздел_недоступен(self, auth_client):
        response = auth_client.get(reverse("reporting:index"))
        assert response.status_code == 403

    def test_руководитель_не_видит_отчёты_кадровой_службы(
        self, manager_user, template, hr_only_template
    ):
        """
        Перечень отчётов расширяется кадровой службой, а не правкой программы, и права обязаны
        расширяться вместе с ним.
        """
        available = {item.code for item in available_templates(manager_user)}
        assert template.code in available
        assert hr_only_template.code not in available

    def test_отключённый_шаблон_исчезает_из_каталога(self, hr_user, template):
        template.is_active = False
        template.save()
        assert available_templates(hr_user).count() == 0

    def test_страница_руководителя_сужена_до_его_разделов(
        self, manager_client, template, hr_only_template
    ):
        response = manager_client.get(reverse("reporting:unit"))
        body = response.content.decode()
        assert response.status_code == 200
        assert template.name in body
        assert hr_only_template.name not in body


# ---------------------------------------------------------------------------
# Форма запуска
# ---------------------------------------------------------------------------
class TestRunForm:
    """Форма строится по описанию параметров шаблона."""

    def test_поля_создаются_по_описанию(self):
        form = ReportRunForm(
            schema={
                "date_from": {"type": "date", "label": "Период с"},
                "readiness": {"type": "choice", "label": "Готовность", "source": "readiness"},
                "only_ready": {"type": "boolean", "label": "Только готовые"},
            }
        )
        assert set(form.fields) == {"date_from", "readiness", "only_ready"}
        assert form.fields["readiness"].choices[0][0] == ""

    def test_все_параметры_необязательны(self):
        """
        Отчёт обязан формироваться и без отбора: иначе сводку по организации целиком получить
        нельзя.
        """
        form = ReportRunForm(data={}, schema={"date_from": {"type": "date"}})
        assert form.is_valid()
        assert form.parameters == {}

    def test_несогласованный_период_отклоняется(self):
        form = ReportRunForm(
            data={"date_from": "2026-05-01", "date_to": "2026-01-01"},
            schema={"date_from": {"type": "date"}, "date_to": {"type": "date"}},
        )
        assert not form.is_valid()
        assert "date_to" in form.errors

    def test_недопустимый_тип_параметра_отвергается(self):
        """
        Описание параметров ведётся через панель администрирования, и ошибка в нём должна
        проявляться внятным отказом, а не сбоем страницы.
        """
        with pytest.raises(ParameterError, match="Недопустимый тип"):
            ReportRunForm(schema={"weird": {"type": "sql"}})

    def test_относительная_начальная_дата_разбирается(self):
        import datetime as dt

        from django.utils import timezone

        form = ReportRunForm(schema={"date_from": {"type": "date", "initial": "year_start"}})
        expected = dt.date(timezone.localdate().year, 1, 1)
        assert form.fields["date_from"].initial == expected


# ---------------------------------------------------------------------------
# Сборщики
# ---------------------------------------------------------------------------
class TestBuilders:
    """Сборщик собирает описание отчёта, не зная о форматах."""

    def test_каждый_шаблон_наполнения_имеет_сборщик(self):
        """
        Ссылка на несуществующий сборщик означала бы отчёт, который невозможно запустить:
        пользователь видит его и получает отказ.
        """
        from apps.reporting.management.commands.seed_reporting import TEMPLATES

        for spec in TEMPLATES:
            assert spec["builder"] in BUILDERS, spec["code"]

    def test_неизвестный_сборщик_отвергается_внятно(self):
        with pytest.raises(DatasetError, match="Сборщик отчёта не найден"):
            get_builder("несуществующий")

    def test_сводка_по_подразделению_содержит_сотрудников(self, hr_user, template, department):
        EmployeeFactory(department=department, last_name="Соколов")
        EmployeeFactory(department=department, last_name="Морозов")

        document = build_document(template, {}, hr_user)
        assert document.title == "Сводка по подразделению"
        assert document.total_rows == 2

        names = {row["employee"] for row in document.tables[0].rows}
        assert any("Соколов" in name for name in names)

    def test_реквизиты_заполнены_всегда(self, hr_user, template):
        """
        Выгрузка без указания, за какой период и по какому отбору она сделана, непригодна ни для
        доклада, ни для приобщения к делу.
        """
        document = build_document(template, {}, hr_user)
        assert document.organization
        assert document.generated_at is not None
        assert document.parameters == [("Отбор", "без ограничений")]

    def test_область_видимости_ограничивает_состав(
        self, manager_user, template, department, other_department
    ):
        """
        Отчёт — такой же способ получить данные, как страница списка, и обходить ограничения он не
        вправе.
        """
        EmployeeFactory(department=department)
        EmployeeFactory(department=other_department)

        document = build_document(template, {}, manager_user)
        assert document.total_rows == 1


# ---------------------------------------------------------------------------
# Формирование и выдача
# ---------------------------------------------------------------------------
class TestGeneration:
    """Сквозной путь от шаблона до файла."""

    def test_отчёт_формируется_и_сохраняется(self, hr_user, template, department):
        EmployeeFactory(department=department)

        report = generate(template, {}, hr_user)
        assert report.status == ReportStatus.READY
        assert report.row_count == 1
        assert report.size_bytes > 0
        assert report.file.name.endswith(".csv")
        assert report.finished_at is not None

    def test_имя_файла_латинское(self, hr_user, template):
        """
        Файл скачивается на рабочие станции с разными настройками и попадает во вложения писем, где
        кириллица в имени приводит к искажениям.
        """
        report = generate(template, {}, hr_user)
        assert report.file.name.isascii()

    def test_недоступный_шаблон_не_запускается(self, employee_user, hr_only_template):
        with pytest.raises(ReportError, match="недоступен"):
            generate(hr_only_template, {}, employee_user)

    def test_отказ_сборщика_записан_в_карточку(self, hr_user, template):
        """
        Запустивший отчёт должен увидеть причину в интерфейсе, а не узнать об отказе по отсутствию
        файла.
        """
        template.builder = "несуществующий"
        template.save()

        with pytest.raises(ReportError):
            generate(template, {}, hr_user)

        report = GeneratedReport.objects.get(template=template)
        assert report.status == ReportStatus.FAILED
        assert "Сборщик отчёта не найден" in report.error_message

    def test_запуск_через_интерфейс_ведёт_к_карточке(self, hr_client, template, department):
        EmployeeFactory(department=department)

        response = hr_client.post(reverse("reporting:template_detail", args=[template.pk]))
        assert response.status_code == 302

        report = GeneratedReport.objects.get()
        assert response.url == report.get_absolute_url()

    def test_файл_выгрузки_отдаётся_автору(self, hr_client, hr_user, template, department):
        EmployeeFactory(department=department)
        report = generate(template, {}, hr_user)

        response = hr_client.get(reverse("reporting:report_download", args=[report.public_id]))
        assert response.status_code == 200
        assert response["Cache-Control"] == "private, no-store"

        content = b"".join(response.streaming_content)
        assert content.startswith(b"\xef\xbb\xbf")

    def test_чужая_выгрузка_недоступна(self, client, employee_user, hr_user, template):
        report = generate(template, {}, hr_user)
        client.force_login(employee_user)

        response = client.get(reverse("reporting:report_download", args=[report.public_id]))
        assert response.status_code in {403, 404}

    def test_история_кадровой_службы_содержит_чужие_запуски(self, hr_user, manager_user, template):
        """
        Сформированный отчёт — документ организации, и вопрос «кто и что выгружал» относится к
        ведению кадровой службы.
        """
        generate(template, {}, manager_user)
        assert reports_for(hr_user).count() == 1
        assert reports_for(manager_user).count() == 1

    def test_параметры_запуска_сохраняются_строками(self, hr_user, template):
        import datetime as dt

        report = generate(template, {"date_from": dt.date(2026, 1, 1)}, hr_user)
        assert report.parameters == {"date_from": "2026-01-01"}


class TestSeedCatalog:
    """Наполнение каталога отчётов."""

    def test_каталог_покрывает_все_форматы(self):
        """
        Писатели форматов различаются существенно, и каталог, где все отчёты выгружаются в .xlsx,
        оставил бы три писателя без проверки на реальных данных.
        """
        from django.core.management import call_command

        call_command("seed_reporting", verbosity=0)

        formats = set(ReportTemplate.objects.values_list("output_format", flat=True))
        assert formats == {
            ReportFormat.DOCX,
            ReportFormat.XLSX,
            ReportFormat.PDF,
            ReportFormat.CSV,
        }

    def test_наполнение_идемпотентно(self):
        from django.core.management import call_command

        call_command("seed_reporting", verbosity=0)
        first = ReportTemplate.objects.count()
        call_command("seed_reporting", verbosity=0)

        assert ReportTemplate.objects.count() == first
