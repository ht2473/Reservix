"""Чтение выгрузки исходной базы данных `cadr_reserve`."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

#: Последовательности, которыми текстовый формат `COPY` кодирует служебные символы.
ESCAPE_SEQUENCES: tuple[tuple[str, str], ...] = (
    ("\\\\", "\\"),
    ("\\t", "\t"),
    ("\\n", "\n"),
    ("\\r", "\r"),
)

#: Обозначение пустого значения в текстовом формате выгрузки.
NULL_PLACEHOLDER = "\\N"


class LegacyDumpError(RuntimeError):
    """Ошибка чтения выгрузки исходной базы."""


@dataclass(slots=True)
class LegacyTable:
    """Таблица выгрузки: перечень столбцов и строки в виде словарей."""

    name: str
    columns: list[str]
    rows: list[dict[str, Any]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)

    def by_id(self) -> dict[int, dict[str, Any]]:
        """Строки, доступные по первичному ключу исходной таблицы."""
        return {int(row["id"]): row for row in self.rows if row.get("id") is not None}


def unescape(value: str) -> str:
    """Восстанавливает исходное значение поля текстового формата `COPY`."""
    for encoded, decoded in ESCAPE_SEQUENCES:
        value = value.replace(encoded, decoded)
    return value


def parse_dump(path: Path, tables: list[str]) -> dict[str, LegacyTable]:
    """Читает указанные таблицы из текстовой выгрузки."""
    if not path.exists():
        raise LegacyDumpError(f"Файл выгрузки не найден: {path}")

    text = path.read_text(encoding="utf-8")
    result: dict[str, LegacyTable] = {}

    for table in tables:
        quoted = re.escape(f'"{table}"')
        plain = re.escape(table)
        pattern = rf"^COPY public\.(?:{quoted}|{plain}) \(([^)]*)\) FROM stdin;\n(.*?)\n\\\.$"
        match = re.search(pattern, text, re.S | re.M)
        if match is None:
            raise LegacyDumpError(f"В выгрузке нет данных таблицы «{table}»")

        columns = [column.strip() for column in match.group(1).split(",")]
        rows: list[dict[str, Any]] = []

        for line in match.group(2).split("\n"):
            if not line:
                continue
            values = line.split("\t")
            if len(values) != len(columns):
                raise LegacyDumpError(
                    f"Таблица «{table}»: в строке {len(rows) + 1} "
                    f"{len(values)} значений при {len(columns)} столбцах"
                )
            rows.append(
                {
                    column: None if raw == NULL_PLACEHOLDER else unescape(raw)
                    for column, raw in zip(columns, values, strict=True)
                }
            )

        result[table] = LegacyTable(name=table, columns=columns, rows=rows)

    return result


# ---------------------------------------------------------------------------
# Преобразование значений
# ---------------------------------------------------------------------------
def as_int(value: Any) -> int | None:
    """Целое число либо пусто."""
    if value in (None, ""):
        return None
    return int(value)


def as_bool(value: Any) -> bool:
    """Логическое значение выгрузки."""
    return value == "t"


def as_date(value: Any) -> dt.date | None:
    """Дата в формате ISO либо пусто."""
    if value in (None, ""):
        return None
    return dt.date.fromisoformat(value)


def as_datetime(value: Any, *, tz: dt.tzinfo | None = None) -> dt.datetime | None:
    """Отметка времени выгрузки, приведённая к часовому поясу."""
    if value in (None, ""):
        return None
    text = value.replace(" ", "T")
    moment = dt.datetime.fromisoformat(text)
    if tz is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment


def as_decimal(value: Any) -> Decimal | None:
    """Десятичное число либо пусто."""
    if value in (None, ""):
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:  # pragma: no cover - защита от повреждённой выгрузки
        raise LegacyDumpError(f"Значение «{value}» не является числом") from exc


def as_text(value: Any) -> str:
    """Строка без пустых значений: модели используют пустую строку, а не NULL."""
    return (value or "").strip()
