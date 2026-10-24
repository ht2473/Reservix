"""Нейтральное описание отчёта."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


class ValueKind(StrEnum):
    """Тип значения в столбце таблицы."""

    TEXT = "text"
    NUMBER = "number"
    INTEGER = "integer"
    PERCENT = "percent"
    DATE = "date"
    BOOLEAN = "boolean"


#: Столбцы этих типов выравниваются по правому краю: разряды чисел должны
#: стоять друг под другом, иначе колонку невозможно просматривать взглядом.
NUMERIC_KINDS: frozenset[ValueKind] = frozenset(
    {ValueKind.NUMBER, ValueKind.INTEGER, ValueKind.PERCENT}
)


@dataclass(frozen=True, slots=True)
class Column:
    """Столбец таблицы отчёта."""

    key: str
    title: str
    kind: ValueKind = ValueKind.TEXT
    width: int = 0

    @property
    def is_numeric(self) -> bool:
        return self.kind in NUMERIC_KINDS


@dataclass(slots=True)
class Table:
    """Таблица отчёта."""

    title: str
    columns: list[Column]
    rows: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def values(self, row: dict[str, Any]) -> list[Any]:
        """Значения строки в порядке столбцов."""
        return [row.get(column.key) for column in self.columns]


@dataclass(slots=True)
class Metric:
    """Показатель в сводке отчёта: подпись, значение, пояснение."""

    label: str
    value: Any
    kind: ValueKind = ValueKind.TEXT
    note: str = ""


@dataclass(slots=True)
class ReportDocument:
    """Отчёт целиком."""

    title: str
    subtitle: str = ""
    organization: str = ""
    author: str = ""
    generated_at: dt.datetime | None = None
    parameters: list[tuple[str, str]] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        """Число строк во всех таблицах — сохраняется в карточке выгрузки."""
        return sum(table.row_count for table in self.tables)

    def add_table(self, table: Table) -> Table:
        self.tables.append(table)
        return table


# ---------------------------------------------------------------------------
# Оформление значений
# ---------------------------------------------------------------------------
#: Прочерк вместо пустого значения.
EMPTY = "—"


def format_value(value: Any, kind: ValueKind) -> str:
    """Приводит значение к виду, принятому в русскоязычном документе."""
    if value is None or value == "":
        return EMPTY

    if kind is ValueKind.BOOLEAN:
        return "да" if value else "нет"

    if kind is ValueKind.DATE:
        if isinstance(value, dt.datetime):
            return value.strftime("%d.%m.%Y %H:%M")
        if isinstance(value, dt.date):
            return value.strftime("%d.%m.%Y")
        return str(value)

    if kind is ValueKind.INTEGER:
        return f"{int(value):,}".replace(",", " ")

    if kind is ValueKind.PERCENT:
        return f"{float(value):.1f}".replace(".", ",") + " %"

    if kind is ValueKind.NUMBER:
        if isinstance(value, Decimal | float | int):
            return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
        return str(value)

    return str(value)


def as_number(value: Any) -> float | int | None:
    """Приводит значение к числу для записи в ячейку таблицы Excel."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return value
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
