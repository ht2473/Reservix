"""Оформление интерфейса: тема и плотность таблиц."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.conf import settings

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

#: Допустимые значения. Проверка обязательна: значение приходит из cookie,
#: то есть от клиента, и попадает в атрибут разметки.
THEMES = ("light", "dark", "system")
DENSITIES = ("comfortable", "compact")

DEFAULT_THEME = "system"
DEFAULT_DENSITY = "comfortable"

#: Разделитель полей в значении cookie.
_SEPARATOR = ":"


@dataclass(frozen=True, slots=True)
class Appearance:
    """Разрешённое оформление интерфейса для текущего запроса."""

    theme: str = DEFAULT_THEME
    density: str = DEFAULT_DENSITY

    @property
    def theme_attribute(self) -> str:
        """Значение атрибута ``data-theme`` корневого элемента."""
        return "" if self.theme == "system" else self.theme

    @property
    def density_attribute(self) -> str:
        """Значение атрибута ``data-density``; обычная плотность — без атрибута."""
        return "compact" if self.density == "compact" else ""

    def as_cookie(self) -> str:
        return f"{self.theme}{_SEPARATOR}{self.density}"


def parse_cookie(value: str | None) -> Appearance | None:
    """Разбирает значение cookie, отбрасывая недопустимое."""
    if not value:
        return None
    theme, _, density = value.partition(_SEPARATOR)
    if theme not in THEMES:
        return None
    return Appearance(theme=theme, density=density if density in DENSITIES else DEFAULT_DENSITY)


def from_profile(user) -> Appearance | None:
    """Оформление, сохранённое в профиле пользователя."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    profile = getattr(user, "profile", None)
    if profile is None:
        return None
    theme = profile.theme if profile.theme in THEMES else DEFAULT_THEME
    density = profile.density if profile.density in DENSITIES else DEFAULT_DENSITY
    return Appearance(theme=theme, density=density)


def resolve(request: HttpRequest) -> Appearance:
    """Оформление текущего запроса по порядку приоритетов."""
    from_cookie = parse_cookie(request.COOKIES.get(settings.APPEARANCE_COOKIE_NAME))
    if from_cookie is not None:
        return from_cookie
    stored = from_profile(getattr(request, "user", None))
    if stored is not None:
        return stored
    return Appearance()


def remember(response: HttpResponse, appearance: Appearance) -> HttpResponse:
    """Записывает оформление в cookie ответа."""
    response.set_cookie(
        settings.APPEARANCE_COOKIE_NAME,
        appearance.as_cookie(),
        max_age=settings.APPEARANCE_COOKIE_AGE,
        samesite="Lax",
        secure=not settings.DEBUG,
    )
    return response
