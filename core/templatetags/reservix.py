"""Фильтры и теги шаблонов Reservix."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django import template
from django.conf import settings
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString, mark_safe
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _

from core.constants import ReadinessLevel, Signal
from core.settings_types import get_reservix_settings
from core.utils import (
    DAY_FORMS,
    days_until,
    format_date,
    format_number,
    format_tenure,
    humanize_count,
    number_separators,
    plural_form,
    split_forms,
    truncate_middle,
)

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

register = template.Library()


# ---------------------------------------------------------------------------
# Числа и единицы измерения
# ---------------------------------------------------------------------------
@register.filter
def spaced(value: Any, digits: int = 0) -> str:
    """Разделяет разряды по правилам языка интерфейса: ``1 240 567``, ``1,240,567``."""
    if value in (None, ""):
        return "—"
    try:
        return format_number(float(value), digits)
    except (TypeError, ValueError):
        return str(value)


@register.filter
def score(value: Any, digits: int = 1) -> str:
    """Оценка или индекс: один знак после разделителя, прочерк вместо пустого значения."""
    if value in (None, ""):
        return "—"
    try:
        return f"{float(value):.{digits}f}".replace(".", number_separators()[1])
    except (TypeError, ValueError):
        return str(value)


@register.filter
def percent(value: Any, digits: int = 0) -> str:
    """Доля в процентах со знаком: ``86 %``."""
    if value in (None, ""):
        return "—"
    try:
        return f"{float(value):.{digits}f}".replace(".", number_separators()[1]) + " %"
    except (TypeError, ValueError):
        return str(value)


@register.filter
def delta(value: Any, digits: int = 1) -> str:
    """Изменение показателя со знаком: ``+6,2`` или ``−3,4``."""
    if value in (None, ""):
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "+" if number > 0 else "−" if number < 0 else ""
    return f"{sign}{abs(number):.{digits}f}".replace(".", number_separators()[1])


@register.filter
def plural(number: int, forms: str) -> str:
    """Согласует слово с числом."""
    parts = split_forms(forms)
    return plural_form(int(number or 0), parts)


@register.filter
def counted(number: int, forms: str) -> str:
    """Число вместе с согласованным словом: ``3 сотрудника``, ``3 employees``."""
    parts = split_forms(forms)
    if not parts:
        return str(number)
    return humanize_count(int(number or 0), parts)


@register.filter
def tenure(months: Any) -> str:
    """Стаж прописью по числу месяцев."""
    try:
        return format_tenure(int(months))
    except (TypeError, ValueError):
        return "—"


# ---------------------------------------------------------------------------
# Даты и сроки
# ---------------------------------------------------------------------------
@register.filter
def deadline(value: dt.date | None) -> SafeString:
    """Срок с указанием остатка времени и сигнальной окраской."""
    if value is None:
        # Постоянная разметка без подстановок. nosec B308.
        return mark_safe('<span class="deadline deadline--none">—</span>')  # nosec B308

    remaining = days_until(value)
    threshold = get_reservix_settings()["ACTIVITY_DUE_SOON_DAYS"]
    date_text = format_date(value)

    if remaining is None:
        return format_html('<span class="deadline">{}</span>', date_text)
    note: StrOrPromise
    if remaining < 0:
        note = _("просрочено на %(days)s") % {
            "days": humanize_count(abs(remaining), split_forms(DAY_FORMS))
        }
        modifier = "risk"
    elif remaining == 0:
        note = _("сегодня")
        modifier = "develop"
    elif remaining <= threshold:
        note = _("осталось %(days)s") % {"days": humanize_count(remaining, split_forms(DAY_FORMS))}
        modifier = "develop"
    else:
        note = ""
        modifier = "ready"

    if note:
        return format_html(
            '<span class="deadline deadline--{}">{}<span class="deadline__note">{}</span></span>',
            modifier,
            date_text,
            note,
        )
    return format_html('<span class="deadline deadline--{}">{}</span>', modifier, date_text)


@register.filter
def date_ru(value: dt.date | dt.datetime | None, fmt: str = "d.m.Y") -> str:
    """Дата в формате действующего языка; пустое значение даёт прочерк."""
    return format_date(value, fmt)


# ---------------------------------------------------------------------------
# Индикаторы предметной области
# ---------------------------------------------------------------------------
@register.simple_tag
def readiness_badge(level: str, label: str = "") -> SafeString:
    """Значок уровня готовности резервиста."""
    mapping: dict[str, tuple[str, StrOrPromise]] = {
        ReadinessLevel.HIGH: ("ready", _("Готов к назначению")),
        ReadinessLevel.MEDIUM: ("develop", _("Готов в течение года")),
        ReadinessLevel.LOW: ("risk", _("Требуется развитие")),
    }
    modifier, default_label = mapping.get(level, ("neutral", _("Не определён")))
    return format_html(
        '<span class="badge badge--{}" data-readiness="{}">{}</span>',
        modifier,
        level or "unknown",
        label or default_label,
    )


@register.simple_tag
def signal_dot(signal: str, title: str = "") -> SafeString:
    """Точка-индикатор состояния для компактных таблиц."""
    known = {Signal.READY, Signal.DEVELOP, Signal.RISK, Signal.NEUTRAL}
    modifier = signal if signal in known else Signal.NEUTRAL
    return format_html(
        '<span class="signal-dot signal-dot--{}" role="img" aria-label="{}" title="{}"></span>',
        modifier,
        title or modifier,
        title or modifier,
    )


@register.simple_tag
def progress_bar(value: Any, maximum: Any = 100, label: str = "", tone: str = "") -> SafeString:
    """
    Полоса выполнения с доступной подписью.

    Без ``tone`` цвет оценивает значение (готовность, выполнение); ``tone="neutral"``
    нужен для долей и весов, где большее значение не лучше и не хуже.
    """
    try:
        current = float(value or 0)
        top = float(maximum or 100) or 100
    except (TypeError, ValueError):
        current, top = 0.0, 100.0

    ratio = max(0.0, min(1.0, current / top))
    if tone == "neutral":
        modifier = "accent"
    else:
        modifier = "ready" if ratio >= 0.75 else "develop" if ratio >= 0.4 else "risk"

    return format_html(
        '<div class="progress" role="progressbar" aria-valuenow="{}" aria-valuemin="0" '
        'aria-valuemax="{}" aria-label="{}">'
        '<div class="progress__fill progress__fill--{}" style="width:{}%"></div></div>',
        round(current, 1),
        round(top, 1),
        label or _("Прогресс"),
        modifier,
        round(ratio * 100, 2),
    )


@register.filter
def localized(value: Any) -> str:
    """Сохранённый системный текст на языке интерфейса: журнал аудита, снимки."""
    text = "" if value is None else str(value)
    if not text or get_language() != "en":
        return text
    from core.data_translation import default_translator

    return default_translator().translate(text, record=False) or text


@register.filter
def localized_name(value: Any) -> str:
    """Часть имени из выборки values(): в английском интерфейсе транслитерируется."""
    text = "" if value is None else str(value)
    if not text or get_language() != "en":
        return text
    from core.data_translation import transliterate

    return transliterate(text)


@register.simple_tag
def icon(name: str, size: int = 20, css_class: str = "") -> SafeString:
    """Значок из единого SVG-спрайта."""
    return format_html(
        '<svg class="icon {}" width="{}" height="{}" aria-hidden="true" focusable="false">'
        '<use href="{}img/icons.svg#icon-{}"></use></svg>',
        css_class,
        size,
        size,
        settings.STATIC_URL,
        name,
    )


# ---------------------------------------------------------------------------
# Работа со строками и структурами
# ---------------------------------------------------------------------------
@register.filter
def shorten(value: str, limit: int = 40) -> str:
    """Сокращает длинное наименование, сохраняя начало и конец."""
    return truncate_middle(str(value or ""), int(limit))


@register.filter
def person(user: Any, empty: str = "—") -> str:
    """Имя пользователя для вывода в таблице: ФИО, иначе адрес почты."""
    if user is None:
        return empty
    full = getattr(user, "get_full_name", None)
    name = full() if callable(full) else ""
    return str(name or getattr(user, "email", "") or empty)


@register.filter
def get_item(mapping: Any, key: Any) -> Any:
    """Доступ к элементу словаря по переменному ключу."""
    if not isinstance(mapping, dict):
        return None
    return mapping.get(key)


@register.filter
def to_json(value: Any) -> SafeString:
    """Сериализует данные для передачи в скрипт графика."""

    def default(obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, dt.date | dt.datetime):
            return obj.isoformat()
        return str(obj)

    payload = json.dumps(value, ensure_ascii=False, default=default)
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    # Символы, способные закрыть тег <script>, заменены экранированными
    # последовательностями JSON строкой выше.
    return mark_safe(payload)  # noqa: S308  # nosec B308 B703


@register.simple_tag(takes_context=True)
def query_replace(context: template.Context, **kwargs: Any) -> str:
    """Изменяет параметры текущего адреса, сохраняя остальные."""
    request = context.get("request")
    if request is None:
        return ""
    params = request.GET.copy()
    for key, value in kwargs.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value
    return params.urlencode()


@register.simple_tag
def field_errors(field) -> SafeString:
    """Список ошибок поля с идентификатором для связи через ``aria-describedby``."""
    if not field.errors:
        # Пустая строка при отсутствии ошибок.
        return mark_safe("")  # nosec B308
    items = format_html_join("", "<li>{}</li>", ((error,) for error in field.errors))
    return format_html('<ul class="field-errors" id="error-{}">{}</ul>', field.html_name, items)


@register.filter
def add_class(field, css: str):
    """Добавляет CSS-класс виджету прямо в шаблоне, не переопределяя форму."""
    existing = field.field.widget.attrs.get("class", "")
    return field.as_widget(attrs={"class": f"{existing} {css}".strip()})
