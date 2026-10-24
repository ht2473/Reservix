"""Проверка чтения выгрузки исходной базы данных."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from django.conf import settings

from core.legacy import (
    LegacyDumpError,
    as_bool,
    as_date,
    as_datetime,
    as_decimal,
    as_int,
    as_text,
    parse_dump,
    unescape,
)

#: Фрагмент выгрузки в текстовом формате `COPY`, повторяющий формат исходной выгрузки.
SAMPLE_DUMP = """--
-- PostgreSQL database dump
--

COPY public.branch (id, name, city, address, is_active, created_at) FROM stdin;
1\tГоловной офис\tМосква\tул. Тверская, 12\tt\t2026-05-11 16:07:57.991311
2\tФилиал СПб\tСанкт-Петербург\t\\N\tf\t2026-05-11 16:07:57.991311
\\.


COPY public."User" (id, last_name, email, is_active) FROM stdin;
1\tСмирнов\tigor@company.ru\tt
\\.
"""


@pytest.fixture
def dump_file(tmp_path: Path) -> Path:
    """Файл выгрузки во временном каталоге."""
    path = tmp_path / "dump.sql"
    path.write_text(SAMPLE_DUMP, encoding="utf-8")
    return path


class TestParseDump:
    """Разбор блоков `COPY`."""

    def test_reads_columns_and_rows(self, dump_file: Path):
        """Столбцы и строки читаются в соответствии с заголовком блока."""
        tables = parse_dump(dump_file, ["branch"])

        assert tables["branch"].columns == [
            "id",
            "name",
            "city",
            "address",
            "is_active",
            "created_at",
        ]
        assert len(tables["branch"]) == 2
        assert tables["branch"].rows[0]["name"] == "Головной офис"

    def test_null_marker_becomes_none(self, dump_file: Path):
        """Обозначение пустого значения превращается в отсутствие значения."""
        tables = parse_dump(dump_file, ["branch"])
        assert tables["branch"].rows[1]["address"] is None

    def test_quoted_table_name_is_supported(self, dump_file: Path):
        """Имя таблицы в кавычках читается наравне с обычным."""
        tables = parse_dump(dump_file, ["User"])
        assert tables["User"].rows[0]["email"] == "igor@company.ru"

    def test_rows_are_indexed_by_primary_key(self, dump_file: Path):
        """Строки доступны по первичному ключу исходной таблицы."""
        tables = parse_dump(dump_file, ["branch"])
        by_id = tables["branch"].by_id()
        assert by_id[2]["city"] == "Санкт-Петербург"

    def test_missing_file_is_reported(self, tmp_path: Path):
        """Отсутствие файла выгрузки сообщается понятной ошибкой."""
        with pytest.raises(LegacyDumpError, match="не найден"):
            parse_dump(tmp_path / "nope.sql", ["branch"])

    def test_missing_table_is_reported(self, dump_file: Path):
        """Отсутствие таблицы в выгрузке сообщается с указанием имени."""
        with pytest.raises(LegacyDumpError, match="reserve"):
            parse_dump(dump_file, ["reserve"])

    def test_column_count_mismatch_is_reported(self, tmp_path: Path):
        """Несовпадение числа значений и столбцов прерывает разбор."""
        path = tmp_path / "broken.sql"
        path.write_text(
            "COPY public.branch (id, name, city) FROM stdin;\n1\tГоловной офис\n\\.\n",
            encoding="utf-8",
        )
        with pytest.raises(LegacyDumpError, match="значений"):
            parse_dump(path, ["branch"])


class TestValueConversion:
    """Преобразование значений выгрузки к типам модели."""

    def test_unescape_restores_special_characters(self):
        """Служебные последовательности восстанавливаются в исходные символы."""
        assert unescape("Первая строка\\nВторая") == "Первая строка\nВторая"
        assert unescape("столбец\\tзначение") == "столбец\tзначение"
        assert unescape("путь\\\\файл") == "путь\\файл"

    @pytest.mark.parametrize(("value", "expected"), [("t", True), ("f", False), (None, False)])
    def test_boolean_conversion(self, value, expected):
        """Логические значения PostgreSQL читаются как «t» и «f»."""
        assert as_bool(value) is expected

    def test_integer_conversion(self):
        """Пустое значение остаётся пустым, а не превращается в ноль."""
        assert as_int("42") == 42
        assert as_int(None) is None
        assert as_int("") is None

    def test_date_conversion(self):
        """Дата читается в формате ISO."""
        assert as_date("2024-03-15") == dt.date(2024, 3, 15)
        assert as_date(None) is None

    def test_datetime_receives_organization_timezone(self):
        """Отметка времени без пояса трактуется в поясе организации."""
        from django.utils import timezone as django_timezone

        moment = as_datetime(
            "2026-05-11 16:07:57.991311", tz=django_timezone.get_default_timezone()
        )
        assert moment is not None
        assert moment.tzinfo is not None
        assert moment.year == 2026

    def test_decimal_conversion(self):
        """Числовые значения читаются без потери точности."""
        assert as_decimal("85.50") == Decimal("85.50")
        assert as_decimal(None) is None

    def test_invalid_decimal_is_reported(self):
        """Нечисловое значение в числовом поле прерывает разбор."""
        with pytest.raises(LegacyDumpError):
            as_decimal("восемьдесят")

    def test_text_conversion_replaces_none_with_empty(self):
        """Пустое значение превращается в пустую строку."""
        assert as_text(None) == ""
        assert as_text("  значение  ") == "значение"


class TestRealDump:
    """Проверка на исходной выгрузке, включённой в репозиторий."""

    @pytest.fixture
    def real_dump(self) -> Path:
        path = Path(settings.BASE_DIR) / "db" / "cadr_reserve.sql"
        if not path.exists():  # pragma: no cover - выгрузка есть в репозитории
            pytest.skip("Выгрузка исходной базы недоступна")
        return path

    def test_all_nine_tables_are_read(self, real_dump: Path):
        """Все девять таблиц исходной схемы читаются целиком."""
        tables = parse_dump(
            real_dump,
            [
                "branch",
                "department",
                "User",
                "candidate",
                "reserve",
                "reservestatushistory",
                "assessment",
                "developmentplan",
                "developmentactivity",
            ],
        )

        counts = {name: len(table) for name, table in tables.items()}
        assert counts == {
            "branch": 4,
            "department": 18,
            "User": 43,
            "candidate": 40,
            "reserve": 39,
            "reservestatushistory": 45,
            "assessment": 85,
            "developmentplan": 39,
            "developmentactivity": 115,
        }
        assert sum(counts.values()) == 428
