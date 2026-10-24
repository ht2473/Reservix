"""Вспомогательные функции общего назначения."""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import re
import secrets
import unicodedata
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from django.utils import timezone
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _


# ---------------------------------------------------------------------------
# Имена
# ---------------------------------------------------------------------------
def full_name(last_name: str, first_name: str, middle_name: str = "") -> str:
    """Собирает полное ФИО, пропуская отсутствующее отчество."""
    return " ".join(part for part in (last_name, first_name, middle_name) if part).strip()


#: Неразрывный пробел: фамилия и инициалы не разносятся по разным строкам.
NBSP = " "


def short_name(last_name: str, first_name: str, middle_name: str = "") -> str:
    """Сокращённая форма записи «Иванов И. С.» с неразрывными пробелами."""
    initials = NBSP.join(f"{part[0]}." for part in (first_name, middle_name) if part)
    return f"{last_name}{NBSP}{initials}" if initials else last_name


def initials(first_name: str, middle_name: str = "") -> str:
    """Инициалы для аватара-заглушки: «ИС»."""
    return "".join(part[0].upper() for part in (first_name, middle_name) if part)[:2]


# ---------------------------------------------------------------------------
# Склонение
# ---------------------------------------------------------------------------
def plural_ru(number: int, forms: tuple[str, str, str]) -> str:
    """Возвращает русскую форму слова, согласованную с числом."""
    n = abs(number) % 100
    if 11 <= n <= 19:
        return forms[2]
    n %= 10
    if n == 1:
        return forms[0]
    if 2 <= n <= 4:
        return forms[1]
    return forms[2]


def plural_form(number: int, forms: Sequence[str]) -> str:
    """Возвращает форму слова по правилу действующего языка интерфейса."""
    if not forms:
        return ""
    if get_language() == "ru" and len(forms) >= 3:
        return plural_ru(number, (forms[0], forms[1], forms[2]))
    index = 0 if abs(number) == 1 else 1
    return forms[min(index, len(forms) - 1)]


def humanize_count(number: int, forms: Sequence[str]) -> str:
    """Число с согласованным словом: «3 сотрудника», ``3 employees``."""
    return f"{format_number(number)} {plural_form(number, forms)}"


# ---------------------------------------------------------------------------
# Числа
# ---------------------------------------------------------------------------
#: Разделители разрядов и дробной части по языкам интерфейса.
NUMBER_SEPARATORS: dict[str, tuple[str, str]] = {
    "ru": (" ", ","),
    "en": (",", "."),
}


def number_separators() -> tuple[str, str]:
    """Разделитель разрядов и разделитель дробной части текущего языка."""
    return NUMBER_SEPARATORS.get(get_language() or "ru", NUMBER_SEPARATORS["ru"])


def format_number(value: float | int | Decimal, digits: int = 0) -> str:
    """Оформляет число по правилам действующего языка интерфейса."""
    group, decimal_point = number_separators()
    formatted = f"{float(value):,.{digits}f}"
    # Промежуточный символ исключает повторную замену: запятая — разделитель разрядов в английском
    # и дробной части в русском, прямая замена одного на другой затронула бы уже подставленные
    # символы.
    return formatted.replace(",", "\x00").replace(".", decimal_point).replace("\x00", group)


# ---------------------------------------------------------------------------
# Даты и периоды
# ---------------------------------------------------------------------------
def calculate_age(birth_date: dt.date, on_date: dt.date | None = None) -> int:
    """Полных лет на указанную дату (по умолчанию — на сегодня)."""
    reference = on_date or timezone.localdate()
    return (
        reference.year
        - birth_date.year
        - ((reference.month, reference.day) < (birth_date.month, birth_date.day))
    )


def calculate_tenure_months(hire_date: dt.date, on_date: dt.date | None = None) -> int:
    """Стаж в организации в месяцах."""
    reference = on_date or timezone.localdate()
    months = (reference.year - hire_date.year) * 12 + (reference.month - hire_date.month)
    if reference.day < hire_date.day:
        months -= 1
    return max(months, 0)


#: Наборы форм для счётных существительных, встречающихся в нескольких местах.
YEAR_FORMS = _("год,года,лет")
MONTH_FORMS = _("месяц,месяца,месяцев")
DAY_FORMS = _("день,дня,дней")


def split_forms(value: object) -> list[str]:
    """Разбирает набор форм слова, записанный через запятую."""
    return [part.strip() for part in str(value).split(",") if part.strip()]


def format_tenure(months: int) -> str:
    """Стаж прописью: «3 года 4 месяца», ``3 years 4 months``."""
    years, rest = divmod(max(months, 0), 12)
    parts: list[str] = []
    if years:
        parts.append(humanize_count(years, split_forms(YEAR_FORMS)))
    if rest or not years:
        parts.append(humanize_count(rest, split_forms(MONTH_FORMS)))
    return " ".join(parts)


#: Порядок частей даты по языкам интерфейса.
DATE_FORMATS: dict[str, dict[str, str]] = {
    "ru": {
        "d.m.Y": "%d.%m.%Y",
        "d.m.Y H:i": "%d.%m.%Y %H:%M",
        "m.Y": "%m.%Y",
        "Y": "%Y",
    },
    "en": {
        "d.m.Y": "%m/%d/%Y",
        "d.m.Y H:i": "%m/%d/%Y %H:%M",
        "m.Y": "%m/%Y",
        "Y": "%Y",
    },
}


def date_format(fmt: str = "d.m.Y") -> str:
    """Шаблон ``strftime`` для действующего языка интерфейса."""
    formats = DATE_FORMATS.get(get_language() or "ru", DATE_FORMATS["ru"])
    return formats.get(fmt, formats["d.m.Y"])


def format_date(value: dt.date | dt.datetime | None, fmt: str = "d.m.Y") -> str:
    """Дата в формате действующего языка; пустое значение даёт прочерк."""
    if value is None:
        return "—"
    return value.strftime(date_format(fmt))


def format_period(start: dt.date | None, end: dt.date | None) -> str:
    """Период в человекочитаемом виде: «01.02.2026 — 30.06.2026»."""
    if start and end:
        return f"{format_date(start)} — {format_date(end)}"
    if start:
        return _("с %(date)s") % {"date": format_date(start)}
    if end:
        return _("по %(date)s") % {"date": format_date(end)}
    return "—"


def days_until(target: dt.date | None) -> int | None:
    """Число дней до указанной даты. Отрицательное значение означает просрочку."""
    if target is None:
        return None
    return (target - timezone.localdate()).days


def shift_months(value: dt.date, months: int) -> dt.date:
    """Сдвигает дату на указанное число месяцев; отрицательное значение — назад."""
    total = value.year * 12 + (value.month - 1) + months
    year, month = divmod(total, 12)
    day = min(value.day, calendar.monthrange(year, month + 1)[1])
    return dt.date(year, month + 1, day)


def quarter_of(value: dt.date) -> int:
    """Номер квартала, которому принадлежит дата."""
    return (value.month - 1) // 3 + 1


def quarter_bounds(year: int, quarter: int) -> tuple[dt.date, dt.date]:
    """Первый и последний день квартала."""
    first_month = 3 * (quarter - 1) + 1
    start = dt.date(year, first_month, 1)
    if quarter == 4:
        end = dt.date(year, 12, 31)
    else:
        end = dt.date(year, first_month + 3, 1) - dt.timedelta(days=1)
    return start, end


# ---------------------------------------------------------------------------
# Числа
# ---------------------------------------------------------------------------
def to_decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    """Безопасное приведение к Decimal: некорректный ввод даёт значение по умолчанию."""
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        return default


def clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    """Ограничивает значение заданным диапазоном."""
    return max(minimum, min(maximum, value))


def normalize_to_scale(
    value: float, source_min: float, source_max: float, target_max: float = 100.0
) -> float:
    """Линейно переносит значение из исходной шкалы в целевую."""
    span = source_max - source_min
    if span <= 0:
        return target_max / 2
    return clamp((value - source_min) / span * target_max, 0, target_max)


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Деление с защитой от нулевого знаменателя."""
    return numerator / denominator if denominator else default


def percentage(part: float, whole: float, digits: int = 1) -> float:
    """Доля в процентах с округлением."""
    return round(safe_divide(part, whole) * 100, digits)


# ---------------------------------------------------------------------------
# Строки и адреса
# ---------------------------------------------------------------------------
_SLUG_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def slugify_ru(value: str, max_length: int = 80) -> str:
    """Транслитерирует кириллицу и формирует адресный фрагмент."""
    value = unicodedata.normalize("NFC", value.lower())
    result = "".join(_SLUG_TRANSLIT.get(char, char) for char in value)
    result = re.sub(r"[^a-z0-9]+", "-", result).strip("-")
    return result[:max_length].rstrip("-")


def section_url(view_name: str, **kwargs: Any) -> str:
    """Адрес страницы раздела, пустой для ещё не подключённого раздела."""
    from django.urls import NoReverseMatch, get_resolver, reverse

    try:
        return reverse(view_name, kwargs=kwargs or None)
    except NoReverseMatch:
        namespace, _, _rest = view_name.partition(":")
        if namespace and namespace not in get_resolver().namespace_dict:
            return ""
        raise


def build_query(base: str = "", **params: Any) -> str:
    """Собирает адрес с параметрами, отбрасывая пустые значения."""
    clean = {k: v for k, v in params.items() if v not in (None, "", [])}
    if not clean:
        return base
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode(clean, doseq=True)}"


def truncate_middle(value: str, limit: int = 40) -> str:
    """Сокращает строку, сохраняя начало и конец."""
    if len(value) <= limit:
        return value
    head = (limit - 3) // 2
    tail = limit - 3 - head
    return f"{value[:head]}...{value[-tail:]}"


def generate_token(length: int = 32) -> str:
    """Криптостойкий токен для ссылок подтверждения и одноразовых операций."""
    return secrets.token_urlsafe(length)[:length]


def content_hash(data: bytes) -> str:
    """Контрольная сумма содержимого файла."""
    return hashlib.sha256(data).hexdigest()
