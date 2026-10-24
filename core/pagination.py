"""Постраничная навигация."""

from __future__ import annotations

from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import QuerySet
from django.http import HttpRequest

from core.settings_types import get_reservix_settings

#: Символ-заполнитель для пропущенных диапазонов страниц.
ELLIPSIS = "…"


class WindowedPaginator(Paginator):
    """Постраничная навигация с ограниченным числом видимых номеров."""

    def __init__(self, *args, window: int = 2, edges: int = 1, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.window = window
        self.edges = edges

    def visible_pages(self, current: int) -> list[int | str]:
        """
        Строит список номеров для вывода.

        Пример при 20 страницах и текущей 10-й::

            [1, '…', 8, 9, 10, 11, 12, '…', 20]
        """
        total = self.num_pages
        if total <= (self.window * 2 + self.edges * 2 + 2):
            return list(self.page_range)

        pages: list[int | str] = []
        head = set(range(1, self.edges + 1))
        tail = set(range(total - self.edges + 1, total + 1))
        middle = set(range(max(1, current - self.window), min(total, current + self.window) + 1))
        visible = sorted(head | middle | tail)

        previous: int | None = None
        for number in visible:
            if previous is not None and number - previous > 1:
                pages.append(ELLIPSIS)
            pages.append(number)
            previous = number
        return pages


def paginate(
    request: HttpRequest,
    queryset: QuerySet,
    *,
    per_page: int | None = None,
    param: str = "page",
    size_param: str = "size",
) -> dict:
    """Разбивает выборку на страницы и готовит контекст для шаблона."""
    options = get_reservix_settings()
    allowed_sizes = options["PAGE_SIZE_CHOICES"]
    default_size = per_page or options["PAGE_SIZE"]

    try:
        requested = int(request.GET.get(size_param, default_size))
        size = requested if requested in allowed_sizes else default_size
    except (TypeError, ValueError):
        size = default_size

    paginator = WindowedPaginator(queryset, size)
    number = request.GET.get(param, 1)

    try:
        page = paginator.page(number)
    except PageNotAnInteger:
        page = paginator.page(1)
    except EmptyPage:
        # Выход за пределы диапазона возвращает последнюю страницу: так ссылка
        # из письма остаётся рабочей после удаления части записей.
        page = paginator.page(paginator.num_pages)

    # Параметры адреса без номера страницы: используются в ссылках навигации,
    # чтобы фильтры и сортировка сохранялись при переходе.
    params = request.GET.copy()
    params.pop(param, None)

    return {
        "page_obj": page,
        "paginator": paginator,
        "object_list": page.object_list,
        "is_paginated": paginator.num_pages > 1,
        "visible_pages": paginator.visible_pages(page.number),
        "page_size": size,
        "page_size_choices": allowed_sizes,
        "pagination_query": params.urlencode(),
        "total_count": paginator.count,
    }
