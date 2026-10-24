"""
Наполнение каталога отчётов.

Запуск::

    uv run python manage.py seed_reporting
    uv run python manage.py seed_reporting --flush
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.reporting.datasets import BUILDERS
from apps.reporting.models import ReportCategory, ReportFormat, ReportTemplate
from core.constants import Role

#: Описание периода — самый частый параметр отчётности.
PERIOD_SCHEMA: dict[str, Any] = {
    "date_from": {"type": "date", "label": "Период с", "initial": "year_start"},
    "date_to": {"type": "date", "label": "Период по", "initial": "today"},
}

TEMPLATES: list[dict[str, Any]] = [
    {
        "code": "reserve-roster",
        "name": "Состав кадрового резерва",
        "description": (
            "Полный перечень резервистов с целевыми должностями, группами резерва, "
            "достигнутым индексом и уровнем готовности. Основная выгрузка кадровой "
            "службы для сверки состава и подготовки к заседанию комиссии."
        ),
        "category": ReportCategory.RESERVE,
        "output_format": ReportFormat.XLSX,
        "builder": "reserve_roster",
        "parameters_schema": {
            "reserve_group": {
                "type": "choice",
                "label": "Группа резерва",
                "source": "reserve_group",
            },
            "readiness": {
                "type": "choice",
                "label": "Уровень готовности",
                "source": "readiness",
            },
        },
        "allowed_roles": [Role.ADMIN, Role.HR],
    },
    {
        "code": "readiness-ranking",
        "name": "Рейтинг готовности резервистов",
        "description": (
            "Ранжирование по композитному индексу с разбором по частным критериям и "
            "долей веса, обеспеченной данными. Материал для кадровой комиссии: "
            "ранжирование без разбора невозможно ни обсудить, ни оспорить."
        ),
        "category": ReportCategory.ANALYTICS,
        "output_format": ReportFormat.XLSX,
        "builder": "readiness_ranking",
        "parameters_schema": {},
        "allowed_roles": [Role.ADMIN, Role.HR],
        "is_async": True,
    },
    {
        "code": "succession-coverage",
        "name": "Обеспеченность критичных должностей",
        "description": (
            "Покрытие, глубина скамейки и немедленная готовность по критичным "
            "должностям, с отдельным перечнем должностей без преемников. "
            "Документ для доклада руководству организации."
        ),
        "category": ReportCategory.SUCCESSION,
        "output_format": ReportFormat.DOCX,
        "builder": "succession_coverage",
        "parameters_schema": {},
        "allowed_roles": [Role.ADMIN, Role.HR],
        "header_note": (
            "Покрытие считается по действующим планам преемственности: должность с "
            "отменённым планом обеспечена не лучше должности без плана."
        ),
    },
    {
        "code": "appointments-log",
        "name": "Назначения на должности",
        "description": (
            "Назначения за период с указанием источника кандидата и доли назначений "
            "из резерва — итогового показателя работы всей системы подготовки."
        ),
        "category": ReportCategory.SUCCESSION,
        "output_format": ReportFormat.XLSX,
        "builder": "appointments_log",
        "parameters_schema": dict(PERIOD_SCHEMA),
        "allowed_roles": [Role.ADMIN, Role.HR],
    },
    {
        "code": "development-progress",
        "name": "Выполнение планов развития",
        "description": (
            "Состояние индивидуальных планов: выполнение по весам мероприятий, "
            "число просроченных задач, кураторы. Просрочка считается по дате, "
            "а не по хранимому состоянию мероприятия."
        ),
        "category": ReportCategory.DEVELOPMENT,
        "output_format": ReportFormat.XLSX,
        "builder": "development_progress",
        "parameters_schema": {
            "status": {"type": "choice", "label": "Состояние плана", "source": "plan_status"},
        },
        "allowed_roles": [Role.ADMIN, Role.HR, Role.MANAGER],
    },
    {
        "code": "mentorship-report",
        "name": "Наставничество",
        "description": (
            "Пары «наставник — подопечный»: срок сопровождения, число проведённых "
            "встреч рядом с плановым ритмом и оценка эффективности."
        ),
        "category": ReportCategory.DEVELOPMENT,
        "output_format": ReportFormat.DOCX,
        "builder": "mentorship_report",
        "parameters_schema": {},
        "allowed_roles": [Role.ADMIN, Role.HR, Role.MANAGER, Role.MENTOR],
    },
    {
        "code": "assessment-summary",
        "name": "Итоги кампаний оценки",
        "description": (
            "Охват и завершённость кампаний за период: где процедура буксует, "
            "видно до истечения срока сдачи анкет."
        ),
        "category": ReportCategory.ASSESSMENT,
        "output_format": ReportFormat.XLSX,
        "builder": "assessment_summary",
        "parameters_schema": dict(PERIOD_SCHEMA),
        "allowed_roles": [Role.ADMIN, Role.HR],
    },
    {
        "code": "nine-box-distribution",
        "name": "Распределение по матрице 9-box",
        "description": (
            "Заполненность девяти ячеек матрицы «результативность — потенциал» "
            "с показателем концентрации: он отвечает на вопрос, различает ли "
            "оценка сотрудников вообще."
        ),
        "category": ReportCategory.ANALYTICS,
        "output_format": ReportFormat.PDF,
        "builder": "nine_box_distribution",
        "parameters_schema": {},
        "allowed_roles": [Role.ADMIN, Role.HR],
    },
    {
        "code": "reserve-health",
        "name": "Здоровье кадрового резерва",
        "description": (
            "Сводка показателей состояния резерва для доклада: численность, доля "
            "готовых, обеспеченность критичных должностей и доля назначений из "
            "резерва. Каждый показатель сопровождён пояснением."
        ),
        "category": ReportCategory.ANALYTICS,
        "output_format": ReportFormat.PDF,
        "builder": "reserve_health",
        "parameters_schema": {},
        "allowed_roles": [Role.ADMIN, Role.HR],
    },
    {
        "code": "unit-summary",
        "name": "Сводка по подразделению",
        "description": (
            "Состав подразделения, участие сотрудников в резерве, достигнутая "
            "готовность и ход планов развития — в одном документе вместо четырёх "
            "страниц интерфейса."
        ),
        "category": ReportCategory.PERSONNEL,
        "output_format": ReportFormat.CSV,
        "builder": "unit_summary",
        "parameters_schema": {},
        "allowed_roles": [Role.ADMIN, Role.HR, Role.MANAGER],
    },
]


class Command(BaseCommand):
    """Создаёт каталог отчётов."""

    help = "Наполняет каталог шаблонов отчётов"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Удалить существующие шаблоны перед наполнением",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        if options["flush"]:
            removed, _details = ReportTemplate.objects.all().delete()
            self.stdout.write(f"Удалено записей каталога: {removed}")

        created = updated = 0
        for spec in TEMPLATES:
            # Ссылка на несуществующий сборщик означала бы шаблон, который невозможно запустить.
            if spec["builder"] not in BUILDERS:
                raise ValueError(f"Сборщик не зарегистрирован: {spec['builder']}")

            code = spec.pop("code")
            _template, is_new = ReportTemplate.objects.update_or_create(code=code, defaults=spec)
            spec["code"] = code
            created += int(is_new)
            updated += int(not is_new)

        self.stdout.write(
            self.style.SUCCESS(f"Каталог отчётов: создано {created}, обновлено {updated}")
        )
