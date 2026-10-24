"""Модульные тесты валидаторов."""

from __future__ import annotations

import datetime as dt

import pytest

from django.core.exceptions import ValidationError

from apps.accounts.validators import ComplexityValidator
from core.validators import (
    normalize_phone,
    validate_birth_date,
    validate_competency_level,
    validate_period,
    validate_safe_filename,
    validate_score,
    validate_weight,
)

pytestmark = pytest.mark.unit


class TestPhoneNormalization:
    """Приведение телефона к каноническому виду."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("+7 900 111-22-33", "+79001112233"),
            ("8 900 111 22 33", "+79001112233"),
            ("8(900)111-22-33", "+79001112233"),
            ("79001112233", "+79001112233"),
            ("9001112233", "+79001112233"),
            ("+7-900-111-22-33", "+79001112233"),
        ],
    )
    def test_normalization(self, raw, expected):
        assert normalize_phone(raw) == expected

    def test_empty_value(self):
        assert normalize_phone("") == ""
        assert normalize_phone(None) == ""


class TestBirthDate:
    """Проверка правдоподобности даты рождения."""

    def test_valid_date(self):
        validate_birth_date(dt.date(1990, 5, 14))

    def test_future_date_rejected(self):
        future = dt.date.today() + dt.timedelta(days=1)
        with pytest.raises(ValidationError, match="будущем"):
            validate_birth_date(future)

    def test_too_young_rejected(self):
        """Возраст ниже минимального для трудоустройства."""
        recent = dt.date.today() - dt.timedelta(days=365 * 10)
        with pytest.raises(ValidationError, match="14 лет"):
            validate_birth_date(recent)

    def test_implausible_age_rejected(self):
        """Опечатка в веке даёт возраст свыше ста лет."""
        with pytest.raises(ValidationError, match="100 лет"):
            validate_birth_date(dt.date(1890, 1, 1))


class TestPeriod:
    """Согласованность дат начала и окончания."""

    def test_valid_period(self):
        validate_period(dt.date(2026, 1, 1), dt.date(2026, 6, 30))

    def test_reversed_period_rejected(self):
        with pytest.raises(ValidationError):
            validate_period(dt.date(2026, 6, 30), dt.date(2026, 1, 1))

    def test_same_day_allowed(self):
        """Однодневное мероприятие — допустимый случай."""
        day = dt.date(2026, 3, 1)
        validate_period(day, day)

    def test_open_period_allowed(self):
        """Отсутствие даты окончания означает бессрочный период."""
        validate_period(dt.date(2026, 1, 1), None)


class TestScores:
    """Диапазоны оценок и весов."""

    @pytest.mark.parametrize("value", [0, 50, 100, 99.9])
    def test_valid_scores(self, value):
        validate_score(value)

    @pytest.mark.parametrize("value", [-1, 101, 1000])
    def test_out_of_range_rejected(self, value):
        with pytest.raises(ValidationError):
            validate_score(value)

    def test_none_allowed(self):
        """Оценка может отсутствовать: сотрудник ещё не оценивался."""
        validate_score(None)

    @pytest.mark.parametrize("level", [1, 3, 5])
    def test_valid_competency_levels(self, level):
        validate_competency_level(level)

    @pytest.mark.parametrize("level", [0, 6, -2])
    def test_invalid_competency_levels(self, level):
        with pytest.raises(ValidationError):
            validate_competency_level(level)

    @pytest.mark.parametrize("weight", [0, 0.35, 1])
    def test_valid_weights(self, weight):
        validate_weight(weight)

    @pytest.mark.parametrize("weight", [-0.1, 1.5])
    def test_invalid_weights(self, weight):
        with pytest.raises(ValidationError):
            validate_weight(weight)


class TestFilenameSafety:
    """Отклонение имён файлов, пригодных для выхода за каталог хранения."""

    @pytest.mark.parametrize(
        "name",
        [
            "../../etc/passwd",
            "..\\..\\windows\\system32",
            "документ/скрытый.docx",
            "",
            ".",
            "..",
            ".hidden",
        ],
    )
    def test_dangerous_names_rejected(self, name):
        with pytest.raises(ValidationError):
            validate_safe_filename(name)

    @pytest.mark.parametrize(
        "name",
        ["отчёт.docx", "plan_2026.xlsx", "Приказ №14 от 01.03.2026.pdf"],
    )
    def test_valid_names_accepted(self, name):
        validate_safe_filename(name)


class TestPasswordComplexity:
    """Требования к составу пароля."""

    validator = ComplexityValidator()

    @pytest.mark.parametrize(
        "password",
        [
            "Nadezhnyi-Parol-2026",
            "Kadrovaya-Sluzhba-77",
            "Vesna2026Moskva",
        ],
    )
    def test_valid_passwords(self, password):
        self.validator.validate(password)

    @pytest.mark.parametrize(
        "password",
        [
            "простойпароль",  # одна группа символов
            "ТОЛЬКОПРОПИСНЫЕ",  # одна группа символов
            "1234567890",  # только цифры
        ],
    )
    def test_insufficient_variety_rejected(self, password):
        with pytest.raises(ValidationError):
            self.validator.validate(password)

    @pytest.mark.parametrize(
        "password",
        ["MyReservixPass1!", "Qwerty12345!", "AdminPanel2026!"],
    )
    def test_forbidden_substrings_rejected(self, password):
        """Слова, очевидные для предметной области, недопустимы."""
        with pytest.raises(ValidationError):
            self.validator.validate(password)

    @pytest.mark.parametrize("password", ["Abcdefg123!", "Zyx98765Ab!"])
    def test_sequences_rejected(self, password):
        """Последовательности подбираются словарём за секунды."""
        with pytest.raises(ValidationError):
            self.validator.validate(password)

    def test_help_text_describes_requirement(self):
        """Подсказка под полем должна называть требуемые группы символов."""
        help_text = self.validator.get_help_text()
        assert "групп" in help_text
        assert "цифры" in help_text
