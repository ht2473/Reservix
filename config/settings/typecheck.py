"""
Конфигурация для статической проверки типов.

Совпадает с тестовой, но без modeltranslation: пакет подменяет менеджеры
моделей при загрузке, и плагин django-stubs перестаёт их распознавать.
"""

from __future__ import annotations

from .test import *

INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "modeltranslation"]
