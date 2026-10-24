"""Интеграционные тесты организационной структуры."""

from __future__ import annotations

import pytest

from django.core.cache import cache
from django.db import IntegrityError

from apps.org.models import Branch, Department, Position
from tests.factories import BranchFactory, DepartmentFactory, PositionFactory, PositionGradeFactory

pytestmark = [pytest.mark.django_db, pytest.mark.integration]


class TestDepartmentHierarchy:
    """Материализованный путь и обход дерева подразделений."""

    def test_root_department_has_zero_depth(self, department):
        assert department.depth == 0
        assert department.is_root is True

    def test_child_extends_parent_path(self, department, child_department):
        assert child_department.path.startswith(department.path + ".")
        assert child_department.depth == 1

    def test_three_level_hierarchy(self, branch):
        top = DepartmentFactory(name="Департамент", branch=branch)
        middle = DepartmentFactory(name="Управление", branch=branch, parent=top)
        bottom = DepartmentFactory(name="Отдел", branch=branch, parent=middle)

        assert bottom.depth == 2
        assert bottom.path.startswith(middle.path + ".")
        assert middle.path.startswith(top.path + ".")

    def test_descendants_include_self(self, department, child_department):
        descendants = Department.objects.descendants_of(department)
        ids = set(descendants.values_list("id", flat=True))

        assert department.pk in ids
        assert child_department.pk in ids

    def test_descendants_exclude_siblings(self, department, child_department, other_department):
        ids = set(Department.objects.descendants_of(department).values_list("id", flat=True))
        assert other_department.pk not in ids

    def test_ancestors_ordered_from_root(self, branch):
        top = DepartmentFactory(name="Департамент", branch=branch)
        middle = DepartmentFactory(name="Управление", branch=branch, parent=top)
        bottom = DepartmentFactory(name="Отдел", branch=branch, parent=middle)

        ancestors = list(Department.objects.ancestors_of(bottom))
        assert [d.pk for d in ancestors] == [top.pk, middle.pk]

    def test_full_path_name(self, branch):
        top = DepartmentFactory(name="Департамент разработки", branch=branch)
        child = DepartmentFactory(name="Отдел серверной разработки", branch=branch, parent=top)

        assert child.full_path_name == "Департамент разработки / Отдел серверной разработки"

    def test_descendant_ids_uses_cache(self, department, child_department):
        """Повторный вызов не обращается к базе данных."""
        cache.clear()

        first = Department.objects.descendant_ids(department.pk)
        cached = cache.get(f"dept:descendants:{department.pk}")

        assert cached == first
        assert child_department.pk in first

    def test_cache_invalidated_on_change(self, department, child_department):
        """Изменение структуры сбрасывает кеш поддерева."""
        Department.objects.descendant_ids(department.pk)
        assert cache.get(f"dept:descendants:{department.pk}") is not None

        child_department.name = "Переименованный отдел"
        child_department.save()

        assert cache.get(f"dept:descendants:{child_department.pk}") is None


class TestDepartmentMove:
    """Перенос подразделения в другую ветвь дерева."""

    def test_subtree_paths_recalculated(self, branch):
        """При переносе узла пути всех потомков обновляются."""
        old_parent = DepartmentFactory(name="Прежний департамент", branch=branch)
        new_parent = DepartmentFactory(name="Новый департамент", branch=branch)

        node = DepartmentFactory(name="Отдел", branch=branch, parent=old_parent)
        leaf = DepartmentFactory(name="Группа", branch=branch, parent=node)

        node.parent = new_parent
        node.save()

        node.refresh_from_db()
        leaf.refresh_from_db()

        assert node.path.startswith(new_parent.path + ".")
        assert leaf.path.startswith(node.path + ".")
        assert not leaf.path.startswith(old_parent.path + ".")

    def test_moved_subtree_visible_from_new_parent(self, branch):
        old_parent = DepartmentFactory(name="Прежний", branch=branch)
        new_parent = DepartmentFactory(name="Новый", branch=branch)
        node = DepartmentFactory(name="Отдел", branch=branch, parent=old_parent)

        node.parent = new_parent
        node.save()

        ids = set(Department.objects.descendants_of(new_parent).values_list("id", flat=True))
        assert node.pk in ids


class TestBranch:
    """Ограничения филиалов."""

    def test_single_head_office_allowed(self, db):
        BranchFactory(code="head", name="Головной офис", is_head_office=True)

        with pytest.raises(IntegrityError):
            BranchFactory(code="second-head", name="Второй головной", is_head_office=True)

    def test_multiple_regular_branches_allowed(self, db):
        BranchFactory(code="spb", city="Санкт-Петербург")
        BranchFactory(code="nsk", city="Новосибирск")

        assert Branch.objects.filter(is_head_office=False).count() == 2

    def test_head_office_sorted_first(self, db):
        BranchFactory(code="nsk", name="Филиал Новосибирск")
        head = BranchFactory(code="msk", name="Головной офис", is_head_office=True)

        assert Branch.objects.first() == head


class TestPosition:
    """Должности и грейды."""

    def test_promotion_detected_by_grade_level(self, department):
        junior = PositionFactory(
            code="dev-middle",
            name="Middle Developer",
            grade=PositionGradeFactory(code="g5", name="Специалист", level=5),
            department=department,
        )
        senior = PositionFactory(
            code="dev-lead",
            name="Team Lead",
            grade=PositionGradeFactory(code="g8", name="Руководитель", level=8, is_managerial=True),
            department=department,
        )

        assert senior.is_promotion_from(junior) is True
        assert junior.is_promotion_from(senior) is False

    def test_key_positions_filter(self, department, grade):
        key = PositionFactory(
            code="key-pos",
            name="Руководитель направления",
            grade=grade,
            department=department,
            is_key_position=True,
        )
        PositionFactory(
            code="regular-pos",
            name="Специалист",
            grade=PositionGradeFactory(code="g3", name="Младший", level=3),
            department=department,
        )

        assert list(Position.objects.key_positions()) == [key]

    def test_headcount_plan_stored(self, position):
        """Плановая численность хранится на должности."""
        position.headcount_plan = 3
        position.save()
        position.refresh_from_db()

        assert position.headcount_plan == 3

    def test_grade_level_is_unique(self, db):
        PositionGradeFactory(code="g10", name="Директор", level=10)

        with pytest.raises(IntegrityError):
            PositionGradeFactory(code="g10-dup", name="Другой", level=10)
