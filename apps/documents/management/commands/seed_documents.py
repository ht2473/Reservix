"""
Наполнение файлового хранилища.

Запуск::

    uv run python manage.py seed_documents
    uv run python manage.py seed_documents --flush
"""

from __future__ import annotations

import datetime as dt
import random

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.documents.models import AccessLevel, Document, DocumentKind
from apps.documents.services import store_document, supersede
from apps.reporting.renderers import render_docx
from apps.reporting.structure import Column, ReportDocument, Table, ValueKind
from core.constants import Role

#: Доля записей резерва, для которых создаётся приказ о зачислении.
ORDER_SHARE = 0.45

#: Доля мероприятий с подтверждением, к которым прикладывается файл
#: сертификата. Остальные подтверждены ссылкой на внутреннюю систему обучения.
CERTIFICATE_SHARE = 0.4


class Command(BaseCommand):
    """Создаёт документы кадрового делопроизводства на стенде."""

    help = "Наполняет файловое хранилище приказами, протоколами и сертификатами"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--seed", type=int, default=None, help="значение генератора")
        parser.add_argument("--flush", action="store_true", help="удалить существующие документы")

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        self.random = random.Random(options["seed"])  # noqa: S311  # nosec B311

        if options["flush"]:
            for document in Document.all_objects.all():
                if document.file:
                    document.file.delete(save=False)
            removed, _details = Document.all_objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Удалено документов: {removed}"))

        author = self._author()
        if author is None:
            self.stdout.write(self.style.WARNING("Нет пользователя HR — документы не создаются"))
            return

        policy = self._create_policy(author)
        orders = self._create_orders(author)
        protocols = self._create_protocols(author)
        certificates = self._create_certificates(author)
        conclusions = self._create_conclusions(author)

        self.stdout.write(
            self.style.SUCCESS(
                f"Хранилище наполнено: общедоступных {policy}, приказов {orders}, "
                f"протоколов {protocols}, сертификатов {certificates}, "
                f"заключений {conclusions}"
            )
        )

    # -- Вспомогательное -------------------------------------------------------
    def _author(self):
        from apps.accounts.models import User

        return User.objects.filter(role=Role.HR, is_active=True).first()

    def _docx(self, title: str, subtitle: str, table: Table) -> ContentFile:
        """Собирает файл документа тем же писателем, что и отчёты."""
        from django.conf import settings

        document = ReportDocument(
            title=title,
            subtitle=subtitle,
            organization=str(settings.ORGANIZATION_NAME),
            author=settings.SITE_NAME,
            generated_at=timezone.localtime(),
        )
        document.add_table(table)
        return ContentFile(render_docx(document), name="document.docx")

    # -- Общедоступные документы ------------------------------------------------
    def _create_policy(self, author) -> int:
        """Кладёт в хранилище положение о резерве и его прежнюю редакцию."""
        from apps.portal.services import regulation_document

        previous = store_document(
            self._docx(
                "Положение о кадровом резерве (редакция 2025 года)",
                "Утратило силу с введением новой редакции",
                Table(
                    title="Содержание",
                    columns=[
                        Column("section", "Раздел", width=24),
                        Column("text", "Текст", width=60),
                    ],
                    rows=[
                        {
                            "section": "Общие положения",
                            "text": "Редакция 2025 года. Заменена действующей редакцией.",
                        }
                    ],
                ),
            ),
            title="Положение о кадровом резерве (редакция 2025 года)",
            uploader=author,
            kind=DocumentKind.OTHER,
            access_level=AccessLevel.PUBLIC,
            description="Прежняя редакция. Сохранена как основание решений, принятых до отмены.",
        )

        source = regulation_document("reserve-policy")
        current = store_document(
            ContentFile(render_docx(source), name="policy.docx"),
            title="Положение о кадровом резерве",
            uploader=author,
            kind=DocumentKind.OTHER,
            access_level=AccessLevel.PUBLIC,
            description=(
                "Действующая редакция локального нормативного акта: порядок отбора, "
                "подготовки и исключения из кадрового резерва."
            ),
        )
        supersede(previous, current)
        return 2

    # -- Приказы ----------------------------------------------------------------
    def _create_orders(self, author) -> int:
        """Приказы о зачислении в резерв, привязанные к записям резерва."""
        from apps.reserve.models import ReserveEntry

        entries = list(ReserveEntry.objects.in_reserve().with_related())
        self.random.shuffle(entries)

        created = 0
        for entry in entries[: int(len(entries) * ORDER_SHARE)]:
            number = f"{self.random.randint(100, 899)}-к"
            table = Table(
                title="Зачислить в кадровый резерв",
                columns=[
                    Column("employee", "Работник", width=30),
                    Column("position", "Целевая должность", width=30),
                    Column("group", "Программа подготовки", width=24),
                    Column("date", "Дата зачисления", ValueKind.DATE, width=14),
                ],
                rows=[
                    {
                        "employee": entry.employee.full_name,
                        "position": str(entry.target_position),
                        "group": str(entry.reserve_group) if entry.reserve_group else "—",
                        "date": entry.inclusion_date,
                    }
                ],
                note="Основание: решение кадровой комиссии.",
            )
            store_document(
                self._docx(f"Приказ № {number} о зачислении в кадровый резерв", "", table),
                title=f"Приказ № {number} о зачислении в кадровый резерв",
                uploader=author,
                kind=DocumentKind.ORDER,
                obj=entry,
                employee=entry.employee,
                access_level=AccessLevel.RESTRICTED,
                description="Приказ о включении работника в кадровый резерв организации.",
            )
            created += 1
        return created

    # -- Протоколы --------------------------------------------------------------
    def _create_protocols(self, author) -> int:
        """Протоколы оценочных процедур, привязанные к кампаниям оценки."""
        from apps.assessment.models import AssessmentCampaign, SessionStatus

        created = 0
        for campaign in AssessmentCampaign.objects.all()[:6]:
            counted = campaign.sessions.filter(
                status__in=[SessionStatus.SUBMITTED, SessionStatus.APPROVED]
            ).count()
            table = Table(
                title="Сведения о процедуре",
                columns=[
                    Column("field", "Показатель", width=30),
                    Column("value", "Значение", width=40),
                ],
                rows=[
                    {"field": "Кампания", "value": campaign.name},
                    {
                        "field": "Оцениваемый период",
                        "value": (
                            f"{campaign.period_start:%d.%m.%Y} — {campaign.period_end:%d.%m.%Y}"
                        ),
                    },
                    {"field": "Модель компетенций", "value": str(campaign.competency_model)},
                    {"field": "Заполнено анкет", "value": str(counted)},
                ],
                note="Протокол составлен по итогам оценочной процедуры.",
            )
            store_document(
                self._docx(f"Протокол оценки: {campaign.name}", "", table),
                title=f"Протокол оценки: {campaign.name}",
                uploader=author,
                kind=DocumentKind.PROTOCOL,
                obj=campaign,
                access_level=AccessLevel.RESTRICTED,
                description="Протокол результатов кампании оценки компетенций.",
            )
            created += 1
        return created

    # -- Сертификаты -------------------------------------------------------------
    def _create_certificates(self, author) -> int:
        """Прикладывает файлы сертификатов к части подтверждений выполнения."""
        from apps.development.models import ActivityEvidence, EvidenceKind

        evidences = list(
            ActivityEvidence.objects.filter(kind=EvidenceKind.CERTIFICATE).select_related(
                "activity", "activity__plan__employee"
            )
        )
        self.random.shuffle(evidences)

        created = 0
        for evidence in evidences[: int(len(evidences) * CERTIFICATE_SHARE)]:
            activity = evidence.activity
            table = Table(
                title="Сертификат",
                columns=[
                    Column("field", "Показатель", width=26),
                    Column("value", "Значение", width=44),
                ],
                rows=[
                    {"field": "Выдан", "value": activity.plan.employee.full_name},
                    {"field": "Мероприятие", "value": activity.title},
                    {"field": "Вид развития", "value": activity.get_activity_type_display()},
                    {
                        "field": "Дата завершения",
                        "value": (
                            activity.completion_date.strftime("%d.%m.%Y")
                            if activity.completion_date
                            else "—"
                        ),
                    },
                ],
            )
            evidence.file.save(
                "certificate.docx",
                self._docx(f"Сертификат: {activity.title}", "", table),
                save=True,
            )
            created += 1
        return created

    # -- Заключения ----------------------------------------------------------------
    def _create_conclusions(self, author) -> int:
        """Конфиденциальные заключения кадровой службы о готовности резервистов."""
        from apps.reserve.models import ReserveEntry
        from core.constants import ReadinessLevel

        entries = list(
            ReserveEntry.objects.in_reserve()
            .filter(readiness_level=ReadinessLevel.HIGH)
            .with_related()[:12]
        )

        created = 0
        for entry in entries:
            table = Table(
                title="Заключение",
                columns=[
                    Column("field", "Показатель", width=28),
                    Column("value", "Значение", width=42),
                ],
                rows=[
                    {"field": "Работник", "value": entry.employee.full_name},
                    {"field": "Целевая должность", "value": str(entry.target_position)},
                    {"field": "Индекс готовности", "value": str(entry.readiness_index)},
                    {"field": "Уровень", "value": entry.get_readiness_level_display()},
                    {
                        "field": "Вывод",
                        "value": (
                            "Рекомендован к рассмотрению на ближайшем заседании кадровой комиссии."
                        ),
                    },
                ],
                note="Документ содержит оценочные суждения и не подлежит выдаче работнику.",
            )
            store_document(
                self._docx(
                    f"Заключение о готовности: {entry.employee.short_name}",
                    "Кадровая служба",
                    table,
                ),
                title=f"Заключение о готовности: {entry.employee.short_name}",
                uploader=author,
                kind=DocumentKind.REPORT,
                obj=entry,
                employee=entry.employee,
                access_level=AccessLevel.CONFIDENTIAL,
                description="Заключение кадровой службы о готовности резервиста к назначению.",
                valid_until=timezone.localdate() + dt.timedelta(days=180),
            )
            created += 1
        return created
