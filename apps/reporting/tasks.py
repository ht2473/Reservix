"""Фоновые задачи модуля отчётности."""

from __future__ import annotations

from celery import shared_task

from django.utils import timezone
from django.utils.translation import gettext as _

from apps.reporting.models import GeneratedReport, ReportStatus
from core.logging import get_logger

logger = get_logger("reporting")


@shared_task(bind=True, max_retries=1)
def build_report(self, report_id: int) -> str:
    """Формирует ранее поставленный в очередь отчёт."""
    report = (
        GeneratedReport.objects.filter(pk=report_id)
        .select_related("template", "requested_by")
        .first()
    )

    if report is None:
        logger.warning("Выгрузка не найдена", extra={"report_id": report_id})
        return "not_found"

    if report.status == ReportStatus.READY:
        return "already_ready"

    requester = report.requested_by
    if requester is None:
        # Учётная запись автора запроса удалена, пока выгрузка стояла в очереди.
        report.mark_failed("Учётная запись, запросившая отчёт, недоступна")
        return "no_requester"

    from apps.reporting.services import ReportError, generate

    try:
        generate(report.template, report.parameters, requester, report=report)
    except ReportError:
        # Причина уже записана в саму выгрузку методом `mark_failed`.
        return "failed"
    except Exception as error:  # pragma: no cover - системный сбой
        report.mark_failed(str(error))
        logger.exception("Сбой при формировании отчёта", extra={"report_id": report_id})
        raise self.retry(exc=error, countdown=60) from error

    _notify_requester(report)
    return "ready"


@shared_task
def cleanup_reports(days: int = 90) -> int:
    """Удаляет устаревшие выгрузки вместе с файлами."""
    from apps.reporting.services import cleanup

    return cleanup(days)


def _notify_requester(report: GeneratedReport) -> None:
    """Извещает автора запроса о готовности отчёта."""
    from apps.notifications.models import NotificationKind
    from apps.notifications.services import notify

    if report.requested_by is None:
        return

    notify(
        report.requested_by,
        NotificationKind.REPORT_READY,
        title=_("Отчёт готов: %(title)s") % {"title": report.title},
        body=(
            _("Строк в выгрузке — %(rows)s. Сформирован %(moment)s.")
            % {
                "rows": report.row_count,
                "moment": f"{timezone.localtime(report.finished_at):%d.%m.%Y %H:%M}",
            }
            if report.finished_at
            else _("Строк в выгрузке — %(rows)s.") % {"rows": report.row_count}
        ),
        obj=report,
        deduplicate=False,
    )
