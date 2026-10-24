"""Методы справочника компетенций, моделей требований и профилей соответствия."""

import uuid

from ninja import Query, Router, Status
from ninja.pagination import paginate

from django.db.models import Q
from django.http import HttpRequest
from django.shortcuts import get_object_or_404

from apps.api.auth import current_user
from apps.api.schemas import (
    CompetencyCategorySchema,
    CompetencyModelSchema,
    CompetencyProfileSchema,
    CompetencyScaleLevelSchema,
    CompetencySchema,
    ErrorSchema,
)
from apps.competencies.models import Competency, CompetencyCategory, CompetencyModel
from apps.competencies.services import build_profile
from apps.people.models import Employee
from core.permissions import apply_employee_scope, can_view_employee

router = Router(tags=["Компетенции"])


@router.get(
    "/categories",
    response=list[CompetencyCategorySchema],
    summary="Категории компетенций",
    description="Разделы справочника, задающие оси лепестковой диаграммы.",
)
def list_categories(request: HttpRequest, active: bool | None = None) -> list[CompetencyCategory]:
    """Возвращает категории справочника компетенций."""
    queryset = CompetencyCategory.objects.all()
    if active is not None:
        queryset = queryset.filter(is_active=active)
    return list(queryset)


@router.get(
    "/competencies",
    response=list[CompetencySchema],
    summary="Компетенции",
    description=(
        "Справочник компетенций с определениями. Поведенческие индикаторы "
        "уровней не передаются: они объёмны и нужны при заполнении формы "
        "оценки, а не при интеграции."
    ),
)
@paginate
def list_competencies(
    request: HttpRequest,
    category: int | None = Query(default=None),
    kind: str | None = Query(default=None),
    testable: bool | None = Query(default=None),
    search: str = Query(default=""),
):
    """Возвращает компетенции с отбором по категории и типу."""
    queryset = Competency.objects.with_category()
    if category is not None:
        queryset = queryset.filter(category_id=category)
    if kind:
        queryset = queryset.filter(kind=kind)
    if testable is not None:
        queryset = queryset.filter(is_testable=testable)
    if search:
        queryset = queryset.filter(Q(name__icontains=search) | Q(code__icontains=search))
    return queryset


@router.get(
    "/models",
    response=list[CompetencyModelSchema],
    summary="Модели компетенций",
    description=(
        "Наборы требований к должностям с целевыми уровнями и весами. Модель — "
        "эталон, относительно которого считается компетентностное соответствие "
        "в индексе готовности."
    ),
)
def list_models(
    request: HttpRequest,
    position: int | None = Query(default=None),
    published: bool | None = Query(default=None),
) -> list[CompetencyModel]:
    """Возвращает модели компетенций вместе с требованиями."""
    queryset = CompetencyModel.objects.with_requirements()
    if position is not None:
        queryset = queryset.for_position(position)
    if published is not None:
        queryset = queryset.filter(is_published=published)
    return list(queryset)


@router.get(
    "/models/{int:model_id}",
    response=CompetencyModelSchema,
    summary="Модель компетенций",
)
def model_detail(request: HttpRequest, model_id: int) -> CompetencyModel:
    """Возвращает модель компетенций с полным перечнем требований."""
    return get_object_or_404(CompetencyModel.objects.with_requirements(), pk=model_id)


@router.get(
    "/competencies/{int:competency_id}/scale",
    response=list[CompetencyScaleLevelSchema],
    summary="Шкала компетенции",
    description=(
        "Поведенческие индикаторы уровней владения. Именно они делают оценку "
        "воспроизводимой: два эксперта, читающие одно описание уровня, ставят "
        "близкие баллы."
    ),
)
def competency_scale(request: HttpRequest, competency_id: int):
    """Возвращает уровни шкалы компетенции с индикаторами."""
    competency = get_object_or_404(Competency, pk=competency_id)
    return list(competency.scale_levels.order_by("level"))


@router.get(
    "/profiles/{uuid:employee_id}",
    response={200: CompetencyProfileSchema, 404: ErrorSchema},
    summary="Профиль компетенций сотрудника",
    description=(
        "Соответствие сотрудника требованиям должности с расшифровкой по "
        "компетенциям. Параметр `model` позволяет сопоставить сотрудника с "
        "требованиями другой должности — так оценивается готовность к "
        "назначению."
    ),
)
def employee_profile(
    request: HttpRequest,
    employee_id: uuid.UUID,
    model: str = Query(default=""),
):
    """Возвращает профиль компетенций в границах прав вызывающего."""
    user = current_user(request)
    employee = (
        apply_employee_scope(Employee.objects.with_org(), user)
        .filter(public_id=employee_id)
        .first()
    )

    # Отсутствие права и отсутствие записи намеренно не различаются: ответ
    # «нет прав» сам по себе подтверждал бы, что такой сотрудник существует.
    if employee is None or not can_view_employee(user, employee):
        return Status(404, {"code": "not_found", "detail": "Сотрудник не найден"})

    competency_model = (
        CompetencyModel.objects.published().filter(code=model).first() if model else None
    )
    profile = build_profile(employee, competency_model)

    return Status(
        200,
        {
            "employee_id": employee.public_id,
            "competency_model_id": (
                profile.competency_model.pk if profile.competency_model else None
            ),
            "competency_model_name": (
                profile.competency_model.name if profile.competency_model else None
            ),
            "match_score": profile.match_score,
            "coverage": profile.coverage,
            "confidence": profile.confidence,
            "average_level": profile.average_level,
            "average_target": profile.average_target,
            "is_blocked": profile.is_blocked,
            "evaluated_on": profile.evaluated_on,
            "gaps": [
                {
                    "competency_id": gap.requirement.competency_id,
                    "competency_name": gap.competency.name,
                    "category_name": gap.competency.category.name,
                    "target_level": gap.target,
                    "actual_level": gap.actual,
                    "gap": gap.gap,
                    "weight": gap.weight,
                    "attainment": round(gap.attainment, 3),
                    "is_critical": gap.requirement.is_critical,
                    "is_satisfied": gap.is_satisfied,
                    "is_blocking": gap.is_blocking,
                    "confidence": gap.measured.confidence if gap.measured else None,
                    "methods": gap.measured.method_count if gap.measured else 0,
                }
                for gap in profile.gaps
            ],
        },
    )
