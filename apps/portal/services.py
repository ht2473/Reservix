"""Сервисный слой публичной части."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.core.cache import cache
from django.db.models import Count, Q
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe
from django.utils.translation import gettext_lazy as _

from apps.org.models import Branch, Department, Position
from core.logging import get_logger

logger = get_logger("portal")

#: Срок кэширования публичных показателей. Данные меняются редко, а страница
#: открывается часто, в том числе автоматическими обходчиками.
SUMMARY_CACHE_TTL = 900
SUMMARY_CACHE_KEY = "portal:summary"


@dataclass(slots=True)
class PublicSummary:
    """Обезличенные показатели организации для главной страницы."""

    branches: int = 0
    departments: int = 0
    positions: int = 0
    key_positions: int = 0
    employees: int = 0
    reserve_entries: int = 0
    ready_now: int = 0
    development_plans: int = 0
    competencies: int = 0
    coverage_percent: float = 0.0
    highlights: list[dict] = field(default_factory=list)


def get_public_summary(*, use_cache: bool = True) -> PublicSummary:
    """Собирает показатели для публичных страниц."""
    if use_cache:
        cached = cache.get(SUMMARY_CACHE_KEY)
        if cached is not None:
            return cached

    summary = PublicSummary()

    org_stats = Branch.objects.aggregate(
        total=Count("id", filter=Q(is_active=True)),
    )
    summary.branches = org_stats["total"] or 0
    summary.departments = Department.objects.filter(is_active=True).count()

    position_stats = Position.objects.aggregate(
        total=Count("id", filter=Q(is_active=True)),
        key=Count("id", filter=Q(is_active=True, is_key_position=True)),
    )
    summary.positions = position_stats["total"] or 0
    summary.key_positions = position_stats["key"] or 0

    _fill_domain_counters(summary)

    if use_cache:
        cache.set(SUMMARY_CACHE_KEY, summary, SUMMARY_CACHE_TTL)

    return summary


def _fill_domain_counters(summary: PublicSummary) -> None:
    """Дополняет сводку показателями предметных модулей."""
    from apps.competencies.models import Competency
    from apps.people.models import Employee
    from apps.reserve.models import ReserveEntry
    from core.constants import ReadinessLevel

    summary.employees = Employee.objects.active().count()
    summary.competencies = Competency.objects.filter(is_active=True).count()

    reserve_stats = ReserveEntry.objects.in_reserve().aggregate(
        total=Count("id"),
        ready=Count("id", filter=Q(readiness_level=ReadinessLevel.HIGH)),
    )
    summary.reserve_entries = reserve_stats["total"] or 0
    summary.ready_now = reserve_stats["ready"] or 0

    from apps.development.models import DevelopmentPlan, PlanStatus

    summary.development_plans = DevelopmentPlan.objects.filter(status=PlanStatus.ACTIVE).count()

    # Обеспеченность критичных должностей — единственная доля, выводимая на главной.
    from apps.succession.services import coverage_report

    summary.coverage_percent = coverage_report().coverage


def invalidate_summary() -> None:
    """Сбрасывает кэш публичных показателей после изменения справочников."""
    cache.delete(SUMMARY_CACHE_KEY)


# ---------------------------------------------------------------------------
# Разметка текстового содержимого
# ---------------------------------------------------------------------------
#: Поддерживаемые конструкции разметки.
_HEADING = re.compile(r"^(#{2,4})\s+(.+)$")
_LIST_ITEM = re.compile(r"^\*\s+(.+)$")
_ORDERED_ITEM = re.compile(r"^\d+\.\s+(.+)$")
_QUOTE = re.compile(r"^>\s+(.+)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_CODE = re.compile(r"`(.+?)`")


def render_markup(text: str) -> SafeString:
    """Преобразует упрощённую разметку в безопасный HTML."""
    if not text:
        # Пустая строка не содержит разметки. nosec B308: значение постоянно.
        return mark_safe("")  # nosec B308

    blocks: list[str] = []
    buffer: list[str] = []
    list_items: list[str] = []
    list_tag = ""

    def flush_paragraph() -> None:
        if buffer:
            blocks.append(f"<p>{' '.join(buffer)}</p>")
            buffer.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_items:
            items = "".join(f"<li>{item}</li>" for item in list_items)
            blocks.append(f"<{list_tag}>{items}</{list_tag}>")
            list_items.clear()
            list_tag = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            flush_paragraph()
            flush_list()
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue

        quote = _QUOTE.match(line)
        if quote:
            flush_paragraph()
            flush_list()
            blocks.append(f"<blockquote>{_inline(quote.group(1))}</blockquote>")
            continue

        unordered = _LIST_ITEM.match(line)
        if unordered:
            flush_paragraph()
            if list_tag and list_tag != "ul":
                flush_list()
            list_tag = "ul"
            list_items.append(_inline(unordered.group(1)))
            continue

        ordered = _ORDERED_ITEM.match(line)
        if ordered:
            flush_paragraph()
            if list_tag and list_tag != "ol":
                flush_list()
            list_tag = "ol"
            list_items.append(_inline(ordered.group(1)))
            continue

        flush_list()
        buffer.append(_inline(line))

    flush_paragraph()
    flush_list()

    # Безопасность результата обеспечена в `_inline`: каждая строка проходит через экранирование,
    # теги добавляются только этим модулем из фиксированного набора.
    return mark_safe("".join(blocks))  # noqa: S308  # nosec B308 B703


def _inline(text: str) -> str:
    """Экранирует текст и применяет внутристрочное оформление."""
    text = escape(text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    return _CODE.sub(r"<code>\1</code>", text)


# ---------------------------------------------------------------------------
# Обращения
# ---------------------------------------------------------------------------
def submit_feedback(feedback, request) -> None:
    """Сохраняет обращение и уведомляет ответственных."""
    from apps.accounts.models import User
    from apps.audit.middleware import get_audit_context
    from apps.notifications.models import NotificationKind
    from apps.notifications.services import notify_many
    from core.constants import Role

    context = get_audit_context()
    feedback.ip_address = context.ip_address
    feedback.user_agent = context.user_agent

    if request.user.is_authenticated:
        feedback.submitted_by = request.user

    feedback.save()

    administrators = list(User.objects.active().with_role(Role.ADMIN))
    notify_many(
        administrators,
        NotificationKind.SYSTEM,
        title=str(_("Новое обращение: %(subject)s") % {"subject": feedback.subject}),
        body=str(
            _("От %(name)s (%(email)s). Тема: %(topic)s.")
            % {
                "name": feedback.name,
                "email": feedback.email,
                "topic": feedback.get_topic_display(),
            }
        ),
        obj=feedback,
        deduplicate=False,
    )

    logger.info(
        "Получено обращение обратной связи",
        extra={"feedback_id": feedback.pk, "topic": feedback.topic},
    )


# ---------------------------------------------------------------------------
# База знаний и глоссарий
# ---------------------------------------------------------------------------
def article_catalog(*, query: str = "", category: str = "", tag: str = ""):
    """Опубликованные материалы базы знаний с применённым отбором."""
    from apps.portal.models import Article

    articles = Article.objects.published().select_related("category", "author")

    if query:
        articles = articles.filter(
            Q(title__icontains=query)
            | Q(summary__icontains=query)
            | Q(body__icontains=query)
            | Q(tags__icontains=query)
        )
    if category:
        articles = articles.filter(category__slug=category)
    if tag:
        articles = articles.filter(tags__icontains=tag)

    return articles.order_by("-published_at")


def article_tags(limit: int = 24) -> list[tuple[str, int]]:
    """Метки материалов с числом упоминаний."""
    from collections import Counter

    from apps.portal.models import Article

    counter: Counter[str] = Counter()
    for raw in Article.objects.published().values_list("tags", flat=True):
        for tag in (part.strip() for part in (raw or "").split(",")):
            if tag:
                counter[tag] += 1
    return counter.most_common(limit)


def glossary_index(query: str = "") -> list[tuple[str, list]]:
    """Термины глоссария, сгруппированные по первой букве."""
    from apps.portal.models import GlossaryTerm

    terms = GlossaryTerm.objects.filter(is_published=True)
    if query:
        terms = terms.filter(
            Q(term__icontains=query) | Q(definition__icontains=query) | Q(english__icontains=query)
        )

    grouped: dict[str, list] = {}
    for term in terms.order_by("term"):
        grouped.setdefault(term.letter, []).append(term)
    return sorted(grouped.items())


def faq_sections():
    """Разделы справки с опубликованными вопросами."""
    from django.db.models import Prefetch

    from apps.portal.models import FaqCategory, FaqItem

    return (
        FaqCategory.objects.filter(is_active=True)
        .prefetch_related(
            Prefetch(
                "items",
                queryset=FaqItem.objects.filter(is_published=True).order_by("sort_order"),
            )
        )
        .order_by("sort_order", "name")
    )


# ---------------------------------------------------------------------------
# Демонстрация методики
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class DemoCriterion:
    """Критерий индекса готовности для публичного калькулятора."""

    code: str
    name: str
    description: str
    weight: float
    value: int

    @property
    def percent_share(self) -> float:
        """Вес в процентах — для вывода в таблице методики."""
        return round(self.weight * 100, 1)


def readiness_demo() -> list[DemoCriterion]:
    """Состав критериев и весов действующей схемы для публичного калькулятора."""
    from apps.analytics.models import ScoringScheme

    scheme = ScoringScheme.objects.usable().default()
    if scheme is None:
        return []

    demo_values = {0: 78, 1: 64, 2: 82, 3: 55, 4: 70, 5: 60}
    criteria: list[DemoCriterion] = []

    for index, weight in enumerate(scheme.weights.select_related("criterion").order_by("-weight")):
        criteria.append(
            DemoCriterion(
                code=weight.criterion.code,
                name=weight.criterion.name,
                description=weight.criterion.description,
                weight=round(float(weight.weight), 4),
                value=demo_values.get(index, 65),
            )
        )
    return criteria


def ninebox_demo() -> dict:
    """Обезличенное распределение по матрице «результативность — потенциал»."""
    from apps.analytics.services import nine_box_grid
    from apps.portal.content import NINE_BOX_ZONES

    grid = nine_box_grid(with_people=False)
    counts = {cell.code: (cell.count, cell.share) for row in grid.rows for cell in row}

    cells = []
    for zone in NINE_BOX_ZONES:
        count, share = counts.get(zone["code"], (0, 0.0))
        cells.append(
            {
                **zone,
                "count": count,
                "share": share,
                "zone": max(1, zone["row"] + zone["column"]),
                "pool": zone["code"] in {"star", "high_potential", "high_performer"},
            }
        )

    return {
        "cells": cells,
        "total": grid.total,
        "talent_share": grid.talent_share,
        "concentration": grid.concentration,
        "is_discriminating": grid.is_discriminating,
    }


def public_competencies():
    """Публичный справочник компетенций: категории с составом."""
    from django.db.models import Prefetch

    from apps.competencies.models import Competency, CompetencyCategory

    return (
        CompetencyCategory.objects.filter(is_active=True)
        .prefetch_related(
            Prefetch(
                "competencies",
                queryset=Competency.objects.filter(is_active=True)
                .prefetch_related("scale_levels")
                .order_by("sort_order", "name"),
            )
        )
        .order_by("sort_order", "name")
    )


# ---------------------------------------------------------------------------
# Типовые формы документов
# ---------------------------------------------------------------------------
def regulation_document(code: str):
    """Собирает типовую форму документа для выгрузки в .docx."""
    from django.conf import settings
    from django.utils import timezone

    from apps.portal.content import REGULATION_TEMPLATES
    from apps.reporting.structure import Column, ReportDocument, Table

    template = next((item for item in REGULATION_TEMPLATES if item["code"] == code), None)
    if template is None:
        raise KeyError(code)

    document = ReportDocument(
        title=str(template["title"]),
        subtitle=str(template["summary"]),
        organization=str(settings.ORGANIZATION_NAME),
        author=settings.SITE_NAME,
        generated_at=timezone.localtime(),
        parameters=[(str(_("Вид документа")), str(_("типовая форма")))],
    )

    table = Table(
        title=str(_("Содержание формы")),
        columns=[
            Column("section", str(_("Раздел")), width=24),
            Column("content", str(_("Содержание")), width=60),
        ],
        note=str(
            _(
                "Форма является образцом и подлежит приспособлению к практике организации: "
                "наименования подразделений, сроки и порядок согласования устанавливаются "
                "её локальными нормативными актами."
            )
        ),
    )
    for name, content in template["sections"]:
        table.rows.append({"section": str(name), "content": str(content)})

    document.add_table(table)
    document.notes.append(
        str(
            _(
                "Документ сформирован информационной системой Reservix как типовая форма "
                "и не является нормативным актом организации."
            )
        )
    )
    return document
