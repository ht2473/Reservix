"""Процессоры контекста шаблонов."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _

from core import appearance as appearance_module
from core.constants import Role
from core.navigation import PUBLIC_MENU, get_public_menu, get_role_home_url, get_workspace_menu
from core.utils import NUMBER_SEPARATORS


def site_identity(request: HttpRequest) -> dict[str, Any]:
    """Идентификация ресурса."""

    def _footer_pages() -> list[Any]:
        # Импорт внутри функции: core — нижний слой, и ссылка на модель приложения на уровне модуля
        # связала бы его с портальным приложением при загрузке настроек.
        from apps.portal.models import Page

        return list(
            Page.objects.filter(is_published=True, show_in_footer=True)
            .only("slug", "title")
            .order_by("title")
        )

    return {
        "SITE_NAME": settings.SITE_NAME,
        "SITE_TAGLINE": settings.SITE_TAGLINE,
        "SITE_DOMAIN": settings.SITE_DOMAIN,
        "ORGANIZATION_NAME": settings.ORGANIZATION_NAME,
        "ORGANIZATION_SHORT": settings.ORGANIZATION_SHORT,
        "APP_VERSION": settings.API_VERSION,
        "footer_pages": SimpleLazyObject(_footer_pages),
    }


def navigation(request: HttpRequest) -> dict[str, Any]:
    """Меню и карта разделов."""

    def _palette() -> list[dict[str, str]]:
        """Собирает список разделов для командной палитры."""
        sprite = f"{settings.STATIC_URL}img/icons.svg#icon-"
        entries: list[dict[str, str]] = []

        for item in get_workspace_menu(request):
            entries.append(
                {
                    "title": str(item["label"]),
                    "url": item["url"],
                    "icon": f"{sprite}{item['icon']}",
                    "group": str(_("Рабочее место")),
                    "keywords": str(item["description"]),
                }
            )

        for item in get_public_menu(request):
            entries.append(
                {
                    "title": str(item["label"]),
                    "url": item["url"],
                    "icon": f"{sprite}{item['icon']}",
                    "group": str(_("Разделы системы")),
                    "keywords": str(item["description"]),
                }
            )

        return entries

    return {
        "public_menu": SimpleLazyObject(lambda: get_public_menu(request)),
        "workspace_menu": SimpleLazyObject(lambda: get_workspace_menu(request)),
        "public_menu_items": PUBLIC_MENU,
        "role_home_url": SimpleLazyObject(lambda: get_role_home_url(request.user)),
        # Функция, а не результат: шаблонизатор вызывает её только при обращении к переменной.
        "palette_entries": _palette,
    }


def interface(request: HttpRequest) -> dict[str, Any]:
    """Оформление интерфейса и словарь подписей для сценариев."""
    resolved = appearance_module.resolve(request)

    def _strings() -> dict[str, str]:
        return {
            # Командная палитра
            "palette.empty": str(_("Ничего не найдено")),
            "palette.sections": str(_("Разделы")),
            "palette.hint": str(_("Начните вводить название раздела")),
            # Всплывающие уведомления
            "toast.close": str(_("Закрыть уведомление")),
            "error.generic": str(_("Не удалось выполнить операцию. Повторите попытку.")),
            "error.forbidden": str(_("Недостаточно прав для выполнения операции.")),
            "error.notFound": str(_("Запрошенные данные не найдены.")),
            "error.server": str(_("Ошибка на сервере. Администратор уведомлён.")),
            "error.offline": str(_("Нет связи с сервером. Проверьте подключение.")),
            # Калькулятор индекса готовности
            "readiness.high": str(_("Готов к назначению")),
            "readiness.medium": str(_("Готов в течение года")),
            "readiness.low": str(_("Требуется развитие")),
            "readiness.weight": str(_("вес")),
            # Разделители чисел: сценарий обязан оформлять значение так же, как это делает сервер,
            # иначе на одной странице встречаются «69,8» и «69.8».
            "number.group": NUMBER_SEPARATORS.get(get_language() or "ru", (" ", ","))[0],
            "number.decimal": NUMBER_SEPARATORS.get(get_language() or "ru", (" ", ","))[1],
        }

    return {
        "ui_theme": resolved.theme_attribute,
        "ui_density": resolved.density_attribute,
        "ui_theme_choice": resolved.theme,
        "ui_density_choice": resolved.density,
        # Функция, а не значение: json_script получает результат вызова только
        # при обращении к переменной в шаблоне.
        "ui_strings": _strings,
    }


def user_context(request: HttpRequest) -> dict[str, Any]:
    """Сведения о текущем пользователе для шапки интерфейса."""
    user = getattr(request, "user", None)

    if user is None or not user.is_authenticated:
        return {
            "current_role": None,
            "current_role_label": "",
            "is_privileged_user": False,
            "nav_counters": {},
            "nav_notifications": [],
        }

    def _counters() -> dict[str, int]:
        """Собирает счётчики значков меню одним обращением к каждому источнику."""
        from apps.notifications.services import get_navigation_counters

        return get_navigation_counters(user)

    def _recent_notifications() -> list:
        from apps.notifications.models import Notification

        return list(Notification.objects.for_user(user).order_by("-created_at")[:6])

    return {
        "current_role": user.role,
        "current_role_label": Role(user.role).label,
        "is_privileged_user": user.role in {Role.ADMIN, Role.HR},
        "is_admin_user": user.role == Role.ADMIN,
        "nav_counters": SimpleLazyObject(_counters),
        "nav_notifications": SimpleLazyObject(_recent_notifications),
    }
