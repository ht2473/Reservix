"""Декларативное описание навигации."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _

from core.constants import Role

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

    from django.contrib.auth.models import AnonymousUser
    from django.http import HttpRequest

    from apps.accounts.models import User


@dataclass(frozen=True, slots=True)
class NavItem:
    """Пункт меню."""

    # Подписи задаются через gettext_lazy: перевод вычисляется в момент
    # отрисовки, когда язык интерфейса уже определён слоем локализации.
    label: StrOrPromise
    url_name: str
    icon: str = "dot"
    roles: frozenset[str] = frozenset()
    description: StrOrPromise = ""
    children: tuple[NavItem, ...] = field(default_factory=tuple)
    badge_key: str = ""
    #: Заголовок группы, который выводится перед пунктом в боковом меню.
    group: StrOrPromise = ""

    @property
    def url(self) -> str:
        """Адрес пункта. Пустая строка, если маршрут ещё не подключён."""
        try:
            return reverse(self.url_name)
        except NoReverseMatch:  # pragma: no cover - защита от неполной конфигурации
            return ""

    def is_visible_for(self, user: User | None) -> bool:
        """Проверяет, показывать ли пункт конкретному пользователю."""
        if not self.roles:
            return True
        if user is None or not user.is_authenticated:
            return False
        return user.role in self.roles

    def match_length(self, path: str) -> int:
        """Длина совпадения адреса пункта с текущим адресом."""
        url = self.url
        if not url:
            return 0
        if url == "/" or self.url_name == "portal:home":
            # Адрес главной («/» или «/en/») — префикс всех адресов: только точное совпадение.
            return 1 if path == url else 0
        if path == url or path.startswith(url.rstrip("/") + "/"):
            return len(url.rstrip("/"))
        return max((child.match_length(path) for child in self.children), default=0)

    def is_active(self, path: str) -> bool:
        """Соответствует ли пункт текущему адресу без учёта прочих пунктов."""
        return self.match_length(path) > 0


# ---------------------------------------------------------------------------
# Публичное меню
# ---------------------------------------------------------------------------
# Двенадцать пунктов, доступных без авторизации. Порядок отражает логику
# знакомства с системой: назначение — методика — инструменты — справка.
PUBLIC_MENU: tuple[NavItem, ...] = (
    NavItem(
        label=_("Главная"),
        url_name="portal:home",
        icon="home",
        description=_("Назначение системы и ключевые показатели кадрового резерва"),
    ),
    NavItem(
        label=_("О системе"),
        url_name="portal:about",
        icon="info",
        description=_("Задачи, границы и архитектура решения"),
    ),
    NavItem(
        label=_("Методология"),
        url_name="portal:methodology",
        icon="route",
        description=_("Семь этапов формирования и развития кадрового резерва"),
    ),
    NavItem(
        label=_("Компетенции"),
        url_name="portal:competencies",
        icon="layers",
        description=_("Справочник компетенций и поведенческих индикаторов"),
    ),
    NavItem(
        label=_("Индекс готовности"),
        url_name="portal:readiness",
        icon="gauge",
        description=_("Методика расчёта композитного индекса и калькулятор"),
    ),
    NavItem(
        label=_("Матрица 9-box"),
        url_name="portal:ninebox",
        icon="grid",
        description=_("Оценка результативности и потенциала: демонстрация"),
    ),
    NavItem(
        label=_("Нормативная база"),
        url_name="portal:regulations",
        icon="scale",
        description=_("Положение о кадровом резерве и типовые формы документов"),
    ),
    NavItem(
        label=_("База знаний"),
        url_name="portal:articles",
        icon="book",
        description=_("Материалы по управлению талантами и преемственностью"),
    ),
    NavItem(
        label=_("Глоссарий"),
        url_name="portal:glossary",
        icon="dictionary",
        description=_("Термины управления кадровым резервом"),
    ),
    NavItem(
        label=_("Справка"),
        url_name="portal:faq",
        icon="help",
        description=_("Ответы на частые вопросы по работе с системой"),
    ),
    NavItem(
        label=_("API"),
        url_name="portal:api_docs",
        icon="code",
        description=_("Программный интерфейс и спецификация OpenAPI"),
    ),
    NavItem(
        label=_("Контакты"),
        url_name="portal:contacts",
        icon="mail",
        description=_("Обратная связь и реквизиты"),
    ),
)


# ---------------------------------------------------------------------------
# Меню личных кабинетов
# ---------------------------------------------------------------------------
ADMIN_MENU: tuple[NavItem, ...] = (
    NavItem(_("Дашборд"), "administration:index", "dashboard", frozenset({Role.ADMIN})),
    NavItem(_("Пользователи и роли"), "administration:users", "users", frozenset({Role.ADMIN})),
    NavItem(_("Оргструктура"), "administration:org", "sitemap", frozenset({Role.ADMIN})),
    NavItem(_("Справочники"), "administration:references", "layers", frozenset({Role.ADMIN})),
    NavItem(_("Модель скоринга"), "administration:scoring", "sliders", frozenset({Role.ADMIN})),
    NavItem(_("Журнал аудита"), "administration:audit", "shield", frozenset({Role.ADMIN})),
    NavItem(_("Данные системы"), "administration:data", "database", frozenset({Role.ADMIN})),
    NavItem(_("Контент портала"), "administration:content", "book", frozenset({Role.ADMIN})),
    NavItem(
        _("Обращения"),
        "administration:feedback",
        "mail",
        frozenset({Role.ADMIN}),
        badge_key="new_feedback",
    ),
)

HR_MENU: tuple[NavItem, ...] = (
    NavItem(_("Дашборд"), "dashboard:hr", "dashboard", frozenset({Role.HR, Role.ADMIN})),
    NavItem(_("Сотрудники"), "people:list", "users", frozenset({Role.HR, Role.ADMIN})),
    NavItem(_("Кадровый резерв"), "reserve:list", "bookmark", frozenset({Role.HR, Role.ADMIN})),
    NavItem(_("Кампании оценки"), "assessment:list", "clipboard", frozenset({Role.HR, Role.ADMIN})),
    NavItem(_("Компетенции"), "competencies:index", "layers", frozenset({Role.HR, Role.ADMIN})),
    NavItem(
        _("Матрица разрывов"),
        "competencies:matrix",
        "scale",
        frozenset({Role.HR, Role.ADMIN}),
    ),
    NavItem(_("Матрица 9-box"), "analytics:ninebox", "grid", frozenset({Role.HR, Role.ADMIN})),
    NavItem(_("Преемственность"), "succession:list", "route", frozenset({Role.HR, Role.ADMIN})),
    NavItem(_("Планы развития"), "development:list", "target", frozenset({Role.HR, Role.ADMIN})),
    NavItem(_("Аналитика"), "analytics:index", "chart", frozenset({Role.HR, Role.ADMIN})),
    NavItem(_("Отчёты"), "reporting:index", "download", frozenset({Role.HR, Role.ADMIN})),
    NavItem(_("Документы"), "documents:list", "file", frozenset({Role.HR, Role.ADMIN})),
    NavItem(
        _("Уведомления"),
        "notifications:list",
        "bell",
        frozenset({Role.HR}),
        badge_key="unread_notifications",
    ),
)

MANAGER_MENU: tuple[NavItem, ...] = (
    NavItem(_("Дашборд"), "dashboard:manager", "dashboard", frozenset({Role.MANAGER})),
    NavItem(_("Мои сотрудники"), "people:team", "users", frozenset({Role.MANAGER})),
    NavItem(_("Мои резервисты"), "reserve:team", "bookmark", frozenset({Role.MANAGER})),
    NavItem(
        _("Оценка"),
        "assessment:inbox",
        "clipboard",
        frozenset({Role.MANAGER}),
        badge_key="pending_assessments",
    ),
    NavItem(
        _("Согласования"),
        "reserve:approvals",
        "check",
        frozenset({Role.MANAGER}),
        badge_key="pending_approvals",
    ),
    NavItem(_("Разрывы команды"), "competencies:matrix", "scale", frozenset({Role.MANAGER})),
    # Матрица «результативность — потенциал» — рабочий инструмент руководителя, а не отчётность
    # кадровой службы: оценки в неё ставит он, и на сессии калибровки защищает их перед коллегами.
    NavItem(_("Матрица 9-box"), "analytics:ninebox", "grid", frozenset({Role.MANAGER})),
    NavItem(_("Наставничество"), "development:mentorship", "handshake", frozenset({Role.MANAGER})),
    NavItem(_("Отчёты подразделения"), "reporting:unit", "download", frozenset({Role.MANAGER})),
    NavItem(_("Документы"), "documents:list", "file", frozenset({Role.MANAGER})),
    NavItem(
        _("Уведомления"),
        "notifications:list",
        "bell",
        frozenset({Role.MANAGER}),
        badge_key="unread_notifications",
    ),
)

MENTOR_MENU: tuple[NavItem, ...] = (
    NavItem(_("Мои подопечные"), "development:mentees", "handshake", frozenset({Role.MENTOR})),
    NavItem(_("Планы подопечных"), "development:mentee_plans", "target", frozenset({Role.MENTOR})),
    NavItem(
        _("Обратная связь"),
        "development:mentor_feedback",
        "message",
        frozenset({Role.MENTOR}),
    ),
    NavItem(_("Материалы"), "development:resources", "book", frozenset({Role.MENTOR})),
    NavItem(_("Отчёты"), "reporting:mentor", "download", frozenset({Role.MENTOR})),
    NavItem(
        _("Уведомления"),
        "notifications:list",
        "bell",
        frozenset({Role.MENTOR}),
        badge_key="unread_notifications",
    ),
)

EMPLOYEE_MENU: tuple[NavItem, ...] = (
    NavItem(_("Обзор"), "dashboard:employee", "dashboard", frozenset({Role.EMPLOYEE})),
    NavItem(_("Мой профиль"), "accounts:profile", "user", frozenset({Role.EMPLOYEE})),
    NavItem(_("Мои компетенции"), "competencies:my_profile", "layers", frozenset({Role.EMPLOYEE})),
    NavItem(_("Мой план развития"), "development:my_plan", "target", frozenset({Role.EMPLOYEE})),
    NavItem(_("Мои оценки"), "assessment:my_results", "clipboard", frozenset({Role.EMPLOYEE})),
    NavItem(_("Тестирование"), "assessment:tests", "check", frozenset({Role.EMPLOYEE})),
    NavItem(_("Заявка в резерв"), "reserve:apply", "bookmark", frozenset({Role.EMPLOYEE})),
    NavItem(_("Мои документы"), "documents:list", "file", frozenset({Role.EMPLOYEE})),
    NavItem(
        _("Уведомления"),
        "notifications:list",
        "bell",
        frozenset({Role.EMPLOYEE}),
        badge_key="unread_notifications",
    ),
)

#: Администратору доступны и разделы кадровой службы: они выводятся отдельной группой.
ADMIN_HR_SECTION: tuple[NavItem, ...] = tuple(
    NavItem(
        item.label,
        item.url_name,
        item.icon,
        item.roles,
        item.description,
        item.children,
        item.badge_key,
        group=_("Кадровая работа") if index == 0 else "",
    )
    for index, item in enumerate(
        entry for entry in HR_MENU if Role.ADMIN in entry.roles and entry.url_name != "dashboard:hr"
    )
)

#: Соответствие роли и меню её рабочего места.
ROLE_MENUS: dict[str, tuple[NavItem, ...]] = {
    Role.ADMIN: (
        *ADMIN_MENU,
        *ADMIN_HR_SECTION,
        NavItem(
            _("Уведомления"),
            "notifications:list",
            "bell",
            frozenset({Role.ADMIN}),
            badge_key="unread_notifications",
        ),
    ),
    Role.HR: HR_MENU,
    Role.MANAGER: MANAGER_MENU,
    Role.MENTOR: MENTOR_MENU,
    Role.EMPLOYEE: EMPLOYEE_MENU,
}

#: Стартовая страница рабочего места для каждой роли.
ROLE_HOME: dict[str, str] = {
    Role.ADMIN: "administration:index",
    Role.HR: "dashboard:hr",
    Role.MANAGER: "dashboard:manager",
    Role.MENTOR: "development:mentees",
    Role.EMPLOYEE: "dashboard:employee",
}


def mark_active(items: list[dict], path: str, matches: list[int]) -> list[dict]:
    """Отмечает активным единственный пункт — с самым точным совпадением адреса."""
    best = max(matches, default=0)
    marked = False
    for item, length in zip(items, matches, strict=True):
        # Первый из равных: одинаковую длину дают только пункты с одним
        # адресом, и подсветить нужно тот, что стоит в меню выше.
        item["is_active"] = bool(length) and length == best and not marked
        marked = marked or item["is_active"]
    return items


def get_public_menu(request: HttpRequest) -> list[dict]:
    """Возвращает публичное меню с отмеченным активным пунктом."""
    path = request.path
    visible = [item for item in PUBLIC_MENU if item.url]
    items = [
        {
            "label": item.label,
            "url": item.url,
            "icon": item.icon,
            "description": item.description,
        }
        for item in visible
    ]
    return mark_active(items, path, [item.match_length(path) for item in visible])


def get_workspace_menu(request: HttpRequest) -> list[dict]:
    """Возвращает меню рабочего места по роли текущего пользователя."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return []

    menu = ROLE_MENUS.get(user.role, ())
    path = request.path
    counters = getattr(request, "nav_counters", {}) or {}

    visible = [item for item in menu if item.url and item.is_visible_for(user)]
    items = [
        {
            "label": item.label,
            "url": item.url,
            "icon": item.icon,
            "description": item.description,
            "badge": counters.get(item.badge_key) if item.badge_key else None,
            "group": item.group,
        }
        for item in visible
    ]
    matches = [item.match_length(path) for item in visible]
    if not any(matches):
        matches = section_matches(request, visible)
    return mark_active(items, path, matches)


def section_matches(request: HttpRequest, items: list[NavItem]) -> list[int]:
    """
    Совпадения по разделу для страниц, не вложенных в адрес пункта меню.

    Карточка компетенции или теста открывается из «Моих компетенций», но лежит
    по другому адресу; подсвечивается пункт того же раздела, ближайший по пути.
    """
    match = getattr(request, "resolver_match", None)
    namespace = match.namespace if match else ""
    parts = [part for part in request.path.split("/") if part]
    result = []
    for item in items:
        if not namespace or item.url_name.split(":")[0] != namespace:
            result.append(0)
            continue
        common = 0
        for left, right in zip(parts, [part for part in item.url.split("/") if part], strict=False):
            if left != right:
                break
            common += 1
        result.append(common)
    return result


def get_role_home_url(user: User | AnonymousUser | None) -> str:
    """Адрес стартовой страницы рабочего места пользователя."""
    if user is None or not user.is_authenticated:
        return reverse("portal:home")
    try:
        return reverse(ROLE_HOME.get(user.role, "portal:home"))
    except NoReverseMatch:  # pragma: no cover - защита от неполной конфигурации
        return reverse("portal:home")
