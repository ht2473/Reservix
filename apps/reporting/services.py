"""Формирование отчётов."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from django.core.files.base import ContentFile
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.reporting.datasets import DatasetError, get_builder
from apps.reporting.models import (
    GeneratedReport,
    ReportCategory,
    ReportFormat,
    ReportStatus,
    ReportTemplate,
)
from apps.reporting.renderers import CONTENT_TYPES, RenderError, render
from apps.reporting.structure import ReportDocument
from core.constants import PRIVILEGED_ROLES, Role
from core.logging import get_logger

if TYPE_CHECKING:
    from apps.accounts.models import User

logger = get_logger("reporting")

#: Срок хранения сформированных отчётов.
RETENTION_DAYS = 90


class ReportError(Exception):
    """Отчёт невозможно сформировать."""


# ---------------------------------------------------------------------------
# Каталог
# ---------------------------------------------------------------------------
def available_templates(user: User) -> QuerySet[ReportTemplate]:
    """Шаблоны отчётов, доступные пользователю."""
    templates = ReportTemplate.objects.filter(is_active=True)
    if user.role in PRIVILEGED_ROLES:
        return templates

    # JSON-поле с перечнем ролей нельзя отфильтровать переносимо между хранилищами, поэтому отбор
    # выполняется в Python: шаблонов десятки, а не тысячи, и разница в стоимости неощутима.
    allowed = [template.pk for template in templates if template.is_available_for(user.role)]
    return templates.filter(pk__in=allowed)


def templates_by_category(user: User) -> list[tuple[str, list[ReportTemplate]]]:
    """Каталог отчётов, сгруппированный по разделам, — для страницы выгрузок."""
    labels = dict(ReportCategory.choices)
    grouped: dict[str, list[ReportTemplate]] = {}
    for template in available_templates(user).order_by("category", "name"):
        grouped.setdefault(template.category, []).append(template)
    return [(str(labels.get(key, key)), items) for key, items in grouped.items()]


def can_run(user: User, template: ReportTemplate) -> bool:
    """Вправе ли пользователь запустить отчёт."""
    if user.role in PRIVILEGED_ROLES:
        return True
    return template.is_active and template.is_available_for(user.role)


# ---------------------------------------------------------------------------
# Формирование
# ---------------------------------------------------------------------------
def build_document(
    template: ReportTemplate,
    parameters: dict[str, Any],
    user: User,
) -> ReportDocument:
    """Собирает содержимое отчёта, не записывая файл."""
    builder = get_builder(template.builder)
    document = builder(parameters, user)
    if template.header_note:
        document.notes.append(template.header_note)
    return document


def generate(
    template: ReportTemplate,
    parameters: dict[str, Any],
    user: User,
    *,
    report: GeneratedReport | None = None,
) -> GeneratedReport:
    """Формирует отчёт и сохраняет файл."""
    if not can_run(user, template):
        raise ReportError(_("Отчёт недоступен для вашей роли"))

    if report is None:
        report = GeneratedReport.objects.create(
            template=template,
            title=template.name,
            parameters=_serialize(parameters),
            requested_by=user,
            status=ReportStatus.RUNNING,
            started_at=timezone.now(),
        )
    else:
        report.status = ReportStatus.RUNNING
        report.started_at = timezone.now()
        report.save(update_fields=["status", "started_at", "updated_at"])

    try:
        document = build_document(template, parameters, user)
        content = render(document, template.output_format)
    except (DatasetError, RenderError, ValueError) as error:
        report.mark_failed(str(error))
        logger.warning(
            "Отчёт не сформирован",
            extra={"template": template.code, "error": str(error)},
        )
        raise ReportError(str(error)) from error

    report.title = document.title
    report.file.save(_filename(template, report), ContentFile(content), save=False)
    report.size_bytes = len(content)
    # Поля записываются до отметки о готовности отдельным сохранением.
    report.save(update_fields=["title", "file", "size_bytes", "updated_at"])
    report.mark_ready(row_count=document.total_rows)

    _record_export(template, document)
    logger.info(
        "Отчёт сформирован",
        extra={
            "template": template.code,
            "format": template.output_format,
            "rows": document.total_rows,
            "size": len(content),
        },
    )
    return report


def enqueue(
    template: ReportTemplate,
    parameters: dict[str, Any],
    user: User,
) -> GeneratedReport:
    """Ставит отчёт в очередь формирования."""
    if not can_run(user, template):
        raise ReportError(_("Отчёт недоступен для вашей роли"))

    report = GeneratedReport.objects.create(
        template=template,
        title=template.name,
        parameters=_serialize(parameters),
        requested_by=user,
        status=ReportStatus.QUEUED,
    )

    from apps.reporting.tasks import build_report

    build_report.delay(report.pk)
    return report


def run(
    template: ReportTemplate,
    parameters: dict[str, Any],
    user: User,
) -> GeneratedReport:
    """Запускает отчёт способом, объявленным в шаблоне: сразу либо фоном."""
    if template.is_async:
        return enqueue(template, parameters, user)
    return generate(template, parameters, user)


def content_type_for(report: GeneratedReport) -> str:
    """Тип содержимого выгрузки для отдачи браузеру."""
    return CONTENT_TYPES.get(report.template.output_format, "application/octet-stream")


# ---------------------------------------------------------------------------
# Обслуживание
# ---------------------------------------------------------------------------
def reports_for(user: User) -> QuerySet[GeneratedReport]:
    """Выгрузки, доступные пользователю."""
    queryset = GeneratedReport.objects.select_related("template", "requested_by")
    if user.role in PRIVILEGED_ROLES:
        return queryset
    return queryset.for_user(user.pk)


def cleanup(days: int = RETENTION_DAYS) -> int:
    """Удаляет устаревшие выгрузки вместе с файлами."""
    threshold = timezone.now() - dt.timedelta(days=days)
    stale = GeneratedReport.objects.filter(created_at__lt=threshold)

    removed = 0
    for report in stale.iterator():
        if report.file:
            report.file.delete(save=False)
        report.delete()
        removed += 1

    if removed:
        logger.info("Удалены устаревшие выгрузки", extra={"count": removed, "days": days})
    return removed


# ---------------------------------------------------------------------------
# Внутренние функции
# ---------------------------------------------------------------------------
def _filename(template: ReportTemplate, report: GeneratedReport) -> str:
    """Имя файла выгрузки: код отчёта, дата и краткий идентификатор."""
    moment = timezone.localtime().strftime("%Y%m%d-%H%M")
    code = slugify(template.code) or "report"
    return f"{code}-{moment}-{str(report.public_id)[:8]}.{template.output_format}"


def _serialize(parameters: dict[str, Any]) -> dict[str, Any]:
    """Приводит параметры запуска к виду, пригодному для хранения в JSON."""
    prepared: dict[str, Any] = {}
    for key, value in parameters.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, dt.date | dt.datetime):
            prepared[key] = value.isoformat()
        elif hasattr(value, "pk"):
            prepared[key] = str(value)
        else:
            prepared[key] = value
    return prepared


def _record_export(template: ReportTemplate, document: ReportDocument) -> None:
    """Фиксирует выгрузку в журнале аудита."""
    from apps.audit.services import record_export

    record_export(
        message=f"Сформирован отчёт «{document.title}»",
        count=document.total_rows,
        fmt=template.output_format,
    )


#: Роли, которым доступен раздел отчётности.
REPORTING_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR, Role.MANAGER, Role.MENTOR)

#: Форматы, для которых требуется дополнительная проверка окружения.
#: Выгрузка в PDF зависит от наличия кириллического шрифта в системе.
FORMATS_REQUIRING_FONT: frozenset[str] = frozenset({ReportFormat.PDF})
