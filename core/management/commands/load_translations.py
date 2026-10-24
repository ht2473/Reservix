"""
Заполнение английских версий справочников и содержимого из каталога.

Запуск::

    uv run python manage.py load_translations
    uv run python manage.py load_translations --report   # показать пробелы
"""

from __future__ import annotations

from typing import Any

from modeltranslation.translator import translator

from django.core.management.base import BaseCommand
from django.db import transaction

from core.data_translation import Translator, english_value, interface_forms, load_catalog


class Command(BaseCommand):
    help = "Заполняет английские версии справочников и содержимого из каталога"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--report", action="store_true", help="показать строки без перевода")

    def handle(self, *args: Any, **options: Any) -> None:
        engine = Translator(load_catalog(), interface_forms())
        updated = 0

        with transaction.atomic():
            for model in translator.get_registered_models():
                fields = translator.get_options_for_model(model).get_field_names()
                manager = getattr(model, "all_objects", model._default_manager)
                changed = []
                for obj in manager.all().iterator(chunk_size=500):
                    dirty = False
                    for name in fields:
                        source = getattr(obj, f"{name}_ru")
                        if not source:
                            continue
                        engine.context = f"{model._meta.label}.{name}"
                        value = english_value(name, source, engine)
                        if value and getattr(obj, f"{name}_en") != value:
                            setattr(obj, f"{name}_en", value)
                            dirty = True
                    if dirty:
                        changed.append(obj)
                if changed:
                    model._default_manager.bulk_update(
                        changed, [f"{name}_en" for name in fields], batch_size=500
                    )
                    updated += len(changed)

        self.stdout.write(self.style.SUCCESS(f"Обновлено записей: {updated}"))
        if engine.missing:
            self.stdout.write(self.style.WARNING(f"Без перевода строк: {len(engine.missing)}"))
            if options["report"]:
                for context, text in sorted(engine.missing):
                    self.stdout.write(f"  {context}: {text[:160]}")
