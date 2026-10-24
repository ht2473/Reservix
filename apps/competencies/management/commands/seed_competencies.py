"""
Развёртывание корпоративной модели компетенций.

Запуск::

    uv run python manage.py seed_competencies
    uv run python manage.py seed_competencies --dry-run   # показать план
    uv run python manage.py seed_competencies --no-merge  # без сведения дублей
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from apps.assessment.models import AssessmentScore
from apps.competencies.dictionary import (
    CATEGORIES,
    COMPETENCIES,
    LEGACY_ALIASES,
    LEVEL_NAMES,
    MODELS,
    ModelSpec,
)
from apps.competencies.models import (
    Competency,
    CompetencyCategory,
    CompetencyModel,
    CompetencyRequirement,
    CompetencyScaleLevel,
)
from apps.org.models import Position, PositionGrade
from core.settings_types import get_reservix_settings

#: Версия модели компетенций, создаваемой командой.
MODEL_VERSION = 1

#: Код служебной модели, созданной при переносе исходного дампа. Она выводится
#: из обращения, как только появляется корпоративная модель компетенций.
LEGACY_MODEL_CODE = "legacy-imported-model"


class Command(BaseCommand):
    """Создаёт корпоративную модель компетенций и сводит наименования дампа."""

    help = "Наполняет справочник компетенций и модели требований к должностям"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="показать, что будет сделано, не изменяя базу",
        )
        parser.add_argument(
            "--no-merge",
            action="store_true",
            help="не сводить наименования исходной базы к корпоративной модели",
        )
        parser.add_argument(
            "--no-publish",
            action="store_true",
            help="оставить созданные модели компетенций черновиками",
        )

    def handle(self, *args, **options) -> None:
        """Выполняет наполнение целиком в одной транзакции."""
        self.dry_run: bool = options["dry_run"]
        self.verbose: int = options["verbosity"]

        with transaction.atomic():
            categories = self._create_categories()
            competencies = self._create_competencies(categories)
            scale_levels = self._create_scale_levels(competencies)

            merged = 0
            moved_scores = 0
            if not options["no_merge"]:
                merged, moved_scores = self._merge_legacy(competencies)
                self._retire_legacy_model()
                self._retire_empty_categories()

            models = self._create_models(competencies, publish=not options["no_publish"])
            removed = self._remove_obsolete_models(models)
            assigned = self._assign_positions(models)

            # Итоговые показатели считаются внутри транзакции: при пробном запуске изменения
            # откатываются, и подсчёт после отката показал бы состояние до наполнения — то есть
            # ровно то, чего пользователь проверить не хотел.
            summary = {
                "categories": len(categories),
                "competencies": len(competencies),
                "scale_levels": scale_levels,
                "merged": merged,
                "moved_scores": moved_scores,
                "models": len(models),
                "removed": removed,
                "assigned": assigned,
                "total": Competency.objects.count(),
                "incomplete": (
                    Competency.objects.annotate(levels=Count("scale_levels", distinct=True))
                    .exclude(levels=Competency.expected_scale_size())
                    .count()
                ),
            }

            if self.dry_run:
                transaction.set_rollback(True)

        self._report(**summary)

    # -- Справочник ---------------------------------------------------------
    def _create_categories(self) -> dict[str, CompetencyCategory]:
        """Создаёт или обновляет категории справочника."""
        result: dict[str, CompetencyCategory] = {}
        for order, spec in enumerate(CATEGORIES, start=1):
            category, _created = CompetencyCategory.objects.update_or_create(
                code=spec.code,
                defaults={
                    "name": spec.name,
                    "kind": spec.kind,
                    "color": spec.color,
                    "description": spec.description,
                    "sort_order": order * 10,
                    "is_active": True,
                },
            )
            result[spec.code] = category
        return result

    def _create_competencies(
        self, categories: dict[str, CompetencyCategory]
    ) -> dict[str, Competency]:
        """Создаёт компетенции корпоративной модели."""
        result: dict[str, Competency] = {}
        for order, spec in enumerate(COMPETENCIES, start=1):
            category = categories[spec.category]
            competency, _created = Competency.objects.update_or_create(
                code=spec.code,
                defaults={
                    "name": spec.name,
                    "category": category,
                    "kind": spec.resolved_kind(category.kind),
                    "description": spec.definition,
                    "definition": spec.definition,
                    "development_hint": spec.development_hint,
                    "is_core": spec.is_core,
                    "is_testable": spec.is_testable,
                    "sort_order": order * 10,
                    "is_active": True,
                },
            )
            result[spec.code] = competency
        return result

    def _create_scale_levels(self, competencies: dict[str, Competency]) -> int:
        """Создаёт поведенческие индикаторы по всем уровням шкалы."""
        options = get_reservix_settings()
        minimum = options["COMPETENCY_SCALE_MIN"]
        maximum = options["COMPETENCY_SCALE_MAX"]

        created = 0
        for spec in COMPETENCIES:
            competency = competencies[spec.code]
            for level in range(minimum, maximum + 1):
                index = level - minimum
                if index >= len(spec.indicators):
                    break
                CompetencyScaleLevel.objects.update_or_create(
                    competency=competency,
                    level=level,
                    defaults={
                        "name": LEVEL_NAMES[min(index, len(LEVEL_NAMES) - 1)],
                        "indicators": spec.indicators[index],
                    },
                )
                created += 1
        return created

    # -- Сведение наименований исходной базы --------------------------------
    def _merge_legacy(self, competencies: dict[str, Competency]) -> tuple[int, int]:
        """Переносит оценки со старых наименований на компетенции модели."""
        merged = 0
        moved = 0

        for legacy_code, target_code in LEGACY_ALIASES.items():
            if legacy_code == target_code:
                # Наименование совпадает с кодом корпоративной компетенции:
                # запись уже переиспользована при создании справочника.
                continue

            legacy = Competency.objects.filter(code=legacy_code).first()
            if legacy is None:
                continue

            target = competencies.get(target_code)
            if target is None:  # pragma: no cover - защита от рассогласования данных
                self.stderr.write(f"Не найдена компетенция-приёмник {target_code}")
                continue

            moved += self._move_scores(legacy, target)
            self._move_requirements(legacy, target)
            legacy.scale_levels.all().delete()
            legacy.delete()
            merged += 1

            if self.verbose >= 2:
                self.stdout.write(f"  {legacy_code} → {target_code}")

        return merged, moved

    def _move_scores(self, legacy: Competency, target: Competency) -> int:
        """Переносит оценки на компетенцию-приёмник."""
        conflicting = set(
            AssessmentScore.objects.filter(competency=target).values_list("session_id", flat=True)
        )
        moved = 0

        for score in AssessmentScore.objects.filter(competency=legacy).select_related("session"):
            if score.session_id in conflicting:
                existing = AssessmentScore.objects.get(
                    session_id=score.session_id, competency=target
                )
                if score.level > existing.level:
                    existing.level = score.level
                    existing.raw_score = None
                    existing.save(update_fields=["level", "raw_score", "updated_at"])
                score.delete()
                continue

            score.competency = target
            score.save(update_fields=["competency", "updated_at"])
            conflicting.add(score.session_id)
            moved += 1

        return moved

    def _move_requirements(self, legacy: Competency, target: Competency) -> None:
        """Переносит требования моделей на компетенцию-приёмник."""
        for requirement in CompetencyRequirement.objects.filter(competency=legacy):
            existing = CompetencyRequirement.objects.filter(
                competency_model_id=requirement.competency_model_id, competency=target
            ).first()
            if existing is None:
                requirement.competency = target
                requirement.save(update_fields=["competency", "updated_at"])
                continue

            existing.target_level = max(existing.target_level, requirement.target_level)
            existing.weight += requirement.weight
            existing.is_critical = existing.is_critical or requirement.is_critical
            existing.save(update_fields=["target_level", "weight", "is_critical", "updated_at"])
            requirement.delete()

    def _retire_legacy_model(self) -> None:
        """Выводит из обращения служебную модель, созданную при переносе дампа."""
        legacy = CompetencyModel.objects.filter(code=LEGACY_MODEL_CODE).first()
        if legacy is None or not legacy.is_active:
            return

        legacy.is_active = False
        legacy.is_published = False
        legacy.description = (
            f"{legacy.description}\n\n"
            "Модель выведена из обращения при развёртывании корпоративной "
            "модели компетенций. Сохранена для объяснения оценок, "
            "перенесённых из исходной базы данных."
        ).strip()
        legacy.save(update_fields=["is_active", "is_published", "description", "updated_at"])

    def _retire_empty_categories(self) -> None:
        """Выводит из обращения категории, оставшиеся без компетенций."""
        empty = (
            CompetencyCategory.objects.filter(is_active=True)
            .annotate(competency_count=Count("competencies"))
            .filter(competency_count=0)
            .exclude(code__in=[spec.code for spec in CATEGORIES])
        )
        for category in empty:
            category.is_active = False
            category.save(update_fields=["is_active", "updated_at"])

    # -- Модели требований --------------------------------------------------
    def _create_models(
        self, competencies: dict[str, Competency], *, publish: bool
    ) -> dict[str, CompetencyModel]:
        """Создаёт модели компетенций вместе с требованиями."""
        grades = {grade.code: grade for grade in PositionGrade.objects.all()}
        result: dict[str, CompetencyModel] = {}

        for spec in MODELS:
            competency_model, _created = CompetencyModel.objects.update_or_create(
                code=spec.code,
                defaults={
                    "name": spec.name,
                    "description": spec.description,
                    "version": MODEL_VERSION,
                    "grade": grades.get(spec.grade_code),
                    "is_published": publish,
                    "is_active": True,
                },
            )

            # Требования пересоздаются целиком: изменённый состав модели не должен оставлять в базе
            # требования, которых в описании больше нет.
            competency_model.requirements.all().delete()
            CompetencyRequirement.objects.bulk_create(
                [
                    CompetencyRequirement(
                        competency_model=competency_model,
                        competency=competencies[requirement.competency],
                        target_level=requirement.target,
                        minimum_level=requirement.minimum,
                        weight=Decimal(str(requirement.weight)),
                        is_critical=requirement.is_critical,
                    )
                    for requirement in spec.requirements
                ]
            )
            result[spec.code] = competency_model

        return result

    def _remove_obsolete_models(self, models: dict[str, CompetencyModel]) -> int:
        """Убирает модели прежних редакций корпоративного справочника."""
        obsolete = CompetencyModel.objects.filter(code__regex=r"^(tier|func)-").exclude(
            code__in=models.keys()
        )

        removed = 0
        for competency_model in obsolete:
            if competency_model.sessions.exists():
                competency_model.is_active = False
                competency_model.is_published = False
                competency_model.save(update_fields=["is_active", "is_published", "updated_at"])
            else:
                competency_model.requirements.all().delete()
                competency_model.positions.clear()
                competency_model.delete()
            removed += 1

        return removed

    def _assign_positions(self, models: dict[str, CompetencyModel]) -> int:
        """Назначает модели должностям."""
        functional = [spec for spec in MODELS if spec.is_functional]
        tiers = [spec for spec in MODELS if not spec.is_functional]

        assignment: dict[str, list[int]] = {spec.code: [] for spec in MODELS}
        unassigned = 0

        for position in Position.objects.select_related("grade", "department"):
            level = position.grade.level if position.grade_id else 1
            department = position.department
            department_code = department.code if department is not None else ""

            spec = self._functional_for(functional, department_code, position.code, level)
            if spec is None:
                spec = self._tier_for(tiers, level)

            if spec is None:  # pragma: no cover - грейды покрывают весь диапазон
                unassigned += 1
                continue
            assignment[spec.code].append(position.pk)

        assigned = 0
        for code, position_ids in assignment.items():
            models[code].positions.set(position_ids)
            assigned += len(position_ids)

        if unassigned and self.verbose >= 1:
            self.stderr.write(f"Должностей без модели компетенций: {unassigned}")
        return assigned

    @staticmethod
    def _functional_for(
        specs: list[ModelSpec], department_code: str, position_code: str, level: int
    ) -> ModelSpec | None:
        """Функциональная модель, применимая к должности."""
        for spec in specs:
            low, high = spec.grade_range
            if not low <= level <= high:
                continue
            if position_code in spec.positions or department_code in spec.departments:
                return spec
        return None

    @staticmethod
    def _tier_for(specs: list[ModelSpec], level: int) -> ModelSpec | None:
        """Уровневая модель, покрывающая уровень грейда."""
        for spec in specs:
            low, high = spec.grade_range
            if low <= level <= high:
                return spec
        return None

    # -- Отчёт --------------------------------------------------------------
    def _report(self, **stats: Any) -> None:
        """Выводит итог наполнения."""
        if self.dry_run:
            self.stdout.write(self.style.WARNING("\nПробный запуск: база не изменена.\n"))
        else:
            self.stdout.write(self.style.SUCCESS("\nКорпоративная модель компетенций развёрнута."))

        self.stdout.write(
            f"  Категорий: {stats['categories']}\n"
            f"  Компетенций корпоративной модели: {stats['competencies']}\n"
            f"  Уровней шкалы: {stats['scale_levels']}\n"
            f"  Сведено наименований исходной базы: {stats['merged']}\n"
            f"  Перенесено оценок: {stats['moved_scores']}\n"
            f"  Моделей требований: {stats['models']}\n"
            f"  Должностей с назначенной моделью: {stats['assigned']}\n"
            f"  Всего компетенций в справочнике: {stats['total']}\n"
            f"  Компетенций с неполной шкалой: {stats['incomplete']}"
        )
