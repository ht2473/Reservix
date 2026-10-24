"""Файловое хранилище."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Self

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q
from django.template.defaultfilters import filesizeformat
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import (
    SoftDeleteManager,
    SoftDeleteModel,
    SoftDeleteQuerySet,
    TimeStampedModel,
    UUIDModel,
)
from core.utils import section_url
from core.validators import FileExtensionValidator, FileSizeValidator


def document_upload_path(instance: Document, filename: str) -> str:
    """Строит обезличенный путь хранения файла."""
    suffix = Path(filename).suffix.lower()
    moment = timezone.now()
    return f"documents/{moment:%Y/%m}/{uuid.uuid4().hex}{suffix}"


class DocumentKind(models.TextChoices):
    """Вид документа в кадровом делопроизводстве."""

    ORDER = "order", _("Приказ")
    PROTOCOL = "protocol", _("Протокол")
    CERTIFICATE = "certificate", _("Сертификат")
    REPORT = "report", _("Отчёт")
    PROFILE = "profile", _("Профиль должности")
    PLAN = "plan", _("План")
    STATEMENT = "statement", _("Заявление")
    OTHER = "other", _("Прочее")


class AccessLevel(models.TextChoices):
    """Уровень доступа к документу."""

    PUBLIC = "public", _("Доступен всем сотрудникам")
    RESTRICTED = "restricted", _("Доступен по области видимости")
    CONFIDENTIAL = "confidential", _("Только кадровая служба и администратор")


class DocumentQuerySet(SoftDeleteQuerySet):
    """Выборки документов."""

    def of_kind(self, *kinds: str) -> Self:
        return self.filter(kind__in=kinds)

    def for_object(self, obj) -> Self:
        """Документы, прикреплённые к указанному объекту."""
        content_type = ContentType.objects.get_for_model(obj)
        return self.filter(content_type=content_type, object_id=obj.pk)

    def public(self) -> Self:
        return self.filter(access_level=AccessLevel.PUBLIC)

    def current_versions(self) -> Self:
        """Только действующие версии: замещённые скрыты из списков."""
        return self.filter(superseded_by__isnull=True)

    def with_uploader(self) -> Self:
        return self.select_related("uploaded_by")


class Document(UUIDModel, SoftDeleteModel, TimeStampedModel):
    """Документ, прикреплённый к объекту системы."""

    title = models.CharField(_("наименование"), max_length=255)
    kind = models.CharField(
        _("вид документа"),
        max_length=20,
        choices=DocumentKind.choices,
        default=DocumentKind.OTHER,
        db_index=True,
    )
    file = models.FileField(
        _("файл"),
        upload_to=document_upload_path,
        validators=[FileSizeValidator(), FileExtensionValidator()],
    )
    original_name = models.CharField(
        _("исходное имя файла"),
        max_length=255,
        help_text=_("Имя, под которым файл будет выгружен пользователю"),
    )
    content_type_name = models.CharField(
        _("тип содержимого"),
        max_length=100,
        blank=True,
        help_text=_("MIME-тип, определённый при загрузке"),
    )
    size_bytes = models.PositiveBigIntegerField(_("размер, байт"), default=0)
    checksum = models.CharField(
        _("контрольная сумма"),
        max_length=64,
        blank=True,
        db_index=True,
        help_text=_("SHA-256 содержимого: выявляет повторную загрузку того же файла"),
    )

    # -- Привязка к объекту -------------------------------------------------
    content_type = models.ForeignKey(
        ContentType,
        verbose_name=_("тип объекта"),
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="documents",
    )
    object_id = models.PositiveIntegerField(_("идентификатор объекта"), null=True, blank=True)
    attached_to = GenericForeignKey("content_type", "object_id")

    employee = models.ForeignKey(
        "people.Employee",
        verbose_name=_("сотрудник"),
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="documents",
        help_text=_("Заполняется, если документ относится к личному делу"),
    )

    # -- Доступ и версии ----------------------------------------------------
    access_level = models.CharField(
        _("уровень доступа"),
        max_length=20,
        choices=AccessLevel.choices,
        default=AccessLevel.RESTRICTED,
        db_index=True,
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("загрузил"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_documents",
    )
    superseded_by = models.OneToOneField(
        "self",
        verbose_name=_("заменён документом"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supersedes",
    )
    valid_until = models.DateField(
        _("действителен до"),
        null=True,
        blank=True,
        help_text=_("Для сертификатов и допусков с ограниченным сроком"),
    )
    description = models.TextField(_("описание"), blank=True)
    download_count = models.PositiveIntegerField(_("число выгрузок"), default=0, editable=False)

    # Менеджер по умолчанию скрывает помеченные удалёнными записи; `all_objects` отдаёт хранилище
    # целиком и применяется в обслуживающих операциях — учёте выгрузок и командах очистки.
    objects = SoftDeleteManager.from_queryset(DocumentQuerySet)()
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("документ")
        verbose_name_plural = _("документы")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["content_type", "object_id"], name="document_object_idx"),
            models.Index(fields=["kind", "-created_at"], name="document_kind_idx"),
            models.Index(fields=["employee", "kind"], name="document_employee_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(content_type__isnull=True, object_id__isnull=True)
                    | Q(content_type__isnull=False, object_id__isnull=False)
                ),
                name="document_generic_link_complete",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return section_url("documents:detail", public_id=self.public_id)

    @property
    def download_url(self) -> str:
        """Адрес защищённой выгрузки: прямой ссылки на файл не существует."""
        return section_url("documents:download", public_id=self.public_id)

    @property
    def extension(self) -> str:
        return Path(self.original_name).suffix.lower().lstrip(".")

    @property
    def size_display(self) -> str:
        """Размер в человекочитаемом виде."""
        return filesizeformat(self.size_bytes)

    @property
    def is_current(self) -> bool:
        return self.superseded_by_id is None

    @property
    def is_expired(self) -> bool:
        return self.valid_until is not None and self.valid_until < timezone.localdate()

    def compute_checksum(self) -> str:
        """Вычисляет SHA-256 содержимого файла."""
        digest = hashlib.sha256()
        self.file.open("rb")
        try:
            for chunk in self.file.chunks(chunk_size=64 * 1024):
                digest.update(chunk)
        finally:
            self.file.close()
        return digest.hexdigest()

    def register_download(self) -> None:
        """Учитывает факт выгрузки без перезаписи остальных полей."""
        Document.all_objects.filter(pk=self.pk).update(
            download_count=models.F("download_count") + 1
        )
