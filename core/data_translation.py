"""
Английские версии справочников и содержимого.

Тексты берутся из каталога core/translations/en: *.json (русская строка →
английская) и пары файлов texts/*.ru.md / *.en.md для длинных текстов.
Составные строки собираются из переведённых частей по шаблонам, ФИО
транслитерируются. Системные тексты новых записей переводятся при сохранении.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from django.conf import settings

CATALOG_DIR = Path(settings.BASE_DIR) / "core" / "translations" / "en"

#: Поля с именами людей: переводятся транслитерацией.
NAME_FIELDS = frozenset({"last_name", "first_name", "middle_name"})

#: Шаблоны составных строк: русский образец и английская форма.
TEMPLATES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.S), form)
    for pattern, form in (
        (
            r"^Веса выведены на (\S+) из (\d+) записей резерва, полных по отобранным "
            r"критериям, — ([\d.,]+) % выборки в (\d+) записей\. "
            r"Исключены по недостаточному покрытию: (.+)\.$",
            "Weights derived on {0} from {1} talent pool records complete for the selected "
            "criteria ({2}% of a sample of {3} records). Excluded for insufficient coverage: {4}.",
        ),
        (
            r"^Сверка результативности и потенциала по филиалу «(.+)» за (\d{4}) год\.$",
            "Performance and potential reconciliation for the {0}, {1}.",
        ),
        (
            r"^Приказ № (\S+)-к о зачислении в кадровый резерв$",
            "Order No. {0}-k on admission to the talent pool",
        ),
        (
            r"^Закрывает разрыв по компетенции «(.+)»: текущий уровень ([\d.]+), целевой (\d+)\.$",
            'Closes the gap in "{0}": current level {1}, target {2}.',
        ),
        (
            r"^Материал развивает компетенцию «(.+)» до уровня (\d) по корпоративной шкале\. (.+)$",
            'Develops the "{0}" competency up to level {1} on the corporate scale. {2}',
        ),
        (
            r"^Достижение готовности к назначению на должность «(.+)» в установленный срок\.$",
            'Reaching readiness for appointment as "{0}" within the set time.',
        ),
        (r"^Подготовка к назначению на должность «(.+)»$", 'Preparation for appointment as "{0}"'),
        (r"^Адаптационный план (.+)$", "Onboarding plan: {0}"),
        (r"^Онбординг (.+)$", "Onboarding: {0}"),
        (
            r"^Средний подтверждённый уровень компетенций\. "
            r"Средний результат тестирования — (\d+)$",
            "Average confirmed competency level. Average test score: {0}",
        ),
        (
            r"^Рассмотрено размещений: (\d+)\. Изменено по итогам обсуждения: (\d+)\. "
            r"Остальные оценки подтверждены без изменений\.$",
            "Placements reviewed: {0}. Changed after discussion: {1}. "
            "The remaining ratings were confirmed without changes.",
        ),
        (r"^(.+) \((\S+@\S+)\)$", "{0} ({1})"),
        (r"^(.+) <(\S+@\S+)>$", "{0} <{1}>"),
        (
            r"^Развитие компетенций, требуемых должностью «(.+)»(\.?)$",
            'Developing the competencies required for "{0}"{1}',
        ),
        (r"^(.+)\. Срок: до (\S+)\.$", "{0}. Due: {1}."),
        (r"^Назначение на должность «(.+)»$", 'Appointment to "{0}"'),
        (r"^«(.+)»$", '"{0}"'),
        (r"^(.+) \(предыдущий уровень\)$", "{0} (previous level)"),
        (r"^Должность подразделения «(.+)»$", "Position in the {0}"),
        (r"^Подразделения организации в городе (.+)$", "Organization units in {0}"),
        (r"^Преемственность: (.+)$", "Succession: {0}"),
        (r"^План развития: (.+)$", "Development plan: {0}"),
        (r"^(.+?): (.+)$", "{0}: {1}"),
        (r"^(.+?) — (.+)$", "{0} — {1}"),
        (r"^(.+?), (.+)$", "{0}, {1}"),
        (r"^(.+?) · (.+)$", "{0} · {1}"),
    )
)

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}  # fmt: skip

CYRILLIC = re.compile(r"[А-Яа-яЁё]")

#: Сокращённое ФИО внутри текста: «Кузнецов А. Д.».
SHORT_NAME = re.compile(r"^[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s[А-ЯЁ]\.\s?(?:[А-ЯЁ]\.)?$")


def transliterate(text: str) -> str:
    """Транслитерация кириллицы латиницей с сохранением регистра."""
    result = []
    for char in text:
        latin = TRANSLIT.get(char.lower())
        if latin is None:
            result.append(char)
        elif char.isupper():
            result.append(latin.capitalize())
        else:
            result.append(latin)
    return "".join(result)


def load_catalog() -> dict[str, str]:
    """Объединяет файлы каталога в один словарь."""
    catalog: dict[str, str] = {}
    for path in sorted(CATALOG_DIR.glob("*.json")):
        catalog.update(json.loads(path.read_text(encoding="utf-8")))
    # Длинные тексты (статьи, страницы) хранятся парами файлов *.ru.md и *.en.md.
    for source in sorted((CATALOG_DIR / "texts").glob("*.ru.md")):
        target = source.with_name(source.name.replace(".ru.md", ".en.md"))
        if target.exists():
            original = source.read_text(encoding="utf-8").strip()
            catalog[original] = target.read_text(encoding="utf-8").strip()
    return catalog


#: Параметр строки интерфейса: «%(name)s».
PLACEHOLDER = re.compile(r"%\((\w+)\)[sd]")

InterfaceForms = tuple[dict[str, str], tuple[tuple[re.Pattern[str], str], ...]]


def interface_forms() -> InterfaceForms:
    """
    Строки английского каталога интерфейса: точные и с параметрами.

    Уведомления и комментарии, сформированные системой, хранятся готовым текстом.
    Их английская версия строится по той же строке каталога, что и интерфейс.
    """
    from django.utils.translation import trans_real

    exact: dict[str, str] = {}
    forms: list[tuple[re.Pattern[str], str]] = []
    catalog = trans_real.translation("en")._catalog  # type: ignore[attr-defined]
    for msgid, msgstr in catalog.items():
        if not isinstance(msgid, str) or not msgstr or not CYRILLIC.search(msgid):
            continue
        if "%(" not in msgid:
            exact[msgid] = msgstr
            continue
        # Короткие строки из одних параметров совпали бы с любым текстом.
        if len(CYRILLIC.findall(PLACEHOLDER.sub("", msgid))) < 4:
            continue
        pieces = PLACEHOLDER.split(msgid)
        names = pieces[1::2]
        if len(set(names)) != len(names):
            continue
        regex = "".join(
            f"(?P<{piece}>.+?)" if index % 2 else re.escape(piece.replace("%%", "%"))
            for index, piece in enumerate(pieces)
        )
        forms.append((re.compile(f"^{regex}$", re.S), PLACEHOLDER.sub(r"%(\1)s", msgstr)))
    return exact, tuple(forms)


class Translator:
    """Перевод строки по каталогу и шаблонам составных строк."""

    def __init__(self, catalog: dict[str, str], interface: InterfaceForms | None = None) -> None:
        self.catalog = catalog
        self.interface_exact, self.interface_forms = interface or ({}, ())
        self.context = ""
        self.missing: set[tuple[str, str]] = set()

    def translate(self, text: str, *, record: bool = True) -> str | None:
        if not text or not CYRILLIC.search(text):
            return text
        if text in self.catalog:
            return self.catalog[text]
        stripped = text.replace("\r\n", "\n").strip()
        if stripped in self.catalog:
            return self.catalog[stripped]
        if SHORT_NAME.match(stripped):
            return transliterate(stripped)
        if stripped in self.interface_exact:
            return self.interface_exact[stripped]
        for pattern, form in self.interface_forms:
            match = pattern.match(stripped)
            if not match:
                continue
            values = {
                name: self.translate(part, record=False) for name, part in match.groupdict().items()
            }
            if all(value is not None for value in values.values()):
                return form % values
        for pattern, form in TEMPLATES:
            match = pattern.match(stripped)
            if not match:
                continue
            parts = [self.translate(part, record=False) for part in match.groups()]
            if all(part is not None for part in parts):
                return form.format(*parts)
        # Журнал из нескольких записей (например, встречи наставника) переводится построчно.
        if "\n" in stripped:
            lines = [
                self.translate(line, record=False) if line.strip() else line
                for line in stripped.split("\n")
            ]
            if all(line is not None for line in lines):
                return "\n".join(line for line in lines if line is not None)
        if record:
            self.missing.add((self.context, text))
        return None


@cache
def default_translator() -> Translator:
    """Переводчик с загруженным каталогом, общий для процесса."""
    return Translator(load_catalog(), interface_forms())


def english_value(field: str, source: str, engine: Translator | None = None) -> str | None:
    """Английское значение поля: транслитерация для имён, каталог для прочего."""
    if field in NAME_FIELDS:
        return transliterate(source)
    return (engine or default_translator()).translate(source, record=engine is not None)


def fill_missing_english(sender: type, instance: Any, **kwargs: Any) -> None:
    """Перед сохранением заполняет пустые английские поля системных текстов."""
    from modeltranslation.translator import translator

    for name in translator.get_options_for_model(sender).get_field_names():
        source = getattr(instance, f"{name}_ru", None)
        if source and not getattr(instance, f"{name}_en", None):
            value = english_value(name, source)
            if value:
                setattr(instance, f"{name}_en", value)
