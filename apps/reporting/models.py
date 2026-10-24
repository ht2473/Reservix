"""Отчёты и печатные формы."""

from __future__ import annotations

import datetime as dt
from typing import Self

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import ActivatableModel, DescribedModel, TimeStampedModel, UUIDModel
from core.utils import section_url
from core.validators import code_validator


class ReportFormat(models.TextChoices):
    """Формат выгрузки."""

    DOCX = "docx", _("Документ Word (.docx)")
    XLSX = "xlsx", _("Таблица Excel (.xlsx)")
    PDF = "pdf", _("Документ PDF")
    CSV = "csv", _("Таблица CSV")


class ReportCategory(models.TextChoices):
    """Раздел отчётности."""

    RESERVE = "reserve", _("Кадровый резерв")
    SUCCESSION = "succession", _("Преемственность")
    ASSESSMENT = "assessment", _("Оценка")
    DEVELOPMENT = "development", _("Развитие")
    ANALYTICS = "analytics", _("Аналитика")
    PERSONNEL = "personnel", _("Кадровый состав")


class ReportTemplate(DescribedModel, ActivatableModel, TimeStampedModel):
    """Шаблон отчёта — описание выгрузки."""

    code = models.SlugField(_("код"), max_length=48, unique=True, validators=[code_validator])
    category = models.CharField(
        _("раздел"),
        max_length=20,
        choices=ReportCategory.choices,
        default=ReportCategory.RESERVE,
        db_index=True,
    )
    output_format = models.CharField(
        _("формат"),
        max_length=10,
        choices=ReportFormat.choices,
        default=ReportFormat.XLSX,
    )
    builder = models.CharField(
        _("сборщик"),
        max_length=64,
        help_text=_("Идентификатор функции формирования отчёта в сервисном слое"),
    )
    parameters_schema = models.JSONField(
        _("описание параметров"),
        default=dict,
        blank=True,
        help_text=_("Состав полей формы запуска: период, филиал, группа резерва"),
    )
    allowed_roles = models.JSONField(
        _("доступен ролям"),
        default=list,
        blank=True,
        help_text=_("Пустой перечень — отчёт доступен всем ролям с правом на отчётность"),
    )
    is_async = models.BooleanField(
        _("формируется фоном"),
        default=False,
        help_text=_("Для объёмных выгрузок: задача выполняется вне запроса пользователя"),
    )
    header_note = models.TextField(
        _("текст в шапке документа"),
        blank=True,
        help_text=_("Наименование организации, гриф, реквизиты"),
    )

    class Meta:
        verbose_name = _("шаблон отчёта")
        verbose_name_plural = _("шаблоны отчётов")
        ordering = ["category", "name"]
        indexes = [
            models.Index(fields=["category", "is_active"], name="template_category_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return section_url("reporting:template_detail", pk=self.pk)

    def is_available_for(self, role: str) -> bool:
        """Доступен ли отчёт указанной роли."""
        return not self.allowed_roles or role in self.allowed_roles


class ReportStatus(models.TextChoices):
    """Состояние формирования отчёта."""

    QUEUED = "queued", _("В очереди")
    RUNNING = "running", _("Формируется")
    READY = "ready", _("Готов")
    FAILED = "failed", _("Ошибка формирования")
    EXPIRED = "expired", _("Срок хранения истёк")


class GeneratedReportQuerySet(models.QuerySet):
    """Выборки сформированных отчётов."""

    def ready(self) -> Self:
        return self.filter(status=ReportStatus.READY)

    def for_user(self, user_id: int) -> Self:
        return self.filter(requested_by_id=user_id)

    def stale(self, days: int = 30) -> Self:
        """Отчёты, подлежащие удалению по сроку хранения."""
        return self.filter(created_at__lt=timezone.now() - dt.timedelta(days=days))


class GeneratedReport(UUIDModel, TimeStampedModel):
    """Результат запуска отчёта."""

    template = models.ForeignKey(
        ReportTemplate,
        verbose_name=_("шаблон"),
        on_delete=models.PROTECT,
        related_name="reports",
    )
    title = models.CharField(_("наименование"), max_length=255)
    status = models.CharField(
        _("состояние"),
        max_length=20,
        choices=ReportStatus.choices,
        default=ReportStatus.QUEUED,
        db_index=True,
    )
    parameters = models.JSONField(
        _("параметры запуска"),
        default=dict,
        blank=True,
        help_text=_("Значения фильтров, с которыми построен отчёт"),
    )
    file = models.FileField(
        _("файл"),
        upload_to="reports/%Y/%m/",
        null=True,
        blank=True,
    )
    size_bytes = models.PositiveBigIntegerField(_("размер, байт"), default=0)
    row_count = models.PositiveIntegerField(
        _("строк в отчёте"),
        null=True,
        blank=True,
        help_text=_("Позволяет отличить пустую выгрузку от ошибки формирования"),
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("запросил"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_reports",
    )
    started_at = models.DateTimeField(_("формирование начато"), null=True, blank=True)
    finished_at = models.DateTimeField(_("формирование завершено"), null=True, blank=True)
    error_message = models.TextField(
        _("сообщение об ошибке"),
        blank=True,
        help_text=_("Заполняется при неудачном формировании"),
    )
    download_count = models.PositiveIntegerField(_("число выгрузок"), default=0, editable=False)

    objects = GeneratedReportQuerySet.as_manager()

    class Meta:
        verbose_name = _("сформированный отчёт")
        verbose_name_plural = _("сформированные отчёты")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["requested_by", "-created_at"], name="report_user_idx"),
            models.Index(fields=["status", "-created_at"], name="report_status_idx"),
        ]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return section_url("reporting:report_detail", public_id=self.public_id)

    @property
    def download_url(self) -> str:
        return section_url("reporting:report_download", public_id=self.public_id)

    @property
    def is_ready(self) -> bool:
        return self.status == ReportStatus.READY and bool(self.file)

    @property
    def duration_seconds(self) -> float | None:
        """Длительность формирования: показатель для контроля тяжёлых отчётов."""
        if not self.started_at or not self.finished_at:
            return None
        return round((self.finished_at - self.started_at).total_seconds(), 2)

    def mark_ready(self, *, row_count: int | None = None) -> None:
        """Фиксирует успешное завершение формирования."""
        self.status = ReportStatus.READY
        self.finished_at = timezone.now()
        if row_count is not None:
            self.row_count = row_count
        if self.file:
            self.size_bytes = self.file.size
        self.save(update_fields=["status", "finished_at", "row_count", "size_bytes", "updated_at"])

    def mark_failed(self, message: str) -> None:
        """Фиксирует ошибку формирования с сохранением диагностики."""
        self.status = ReportStatus.FAILED
        self.finished_at = timezone.now()
        self.error_message = message[:2000]
        self.save(update_fields=["status", "finished_at", "error_message", "updated_at"])
