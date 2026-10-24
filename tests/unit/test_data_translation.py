"""Модульные тесты английских версий справочников и содержимого."""

from __future__ import annotations

import re

import pytest

from django.db.models import Count
from django.utils import translation

from apps.org.models import Department
from apps.people.models import Employee
from core.data_translation import Translator, transliterate
from tests.factories import DepartmentFactory
from tests.factories.domain import EmployeeFactory

pytestmark = pytest.mark.unit


@pytest.fixture
def engine() -> Translator:
    return Translator(
        {
            "Онлайн-курс": "Online course",
            "Анализ данных": "Data analysis",
            "Отдел кадров": "Human Resources",
        }
    )


class TestTransliterate:
    def test_keeps_case_and_punctuation(self):
        assert transliterate("Иванова М. С.") == "Ivanova M. S."

    def test_multiletter_sounds(self):
        assert transliterate("Щукин Юрий") == "Shchukin Yuriy"


class TestTranslator:
    def test_exact_match(self, engine):
        assert engine.translate("Анализ данных") == "Data analysis"

    def test_composite_string_built_from_parts(self, engine):
        assert engine.translate("Онлайн-курс: Анализ данных") == "Online course: Data analysis"

    def test_short_name_transliterated(self, engine):
        assert engine.translate("Кузнецов А. Д.") == "Kuznetsov A. D."

    def test_latin_text_returned_as_is(self, engine):
        assert engine.translate("Tech Lead") == "Tech Lead"

    def test_unknown_text_recorded(self, engine):
        assert engine.translate("Неизвестная строка") is None
        assert ("", "Неизвестная строка") in engine.missing

    def test_multiline_log_translated_line_by_line(self, engine):
        assert engine.translate("Анализ данных\nОтдел кадров") == "Data analysis\nHuman Resources"


class TestInterfaceForms:
    """Системные тексты переводятся по строкам каталога интерфейса."""

    @pytest.fixture
    def engine(self) -> Translator:
        interface = (
            {"Вы включены в кадровый резерв": "You have been added to the talent pool"},
            (
                (
                    re.compile(r"^Требуется оценка: (?P<campaign>.+?)$"),
                    "Assessment required: %(campaign)s",
                ),
            ),
        )
        return Translator({"Годовая оценка 2026": "Annual review 2026"}, interface)

    def test_exact_interface_string(self, engine):
        assert engine.translate("Вы включены в кадровый резерв") == (
            "You have been added to the talent pool"
        )

    def test_parameter_translated_recursively(self, engine):
        assert engine.translate("Требуется оценка: Годовая оценка 2026") == (
            "Assessment required: Annual review 2026"
        )

    def test_untranslatable_parameter_leaves_text_missing(self, engine):
        assert engine.translate("Требуется оценка: Неизвестная кампания") is None


@pytest.mark.django_db
class TestModelFields:
    def test_english_filled_on_save(self):
        department = DepartmentFactory(name="Отдел кадров (HR)")
        department.refresh_from_db()
        assert department.name_en == "Human Resources (HR)"

    def test_english_interface_falls_back_to_russian(self):
        department = DepartmentFactory(name="Подразделение без перевода")
        with translation.override("en"):
            assert Department.objects.get(pk=department.pk).name == "Подразделение без перевода"

    def test_grouping_ignores_default_ordering(self):
        """Сортировка по умолчанию не дробит агрегаты values().annotate()."""
        department = DepartmentFactory()
        EmployeeFactory.create_batch(3, department=department)
        rows = list(
            Employee.objects.filter(department=department)
            .values("department_id")
            .annotate(total=Count("id"))
        )
        assert rows == [{"department_id": department.pk, "total": 3}]
