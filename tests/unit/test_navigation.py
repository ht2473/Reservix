"""Модульные тесты навигации и «хлебных крошек»."""

from __future__ import annotations

import itertools

import pytest

from django.test import RequestFactory
from django.urls import reverse

from core.breadcrumbs import BreadcrumbTrail, build_trail
from core.constants import Role
from core.navigation import (
    ADMIN_MENU,
    EMPLOYEE_MENU,
    HR_MENU,
    PUBLIC_MENU,
    ROLE_HOME,
    ROLE_MENUS,
    NavItem,
    get_public_menu,
    get_workspace_menu,
)

pytestmark = pytest.mark.unit


class TestPublicMenu:
    """Состав публичного меню."""

    def test_all_items_have_labels(self):
        assert all(str(item.label).strip() for item in PUBLIC_MENU)

    def test_all_items_have_descriptions(self):
        """Описание используется в мобильном меню и на карте сайта."""
        assert all(str(item.description).strip() for item in PUBLIC_MENU)

    def test_all_items_have_icons(self):
        assert all(item.icon for item in PUBLIC_MENU)

    def test_url_names_are_unique(self):
        names = [item.url_name for item in PUBLIC_MENU]
        assert len(names) == len(set(names))

    def test_first_item_is_home(self):
        assert PUBLIC_MENU[0].url_name == "portal:home"


class TestWorkspaceMenus:
    """Меню рабочих мест по ролям."""

    def test_every_role_has_menu(self):
        for role in Role.values:
            assert role in ROLE_MENUS, f"Для роли {role} не задано меню"

    def test_admin_menu_restricted_to_admin(self):
        """Разделы администрирования недоступны прочим ролям."""
        for item in ADMIN_MENU:
            assert item.roles == frozenset({Role.ADMIN})

    def test_employee_menu_excludes_privileged_sections(self):
        """Сотрудник не должен видеть разделы управления резервом."""
        for item in EMPLOYEE_MENU:
            assert Role.EMPLOYEE in item.roles
            assert Role.HR not in item.roles


@pytest.mark.django_db
class TestNavigationCompleteness:
    """Полнота подключения разделов."""

    def test_every_menu_route_resolves(self):
        """Каждый пункт всех меню ссылается на подключённый маршрут."""
        unresolved = [
            f"{item.label} → {item.url_name}"
            for menu in (PUBLIC_MENU, *ROLE_MENUS.values())
            for item in menu
            if not item.url
        ]
        assert not unresolved, "Пункты меню без подключённого маршрута: " + "; ".join(unresolved)

    def test_every_role_home_resolves(self):
        """Стартовая страница каждой роли существует."""
        for role, url_name in ROLE_HOME.items():
            assert reverse(url_name), f"Стартовая страница роли {role} не разобрана"


class TestNavItemBehaviour:
    """Логика подсветки активного пункта."""

    def test_visible_for_anonymous_when_no_roles(self):
        item = NavItem("Главная", "portal:home")
        assert item.is_visible_for(None) is True

    def test_hidden_for_anonymous_when_roles_set(self):
        item = NavItem("Резерв", "reserve:list", roles=frozenset({Role.HR}))
        assert item.is_visible_for(None) is False

    def test_root_url_matches_strictly(self, db):
        """Корневой адрес не должен подсвечиваться на вложенных страницах."""
        item = NavItem("Главная", "portal:home")
        assert item.is_active("/") is True
        assert item.is_active("/about/") is False

    def test_section_active_on_nested_pages(self, db):
        """Раздел остаётся подсвеченным на вложенных страницах."""
        item = NavItem("О системе", "portal:about")
        assert item.is_active("/about/") is True
        assert item.is_active("/about/details/") is True
        assert item.is_active("/aboutus/") is False


@pytest.mark.django_db
class TestMenuBuilders:
    """Построение меню для конкретного запроса."""

    def test_public_menu_marks_active_item(self):
        request = RequestFactory().get("/about/")
        menu = get_public_menu(request)

        active = [item for item in menu if item["is_active"]]
        assert len(active) == 1
        assert active[0]["label"] == "О системе"

    def test_only_one_public_item_is_active(self):
        """Подсветка меню всегда одна: «вы здесь» не имеет двух ответов."""
        for path in ("/", "/about/", "/competencies/", "/readiness/"):
            menu = get_public_menu(RequestFactory().get(path))
            active = [item for item in menu if item["is_active"]]
            assert len(active) <= 1, f"{path}: подсвечено {len(active)} пунктов"

    @pytest.mark.django_db
    def test_nested_section_wins_over_parent(self, hr_user):
        """На вложенной странице подсвечивается самый точный пункт."""
        request = RequestFactory().get(reverse("analytics:ninebox"))
        request.user = hr_user

        menu = get_workspace_menu(request)
        active = [item for item in menu if item["is_active"]]

        assert len(active) == 1
        assert active[0]["url"] == reverse("analytics:ninebox")

    @pytest.mark.django_db
    def test_parent_section_stays_active_on_its_own_pages(self, hr_user):
        """Раздел остаётся подсвеченным на собственных вложенных страницах."""
        request = RequestFactory().get(reverse("analytics:index"))
        request.user = hr_user

        active = [item for item in get_workspace_menu(request) if item["is_active"]]

        assert len(active) == 1
        assert active[0]["url"] == reverse("analytics:index")

    @pytest.mark.django_db
    def test_workspace_icons_are_distinct_within_menu(self, hr_user):
        """Соседние пункты меню не повторяют значок."""
        request = RequestFactory().get(reverse("dashboard:hr"))
        request.user = hr_user

        icons = [item["icon"] for item in get_workspace_menu(request)]
        for first, second in itertools.pairwise(icons):
            assert first != second, f"значок «{first}» повторяется подряд"

    def test_workspace_menu_empty_for_anonymous(self):
        from django.contrib.auth.models import AnonymousUser

        request = RequestFactory().get("/")
        request.user = AnonymousUser()

        assert get_workspace_menu(request) == []

    def test_workspace_menu_selects_by_role(self, hr_user, admin_user):
        """Пользователю выдаётся меню, соответствующее его роли."""
        assert ROLE_MENUS[hr_user.role] is HR_MENU
        assert ROLE_MENUS[admin_user.role][: len(ADMIN_MENU)] == ADMIN_MENU

        hr_labels = {str(item.label) for item in ROLE_MENUS[hr_user.role]}
        assert "Кадровый резерв" in hr_labels
        assert "Журнал аудита" not in hr_labels

    def test_workspace_menu_skips_unresolved_routes(self, hr_user):
        """Пункт с неподключённым маршрутом не попадает в меню."""
        request = RequestFactory().get("/")
        request.user = hr_user

        menu = get_workspace_menu(request)

        assert all(item["url"] for item in menu)
        assert len(menu) <= len(HR_MENU)


@pytest.mark.django_db
class TestBreadcrumbs:
    """Формирование цепочки навигации."""

    def test_trail_always_has_root(self):
        request = RequestFactory().get("/")
        request.user = None

        trail = BreadcrumbTrail(request)
        crumbs = trail.resolve()

        assert len(crumbs) == 1
        assert crumbs[0].label == "Главная"

    def test_last_crumb_has_no_link(self):
        """Ссылка на текущую страницу дезориентирует пользователя."""
        request = RequestFactory().get("/about/")
        request.user = None

        crumbs = BreadcrumbTrail(request).add("О системе").resolve()

        assert crumbs[-1].is_link is False
        assert crumbs[0].is_link is True

    def test_intermediate_crumbs_keep_links(self):
        request = RequestFactory().get("/")
        request.user = None

        crumbs = (
            BreadcrumbTrail(request)
            .add("Контакты", "portal:contacts")
            .add("Обратная связь")
            .resolve()
        )

        assert len(crumbs) == 3
        assert crumbs[1].is_link is True
        assert crumbs[1].label == "Контакты"
        assert crumbs[2].is_link is False

    def test_root_differs_for_authenticated_user(self, hr_user):
        """Для авторизованного пользователя корнем служит его рабочее место."""
        request = RequestFactory().get("/")
        request.user = hr_user

        crumbs = BreadcrumbTrail(request).resolve()
        assert crumbs[0].label == "Рабочее место"

    def test_unknown_route_yields_text_crumb(self):
        """Несуществующее имя маршрута не приводит к отказу."""
        request = RequestFactory().get("/")
        request.user = None

        crumbs = BreadcrumbTrail(request).add("Раздел", "несуществующий:маршрут").resolve()
        assert crumbs[-1].label == "Раздел"

    def test_build_trail_helper(self):
        request = RequestFactory().get("/")
        request.user = None

        crumbs = build_trail(request, ("Контакты", "portal:contacts"), "Обратная связь")

        assert [c.label for c in crumbs] == ["Главная", "Контакты", "Обратная связь"]
