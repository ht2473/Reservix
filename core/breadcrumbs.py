"""Построение «хлебных крошек»."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

    from django.http import HttpRequest

    from apps.accounts.models import User


@dataclass(frozen=True, slots=True)
class Crumb:
    """Звено цепочки навигации."""

    # Подпись приводится к строке при создании звена: цепочка формируется
    # в момент отрисовки, когда язык интерфейса уже определён.
    label: str
    url: str = ""

    @property
    def is_link(self) -> bool:
        return bool(self.url)


class BreadcrumbTrail:
    """
    Накопитель цепочки навигации.

    Использование в представлении::

        trail = BreadcrumbTrail(self.request)
        trail.add(_("Кадровый резерв"), "reserve:list")
        trail.add(employee.short_name)
    """

    def __init__(self, request: HttpRequest | None = None) -> None:
        self._crumbs: list[Crumb] = []
        self._request = request
        self._add_root()

    # -- построение ---------------------------------------------------------

    def _add_root(self) -> None:
        """Добавляет корневое звено."""
        user: User | None = getattr(self._request, "user", None) if self._request else None
        if user is not None and user.is_authenticated:
            from core.navigation import get_role_home_url

            self._crumbs.append(Crumb(str(_("Рабочее место")), get_role_home_url(user)))
        else:
            self._crumbs.append(Crumb(str(_("Главная")), self._safe_reverse("portal:home")))

    @staticmethod
    def _safe_reverse(url_name: str, *args, **kwargs) -> str:
        """Разрешает имя маршрута, возвращая пустую строку при его отсутствии."""
        try:
            return reverse(url_name, args=args, kwargs=kwargs)
        except NoReverseMatch:  # pragma: no cover - защита от неполной конфигурации
            return ""

    def add(self, label: StrOrPromise, url_name: str = "", *args, **kwargs) -> BreadcrumbTrail:
        """Добавляет звено в конец цепочки."""
        url = self._safe_reverse(url_name, *args, **kwargs) if url_name else ""
        self._crumbs.append(Crumb(str(label), url))
        return self

    def add_raw(self, label: StrOrPromise, url: str = "") -> BreadcrumbTrail:
        """Добавляет звено с готовым адресом (например, `get_absolute_url`)."""
        self._crumbs.append(Crumb(str(label), url))
        return self

    def extend(self, crumbs: list[Crumb]) -> BreadcrumbTrail:
        """Присоединяет готовый фрагмент цепочки."""
        self._crumbs.extend(crumbs)
        return self

    # -- вывод --------------------------------------------------------------

    def resolve(self) -> list[Crumb]:
        """Возвращает готовую цепочку."""
        if not self._crumbs:
            return []
        result = list(self._crumbs[:-1])
        last = self._crumbs[-1]
        result.append(Crumb(last.label))
        return result

    def __iter__(self):
        return iter(self.resolve())

    def __len__(self) -> int:
        return len(self._crumbs)

    def __repr__(self) -> str:  # pragma: no cover - диагностика
        path = " → ".join(crumb.label for crumb in self._crumbs)
        return f"<BreadcrumbTrail: {path}>"


def build_trail(
    request: HttpRequest, *items: tuple[StrOrPromise, str] | StrOrPromise
) -> list[Crumb]:
    """Краткий способ собрать цепочку во функциональном представлении."""
    trail = BreadcrumbTrail(request)
    for item in items:
        if isinstance(item, tuple):
            trail.add(item[0], item[1])
        else:
            trail.add(item)
    return trail.resolve()
