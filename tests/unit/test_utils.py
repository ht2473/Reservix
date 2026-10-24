"""Модульные тесты вспомогательных функций."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from core.utils import (
    build_query,
    calculate_age,
    calculate_tenure_months,
    clamp,
    format_period,
    format_tenure,
    full_name,
    humanize_count,
    initials,
    normalize_to_scale,
    percentage,
    plural_ru,
    quarter_bounds,
    quarter_of,
    safe_divide,
    short_name,
    slugify_ru,
    to_decimal,
    truncate_middle,
)

pytestmark = pytest.mark.unit


class TestNames:
    """Форматирование ФИО."""

    def test_full_name_with_middle(self):
        assert full_name("Иванова", "Мария", "Сергеевна") == "Иванова Мария Сергеевна"

    def test_full_name_without_middle(self):
        assert full_name("Смирнов", "Игорь", "") == "Смирнов Игорь"

    def test_short_name_with_middle(self):
        assert short_name("Иванова", "Мария", "Сергеевна") == "Иванова М. С."

    def test_short_name_without_middle(self):
        assert short_name("Смирнов", "Игорь") == "Смирнов И."

    def test_initials(self):
        assert initials("Мария", "Сергеевна") == "МС"

    def test_initials_without_middle(self):
        assert initials("Игорь") == "И"


class TestPluralization:
    """Согласование существительных с числительными."""

    FORMS = ("сотрудник", "сотрудника", "сотрудников")

    @pytest.mark.parametrize(
        ("number", "expected"),
        [
            (1, "сотрудник"),
            (2, "сотрудника"),
            (4, "сотрудника"),
            (5, "сотрудников"),
            (11, "сотрудников"),
            (12, "сотрудников"),
            (14, "сотрудников"),
            (21, "сотрудник"),
            (22, "сотрудника"),
            (25, "сотрудников"),
            (101, "сотрудник"),
            (111, "сотрудников"),
            (112, "сотрудников"),
            (0, "сотрудников"),
        ],
    )
    def test_plural_forms(self, number, expected):
        assert plural_ru(number, self.FORMS) == expected

    def test_humanize_count(self):
        assert humanize_count(3, self.FORMS) == "3 сотрудника"

    def test_negative_number_uses_absolute_value(self):
        """Отрицательное изменение показателя согласуется по модулю."""
        assert plural_ru(-3, self.FORMS) == "сотрудника"


class TestTenure:
    """Расчёт возраста и стажа."""

    def test_age_before_birthday(self):
        """До дня рождения возраст на год меньше."""
        birth = dt.date(1990, 12, 31)
        assert calculate_age(birth, dt.date(2026, 12, 30)) == 35

    def test_age_on_birthday(self):
        birth = dt.date(1990, 6, 15)
        assert calculate_age(birth, dt.date(2026, 6, 15)) == 36

    def test_tenure_incomplete_month(self):
        """Месяц засчитывается только по достижении дня приёма."""
        hire = dt.date(2026, 3, 15)
        assert calculate_tenure_months(hire, dt.date(2026, 4, 14)) == 0
        assert calculate_tenure_months(hire, dt.date(2026, 4, 15)) == 1

    def test_tenure_across_years(self):
        hire = dt.date(2020, 3, 15)
        assert calculate_tenure_months(hire, dt.date(2026, 8, 20)) == 77

    def test_tenure_never_negative(self):
        """Дата приёма в будущем не даёт отрицательного стажа."""
        hire = dt.date(2027, 1, 1)
        assert calculate_tenure_months(hire, dt.date(2026, 1, 1)) == 0

    @pytest.mark.parametrize(
        ("months", "expected"),
        [
            (0, "0 месяцев"),
            (1, "1 месяц"),
            (3, "3 месяца"),
            (12, "1 год"),
            (13, "1 год 1 месяц"),
            (26, "2 года 2 месяца"),
            (60, "5 лет"),
        ],
    )
    def test_format_tenure(self, months, expected):
        assert format_tenure(months) == expected


class TestPeriods:
    """Работа с периодами и кварталами."""

    def test_format_period_full(self):
        result = format_period(dt.date(2026, 2, 1), dt.date(2026, 6, 30))
        assert result == "01.02.2026 — 30.06.2026"

    def test_format_period_empty(self):
        assert format_period(None, None) == "—"

    @pytest.mark.parametrize(
        ("month", "quarter"),
        [(1, 1), (3, 1), (4, 2), (6, 2), (7, 3), (9, 3), (10, 4), (12, 4)],
    )
    def test_quarter_of(self, month, quarter):
        assert quarter_of(dt.date(2026, month, 15)) == quarter

    def test_quarter_bounds_first(self):
        start, end = quarter_bounds(2026, 1)
        assert start == dt.date(2026, 1, 1)
        assert end == dt.date(2026, 3, 31)

    def test_quarter_bounds_last(self):
        start, end = quarter_bounds(2026, 4)
        assert start == dt.date(2026, 10, 1)
        assert end == dt.date(2026, 12, 31)


class TestNumbers:
    """Числовые преобразования, используемые в расчёте индекса готовности."""

    def test_to_decimal_accepts_comma(self):
        """Пользователь вводит дробное значение через запятую."""
        assert to_decimal("3,5") == Decimal("3.5")

    def test_to_decimal_invalid_returns_default(self):
        assert to_decimal("не число", Decimal("0")) == Decimal("0")

    def test_clamp(self):
        assert clamp(150) == 100
        assert clamp(-10) == 0
        assert clamp(42.5) == 42.5

    def test_normalize_competency_scale(self):
        """Уровень 3 по шкале 1–5 соответствует 50 баллам из 100."""
        assert normalize_to_scale(3, 1, 5) == 50.0

    def test_normalize_boundaries(self):
        assert normalize_to_scale(1, 1, 5) == 0.0
        assert normalize_to_scale(5, 1, 5) == 100.0

    def test_normalize_degenerate_scale_returns_middle(self):
        """Нулевой размах шкалы даёт середину диапазона."""
        assert normalize_to_scale(7, 5, 5) == 50.0

    def test_safe_divide_by_zero(self):
        assert safe_divide(10, 0) == 0.0
        assert safe_divide(10, 0, default=1.0) == 1.0

    def test_percentage(self):
        assert percentage(3, 4) == 75.0
        assert percentage(0, 0) == 0.0


class TestStrings:
    """Преобразование строк и построение адресов."""

    def test_slugify_cyrillic(self):
        assert slugify_ru("Кадровый резерв") == "kadrovyy-rezerv"

    def test_slugify_removes_punctuation(self):
        assert slugify_ru("Оценка 360°: как проводить?") == "otsenka-360-kak-provodit"

    def test_slugify_respects_max_length(self):
        result = slugify_ru("Формирование кадрового резерва организации", max_length=20)
        assert len(result) <= 20
        assert not result.endswith("-")

    def test_build_query_skips_empty(self):
        result = build_query("/reserve/", status="active", branch="", page=2)
        assert "status=active" in result
        assert "branch=" not in result
        assert "page=2" in result

    def test_build_query_without_params(self):
        assert build_query("/reserve/") == "/reserve/"

    def test_truncate_middle_keeps_both_ends(self):
        value = "Ведущий специалист департамента корпоративных продаж"
        result = truncate_middle(value, 30)
        assert len(result) == 30
        assert result.startswith("Ведущий")
        assert result.endswith("продаж")

    def test_truncate_middle_short_string_unchanged(self):
        assert truncate_middle("Аналитик", 40) == "Аналитик"
