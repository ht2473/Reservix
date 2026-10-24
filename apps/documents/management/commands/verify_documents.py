"""
Сверка целостности файлового хранилища.

Запуск::

    uv run python manage.py verify_documents
    uv run python manage.py verify_documents --quiet    # только итог
    uv run python manage.py verify_documents --limit 500
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.documents.models import Document
from apps.documents.services import verify_integrity


class Command(BaseCommand):
    """Сверяет контрольные суммы всех документов хранилища."""

    help = "Сверяет контрольные суммы файлов хранилища с сохранёнными при приёме"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Проверить только указанное число документов (0 — все)",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Не перечислять документы по одному, вывести только итог",
        )
        parser.add_argument(
            "--fail-on-mismatch",
            action="store_true",
            help="Завершиться с ненулевым кодом при первом же расхождении",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        limit: int = options["limit"]
        quiet: bool = options["quiet"]
        strict: bool = options["fail_on_mismatch"]

        # Удалённые документы включаются в проверку намеренно: мягкое удаление оставляет файл на
        # диске, и он остаётся частью хранилища — а значит, частью того, за что хранилище отвечает.
        queryset = Document.all_objects.order_by("created_at")
        if limit:
            queryset = queryset[:limit]

        total = 0
        intact = 0
        mismatched: list[Document] = []
        missing: list[Document] = []

        for document in queryset.iterator(chunk_size=200):
            total += 1

            if not document.checksum:
                # Документ без сохранённой суммы принят до введения сверки
                # либо сохранён в обход служебного слоя. Это не повреждение,
                # но и подтвердить целостность нечем.
                missing.append(document)
                continue

            if verify_integrity(document):
                intact += 1
                if not quiet:
                    self.stdout.write(f"  ✓ {document.public_id}  {document.title}")
            else:
                mismatched.append(document)
                self.stdout.write(self.style.ERROR(f"  ✗ {document.public_id}  {document.title}"))

        self.stdout.write("")
        self.stdout.write(f"Проверено документов: {total}")
        self.stdout.write(self.style.SUCCESS(f"Целостность подтверждена: {intact}"))

        if missing:
            self.stdout.write(
                self.style.WARNING(f"Без сохранённой контрольной суммы: {len(missing)}")
            )
            for document in missing[:10]:
                self.stdout.write(f"    {document.public_id}  {document.title}")

        if mismatched:
            self.stdout.write(self.style.ERROR(f"Не прошли сверку: {len(mismatched)}"))
            self.stdout.write(
                "Расхождение означает подмену, повреждение файла либо базу и "
                "медиафайлы из разных резервных копий."
            )
            if strict:
                raise CommandError("Хранилище содержит документы, не прошедшие сверку")
        else:
            self.stdout.write(self.style.SUCCESS("Расхождений не обнаружено"))
