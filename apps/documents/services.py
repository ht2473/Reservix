"""Сервисный слой файлового хранилища."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.core.files.base import File
from django.db import models, transaction
from django.db.models import Count, Q, QuerySet, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.documents.models import AccessLevel, Document, DocumentKind
from core.constants import PRIVILEGED_ROLES, Role, ScopeLevel
from core.logging import get_logger
from core.permissions import can_view_employee, scope_condition

if TYPE_CHECKING:
    from apps.accounts.models import User

logger = get_logger("documents")

#: Предельная длина цепочки версий при обходе истории документа.
MAX_VERSION_CHAIN = 20

#: Роли, которым разрешена загрузка документов в хранилище.
UPLOADER_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR, Role.MANAGER, Role.MENTOR)


class DocumentError(Exception):
    """Нарушение правил работы с хранилищем."""


@dataclass(slots=True)
class StorageStatistics:
    """Сводка по хранилищу для панели администрирования."""

    total: int = 0
    size_bytes: int = 0
    expired: int = 0
    superseded: int = 0
    by_kind: list[tuple[str, int]] = field(default_factory=list)

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 1)


# ---------------------------------------------------------------------------
# Приём файлов
# ---------------------------------------------------------------------------
@transaction.atomic
def store_document(
    upload: File,
    *,
    title: str,
    uploader: User,
    kind: str = DocumentKind.OTHER,
    obj: models.Model | None = None,
    employee: Any | None = None,
    access_level: str = AccessLevel.RESTRICTED,
    description: str = "",
    valid_until: dt.date | None = None,
    supersedes: Document | None = None,
) -> Document:
    """Сохраняет файл в хранилище и регистрирует документ."""
    document = Document(
        title=title.strip(),
        kind=kind,
        file=upload,
        original_name=upload.name or "",
        content_type_name=getattr(upload, "content_type", "") or "",
        size_bytes=upload.size or 0,
        uploaded_by=uploader,
        access_level=access_level,
        description=description,
        valid_until=valid_until,
    )

    if obj is not None:
        from django.contrib.contenttypes.models import ContentType

        document.content_type = ContentType.objects.get_for_model(obj)
        document.object_id = obj.pk

    if employee is not None:
        document.employee = employee

    document.save()

    document.checksum = document.compute_checksum()
    document.save(update_fields=["checksum", "updated_at"])

    if supersedes is not None:
        supersede(supersedes, document)

    logger.info(
        "Документ принят в хранилище",
        extra={
            "document_id": str(document.public_id),
            "kind": kind,
            "size": document.size_bytes,
        },
    )
    return document


def supersede(previous: Document, replacement: Document) -> None:
    """Помечает документ замещённым более поздней версией."""
    if previous.pk == replacement.pk:
        raise DocumentError(_("Документ не может замещать сам себя"))
    if previous.superseded_by_id is not None:
        raise DocumentError(_("Документ уже замещён более поздней версией"))

    previous.superseded_by = replacement
    previous.save(update_fields=["superseded_by", "updated_at"])


def version_history(document: Document) -> list[Document]:
    """Прежние редакции документа, от ближайшей к самой ранней."""
    history: list[Document] = []
    current = document
    for _step in range(MAX_VERSION_CHAIN):
        previous = getattr(current, "supersedes", None)
        if previous is None:
            break
        history.append(previous)
        current = previous
    return history


def verify_integrity(document: Document) -> bool:
    """Сверяет контрольную сумму файла с сохранённой при приёме."""
    if not document.checksum:
        return False
    try:
        return document.compute_checksum() == document.checksum
    except (FileNotFoundError, OSError) as error:
        logger.warning(
            "Файл документа недоступен",
            extra={"document_id": str(document.public_id), "error": str(error)},
        )
        return False


# ---------------------------------------------------------------------------
# Доступ
# ---------------------------------------------------------------------------
def can_access(user: User, document: Document) -> bool:
    """Проверяет право пользователя на чтение документа."""
    if user.role in PRIVILEGED_ROLES:
        return document.employee_id is None or can_view_employee(user, document.employee)
    if document.uploaded_by_id == user.pk:
        return True

    # Документ о самом пользователе доступен ему, кроме конфиденциального: заключение кадровой
    # службы о готовности к назначению не предназначено для того, о ком составлено.
    employee = getattr(user, "employee", None)
    if employee is not None and document.employee_id == employee.pk:
        return document.access_level != AccessLevel.CONFIDENTIAL

    # Оставшиеся уровни: открытый — доступен всем сотрудникам,
    # конфиденциальный — только кадровой службе, отсеянной выше.
    if document.access_level == AccessLevel.PUBLIC:
        return True
    if document.access_level == AccessLevel.CONFIDENTIAL:
        return False
    if document.employee_id is None:
        return False
    return can_view_employee(user, document.employee)


def visible_documents(user: User) -> QuerySet[Document]:
    """Выборка документов, доступных пользователю."""
    queryset = Document.objects.with_uploader().select_related("employee")
    if user.role in PRIVILEGED_ROLES:
        if user.scope_level == ScopeLevel.ORGANIZATION:
            return queryset
        return queryset.filter(Q(employee__isnull=True) | scope_condition(user, prefix="employee"))

    condition = Q(access_level=AccessLevel.PUBLIC) | Q(uploaded_by=user)

    scope = scope_condition(user, prefix="employee")
    condition |= Q(access_level=AccessLevel.RESTRICTED, employee__isnull=False) & scope

    employee = getattr(user, "employee", None)
    if employee is not None:
        condition |= Q(employee=employee) & ~Q(access_level=AccessLevel.CONFIDENTIAL)

    return queryset.filter(condition).distinct()


def documents_for(obj: models.Model, *, user: User | None = None) -> list[Document]:
    """Документы, прикреплённые к объекту и доступные пользователю."""
    attached = (
        Document.objects.for_object(obj).current_versions().with_uploader().order_by("-created_at")
    )
    if user is None:
        return list(attached)
    return [document for document in attached if can_access(user, document)]


# ---------------------------------------------------------------------------
# Учёт
# ---------------------------------------------------------------------------
def register_download(document: Document, user: User) -> None:
    """Фиксирует выгрузку документа в счётчике и журнале аудита."""
    from apps.audit.services import record_export

    document.register_download()
    record_export(
        message=f"Выгрузка документа «{document.title}»",
        count=1,
        fmt=document.extension,
    )
    logger.info(
        "Документ выгружен",
        extra={"document_id": str(document.public_id), "user_id": user.pk},
    )


def storage_statistics() -> StorageStatistics:
    """Сводка по хранилищу."""
    totals = Document.objects.aggregate(
        total=Count("id"),
        size=Sum("size_bytes"),
        superseded=Count("id", filter=Q(superseded_by__isnull=False)),
        expired=Count("id", filter=Q(valid_until__lt=timezone.localdate())),
    )

    kinds = dict(DocumentKind.choices)
    by_kind = [
        (str(kinds.get(row["kind"], row["kind"])), row["count"])
        for row in Document.objects.values("kind").annotate(count=Count("id")).order_by("-count")
    ]

    return StorageStatistics(
        total=totals["total"] or 0,
        size_bytes=totals["size"] or 0,
        expired=totals["expired"] or 0,
        superseded=totals["superseded"] or 0,
        by_kind=by_kind,
    )
