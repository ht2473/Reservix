#!/usr/bin/env python
"""Утилита командной строки Django для административных задач Reservix."""

from __future__ import annotations

import os
import sys


def main() -> None:
    """Запускает команду Django, используя конфигурацию разработки по умолчанию."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover - диагностика окружения
        raise ImportError(
            "Не удалось импортировать Django. Убедитесь, что зависимости установлены "
            "и активировано виртуальное окружение: `uv sync`."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
