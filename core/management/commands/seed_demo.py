"""
Полное наполнение демонстрационного стенда.

Запуск::

    uv run python manage.py seed_demo
    uv run python manage.py seed_demo --reset     # пересоздать данные с нуля
    uv run python manage.py seed_demo --minimal   # без оценок и тестов
"""

from __future__ import annotations

import time
from typing import Any

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.test import override_settings

#: Последовательность наполнения: команда и то, что она создаёт.
PIPELINE: tuple[tuple[str, str], ...] = (
    ("seed_org", "справочники организации и штатное расписание"),
    ("seed_users", "учётные записи всех ролей"),
    ("import_legacy", "перенос исходной базы данных"),
    ("seed_people", "кадровый состав до плановой численности"),
    ("seed_competencies", "корпоративная модель компетенций"),
    ("seed_assessment", "кампании оценки, опрос 360°, тесты"),
    ("seed_reserve", "кадровый резерв: программы, заявки, записи"),
    ("seed_succession", "критичные должности, планы преемственности, назначения"),
    ("seed_analytics", "схемы весов, матрица 9-box, расчёт индексов готовности"),
    ("seed_development", "библиотека развития, планы, мероприятия, наставничество"),
    ("seed_documents", "приказы, протоколы, сертификаты и заключения в хранилище"),
    ("seed_reporting", "каталог отчётов и шаблоны выгрузок"),
    ("seed_portal", "содержимое публичной части: статьи, глоссарий, справка"),
    ("seed_activity", "текущая работа ролей: заявки, оценки, отчёты, уведомления"),
)

#: Команды, принимающие значение генератора случайных чисел.
SEEDED_STEPS = frozenset(
    {
        "seed_people",
        "seed_assessment",
        "seed_reserve",
        "seed_succession",
        "seed_analytics",
        "seed_development",
        "seed_documents",
        "seed_activity",
    }
)

#: Команды, относящиеся к оценке.
ASSESSMENT_STEPS = frozenset(
    {
        "seed_competencies",
        "seed_assessment",
        "seed_reserve",
        "seed_succession",
        # Аналитическое ядро считает индекс по шести критериям, четыре из которых берут данные из
        # оценки.
        "seed_analytics",
        # Планы развития строятся от разрывов компетенций: без оценки разрывов
        # нет, и наполнение создало бы планы без единого мероприятия.
        "seed_development",
        # Документы хранилища привязаны к записям резерва, кампаниям оценки и
        # подтверждениям выполнения мероприятий: без них прикреплять нечего.
        "seed_documents",
        # Текущая работа ролей опирается на кампании, заявки и планы развития.
        "seed_activity",
    }
)


class Command(BaseCommand):
    """Выполняет все команды наполнения в правильном порядке."""

    help = "Наполняет демонстрационный стенд целиком"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--reset",
            action="store_true",
            help="очистить базу целиком перед наполнением (только в режиме отладки)",
        )
        parser.add_argument(
            "--minimal",
            action="store_true",
            help="только организация, люди и перенос дампа — без оценок и тестов",
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=None,
            help="значение генератора случайных чисел для воспроизводимости",
        )

    # Наполнение порождает сотни уведомлений; письма по ним не отправляются и не печатаются.
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def handle(self, *args: Any, **options: Any) -> None:
        """Последовательно выполняет команды наполнения."""
        reset: bool = options["reset"]
        minimal: bool = options["minimal"]
        verbosity: int = options["verbosity"]

        if reset:
            self._reset_database(verbosity=verbosity)
        else:
            self._guard_populated_database()

        steps = [step for step in PIPELINE if not (minimal and step[0] in ASSESSMENT_STEPS)]
        started = time.monotonic()

        for index, (command, description) in enumerate(steps, start=1):
            self.stdout.write(
                self.style.MIGRATE_HEADING(f"\n[{index}/{len(steps)}] {command} — {description}")
            )

            arguments: dict[str, Any] = {"verbosity": max(0, verbosity - 1)}
            if options["seed"] is not None and command in SEEDED_STEPS:
                arguments["seed"] = options["seed"]

            try:
                call_command(command, **arguments)
            except Exception as error:
                # Прерывание на середине оставляет базу в промежуточном
                # состоянии, поэтому сообщается, на каком шаге это произошло:
                # повторный запуск начинается с него, а не с начала.
                raise CommandError(f"Наполнение прервано на шаге «{command}»: {error}") from error

        # Английские версии справочников и содержимого из каталога переводов.
        self.stdout.write(
            self.style.MIGRATE_HEADING("\nload_translations — английская версия данных")
        )
        call_command("load_translations", verbosity=max(0, verbosity - 1))

        elapsed = time.monotonic() - started
        self.stdout.write(
            self.style.SUCCESS(
                f"\nДемонстрационный стенд наполнен за {elapsed:.0f} с. "
                f"Выполнено шагов: {len(steps)}."
            )
        )
        self.stdout.write("Учётные данные ролей — в docs/CREDENTIALS.md")

    @staticmethod
    def _guard_populated_database() -> None:
        """Не даёт наполнить уже наполненную базу."""
        from apps.people.models import Employee

        existing = Employee.all_objects.count()
        if not existing:
            return

        raise CommandError(
            f"База уже содержит {existing} карточек сотрудников. "
            "Повторное наполнение поверх существующих данных исказит "
            "численность штата. Выполните `seed_demo --reset`, чтобы "
            "пересоздать демонстрационные данные с нуля."
        )

    def _reset_database(self, *, verbosity: int) -> None:
        """Очищает базу целиком перед наполнением."""
        from django.conf import settings

        if not settings.DEBUG:
            raise CommandError(
                "Очистка базы доступна только в режиме отладки. "
                "В рабочем окружении демонстрационные данные не создаются."
            )

        self.stdout.write(self.style.WARNING("Очистка базы данных перед наполнением…"))
        call_command("flush", "--no-input", verbosity=max(0, verbosity - 1))
