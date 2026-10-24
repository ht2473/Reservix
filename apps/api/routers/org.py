"""Методы организационной структуры: филиалы, подразделения, должности."""

from ninja import Query, Router, Status
from ninja.pagination import paginate

from django.db.models import Count, Q
from django.http import HttpRequest
from django.shortcuts import get_object_or_404

from apps.api.schemas import (
    BranchSchema,
    DepartmentSchema,
    DepartmentTreeSchema,
    ErrorSchema,
    PositionGradeSchema,
    PositionProfileSchema,
    PositionSchema,
)
from apps.org.models import Branch, Department, Position, PositionGrade, PositionProfile

router = Router(tags=["Организационная структура"])


@router.get(
    "/branches",
    response=list[BranchSchema],
    summary="Филиалы",
    description="Перечень филиалов организации с часовыми поясами.",
)
def list_branches(request: HttpRequest, active: bool | None = None) -> list[Branch]:
    """Возвращает филиалы, при необходимости отбирая только действующие."""
    queryset = Branch.objects.all()
    if active is not None:
        queryset = queryset.filter(is_active=active)
    return list(queryset)


@router.get(
    "/departments",
    response=list[DepartmentSchema],
    summary="Подразделения",
    description=(
        "Плоский перечень подразделений с материализованным путём. Путь "
        "позволяет потребителю восстановить иерархию без дополнительных "
        "запросов: подразделения одной ветви имеют общий префикс."
    ),
)
def list_departments(
    request: HttpRequest,
    branch: int | None = None,
    parent: int | None = None,
    active: bool | None = None,
) -> list[Department]:
    """Возвращает подразделения с отбором по филиалу и родительскому узлу."""
    queryset = Department.objects.all()
    if branch is not None:
        queryset = queryset.filter(branch_id=branch)
    if parent is not None:
        queryset = queryset.filter(parent_id=parent)
    if active is not None:
        queryset = queryset.filter(is_active=active)
    return list(queryset)


@router.get(
    "/departments/tree",
    response=list[DepartmentTreeSchema],
    summary="Дерево подразделений",
    description=(
        "Иерархия подразделений с численностью сотрудников в каждом узле. "
        "Дерево собирается одним запросом: подразделения читаются в порядке "
        "материализованного пути, поэтому родитель всегда встречается раньше "
        "своих детей и вложение строится за один проход."
    ),
)
def department_tree(request: HttpRequest, branch: int | None = None) -> list[dict[str, object]]:
    """Строит дерево подразделений с численностью персонала."""
    queryset = (
        Department.objects.filter(is_active=True)
        .annotate(
            headcount=Count(
                "employees",
                filter=Q(employees__status__in=["active", "probation"]),
                distinct=True,
            )
        )
        .order_by("path")
    )
    if branch is not None:
        queryset = queryset.filter(branch_id=branch)

    nodes: dict[int, dict[str, object]] = {}
    roots: list[dict[str, object]] = []

    for department in queryset:
        node: dict[str, object] = {
            "id": department.pk,
            "code": department.code,
            "name": department.name,
            "branch_id": department.branch_id,
            "depth": department.depth,
            "headcount": department.headcount,
            "children": [],
        }
        nodes[department.pk] = node

        parent = nodes.get(department.parent_id) if department.parent_id else None
        if parent is None:
            roots.append(node)
        else:
            children = parent["children"]
            assert isinstance(children, list)
            children.append(node)

    return roots


@router.get(
    "/grades",
    response=list[PositionGradeSchema],
    summary="Грейды",
    description="Уровни должностей, задающие вертикаль организации.",
)
def list_grades(request: HttpRequest) -> list[PositionGrade]:
    """Возвращает грейды в порядке возрастания уровня."""
    return list(PositionGrade.objects.all())


@router.get(
    "/positions",
    response=list[PositionSchema],
    summary="Должности",
    description="Штатное расписание с указанием грейда и признака критичности.",
)
@paginate
def list_positions(
    request: HttpRequest,
    branch: int | None = Query(default=None),
    department: int | None = Query(default=None),
    key_only: bool = Query(default=False),
    search: str = Query(default=""),
):
    """Возвращает должности с отбором по подразделению и критичности."""
    queryset = Position.objects.select_related("grade", "department").order_by(
        "-grade__level", "name"
    )
    if branch is not None:
        queryset = queryset.filter(department__branch_id=branch)
    if department is not None:
        queryset = queryset.filter(department_id=department)
    if key_only:
        queryset = queryset.filter(is_key_position=True)
    if search:
        queryset = queryset.filter(Q(name__icontains=search) | Q(code__icontains=search))
    return queryset


@router.get(
    "/positions/{int:position_id}/profile",
    response={200: PositionProfileSchema, 404: ErrorSchema},
    summary="Профиль должности",
    description=(
        "Формальные требования к кандидату на должность. Отсутствие профиля — "
        "не ошибка данных, а состояние, требующее внимания кадровой службы: "
        "без профиля соответствие кандидата должности не рассчитывается."
    ),
)
def position_profile(request: HttpRequest, position_id: int):
    """Возвращает профиль должности либо пояснение о его отсутствии."""
    position = get_object_or_404(Position, pk=position_id)
    profile = PositionProfile.objects.filter(position=position).first()
    if profile is None:
        return Status(
            404,
            {
                "code": "profile_not_defined",
                "detail": f"Для должности «{position.name}» профиль требований не составлен",
            },
        )
    return Status(200, profile)
