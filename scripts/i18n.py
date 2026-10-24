#!/usr/bin/env python
"""
Работа с каталогами перевода без GNU gettext.

Использование::

    uv run python scripts/i18n.py extract     # обновить .po по исходным текстам
    uv run python scripts/i18n.py compile     # собрать .mo из .po
    uv run python scripts/i18n.py check       # доля переведённого, для CI
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import struct
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOCALE_DIR = BASE_DIR / "locale"

#: Язык оригинала. Его каталог заполняется тождественно и служит проверкой:
#: пустая строка перевода здесь означает опечатку в исходном тексте.
SOURCE_LANGUAGE = "ru"

#: Языки, для которых ведутся каталоги.
LANGUAGES = ("ru", "en")

#: Каталоги, не содержащие исходных текстов проекта.
SKIP_DIRS = {
    ".venv",
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "staticfiles",
    "locale",
    "logs",
    "media",
    "migrations",
    "deploy",
    "docs",
    "tests",
}

#: Функции перевода. Позиция аргументов различается, поэтому разбираются
#: отдельно: контекст идёт первым, множественная форма — второй.
PLAIN_FUNCS = {"_", "gettext", "gettext_lazy", "ugettext", "ugettext_lazy", "gettext_noop"}
CONTEXT_FUNCS = {"pgettext", "pgettext_lazy"}
PLURAL_FUNCS = {"ngettext", "ngettext_lazy"}
CONTEXT_PLURAL_FUNCS = {"npgettext", "npgettext_lazy"}
ALL_FUNCS = PLAIN_FUNCS | CONTEXT_FUNCS | PLURAL_FUNCS | CONTEXT_PLURAL_FUNCS


# ---------------------------------------------------------------------------
# Модель каталога
# ---------------------------------------------------------------------------
@dataclass
class Message:
    """Единица каталога."""

    msgid: str
    msgid_plural: str | None = None
    context: str | None = None
    translations: dict[int, str] = field(default_factory=dict)
    locations: list[str] = field(default_factory=list)
    obsolete: bool = False

    @property
    def key(self) -> tuple[str | None, str]:
        return (self.context, self.msgid)

    @property
    def is_translated(self) -> bool:
        return bool(self.translations.get(0))


# ---------------------------------------------------------------------------
# Разбор исходных текстов
# ---------------------------------------------------------------------------
def _literal(node: ast.AST) -> str | None:
    """Возвращает значение строкового литерала либо None для выражения."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # Соседние литералы, записанные в скобках через перенос строки, Python
    # склеивает ещё на этапе разбора, поэтому отдельная обработка не нужна.
    return None


def collect_from_python(source: str, relpath: str, catalog: OrderedDict) -> None:
    """Отбирает вызовы функций перевода в модуле Python."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:  # pragma: no cover - защита от повреждённого файла
        print(f"  пропущен {relpath}: {error}", file=sys.stderr)
        return

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name not in ALL_FUNCS:
            continue

        args = [_literal(argument) for argument in node.args]
        location = f"{relpath}:{node.lineno}"

        if name in CONTEXT_FUNCS and len(args) >= 2:
            _store(catalog, args[1], location, context=args[0])
        elif name in CONTEXT_PLURAL_FUNCS and len(args) >= 3:
            _store(catalog, args[1], location, context=args[0], plural=args[2])
        elif name in PLURAL_FUNCS and len(args) >= 2:
            _store(catalog, args[0], location, plural=args[1])
        elif name in PLAIN_FUNCS and args:
            _store(catalog, args[0], location)


#: Разбор результата ``templatize``.
_QUOTED = r"(?:u?'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\")"
_CALL_RE = re.compile(
    rf"(?<![\w.])(n?p?gettext|_)\(\s*{_QUOTED}"
    rf"(?:\s*,\s*{_QUOTED})?(?:\s*,\s*{_QUOTED})?"
)
_UNESCAPE = {"\\n": "\n", "\\t": "\t", "\\r": "\r", "\\'": "'", '\\"': '"', "\\\\": "\\"}
_UNESCAPE_RE = re.compile(r"\\[ntr'\"\\]")


def _unescape(raw: str | None) -> str | None:
    if raw is None:
        return None
    return _UNESCAPE_RE.sub(lambda match: _UNESCAPE[match.group(0)], raw)


def collect_from_template(source: str, relpath: str, catalog: OrderedDict) -> None:
    """Отбирает переводимые строки шаблона через штатную функцию Django."""
    from django.utils.translation.template import templatize

    try:
        pseudo = templatize(source, origin=relpath)
    except Exception as error:  # pragma: no cover - синтаксическая ошибка шаблона
        print(f"  пропущен {relpath}: {error}", file=sys.stderr)
        return

    line_number = 1
    position = 0
    for match in _CALL_RE.finditer(pseudo):
        line_number += pseudo.count("\n", position, match.start())
        position = match.start()
        name = match.group(1)
        # Каждый аргумент занимает две группы — под одинарные и двойные
        # кавычки; заполнена всегда ровно одна из пары.
        groups = match.groups()
        first, second, third = (
            _unescape(groups[index] if groups[index] is not None else groups[index + 1])
            for index in (1, 3, 5)
        )
        location = f"{relpath}:{line_number}"

        if name in ("gettext", "_"):
            _store(catalog, first, location)
        elif name == "ngettext":
            _store(catalog, first, location, plural=second)
        elif name == "pgettext":
            _store(catalog, second, location, context=first)
        elif name == "npgettext":
            _store(catalog, second, location, context=first, plural=third)


def _store(
    catalog: OrderedDict,
    msgid: str | None,
    location: str,
    *,
    context: str | None = None,
    plural: str | None = None,
) -> None:
    """Добавляет сообщение в каталог, накапливая места использования."""
    if not msgid or not msgid.strip():
        return
    key = (context, msgid)
    message = catalog.get(key)
    if message is None:
        message = Message(msgid=msgid, context=context)
        catalog[key] = message
    if plural:
        message.msgid_plural = plural
    if location not in message.locations:
        message.locations.append(location)


def scan_sources() -> OrderedDict:
    """Обходит проект и собирает каталог сообщений."""
    catalog: OrderedDict[tuple[str | None, str], Message] = OrderedDict()
    python_files: list[tuple[Path, str]] = []
    template_files: list[tuple[Path, str]] = []

    for root, dirs, files in os.walk(BASE_DIR):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
        for name in sorted(files):
            path = Path(root) / name
            relpath = path.relative_to(BASE_DIR).as_posix()
            if name.endswith(".py") and not relpath.startswith("scripts/"):
                python_files.append((path, relpath))
            elif name.endswith((".html", ".txt")) and "templates/" in relpath:
                template_files.append((path, relpath))

    for path, relpath in python_files:
        collect_from_python(path.read_text(encoding="utf-8"), relpath, catalog)
    for path, relpath in template_files:
        collect_from_template(path.read_text(encoding="utf-8"), relpath, catalog)

    print(f"Просмотрено: {len(python_files)} модулей, {len(template_files)} шаблонов")
    print(f"Найдено сообщений: {len(catalog)}")
    return catalog


# ---------------------------------------------------------------------------
# Чтение и запись .po
# ---------------------------------------------------------------------------
_PO_TOKEN_RE = re.compile(r"^(#~\s*)?(msgctxt|msgid_plural|msgid|msgstr)(?:\[(\d+)\])?\s+(.*)$")


def _po_unquote(value: str) -> str:
    value = value.strip()
    if not (value.startswith('"') and value.endswith('"')):
        return value
    inner = value[1:-1]
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
    return re.sub(r"\\(.)", lambda m: escapes.get(m.group(1), m.group(1)), inner)


def read_po(path: Path) -> OrderedDict:
    """Читает каталог перевода."""
    catalog: OrderedDict[tuple[str | None, str], Message] = OrderedDict()
    if not path.exists():
        return catalog

    current: dict = {}
    field_name: str | None = None
    plural_index = 0
    obsolete = False

    def flush() -> None:
        nonlocal current, field_name, plural_index, obsolete
        msgid = current.get("msgid")
        if msgid:
            message = Message(
                msgid=msgid,
                msgid_plural=current.get("msgid_plural") or None,
                context=current.get("msgctxt") or None,
                translations={
                    index: text for index, text in current.get("msgstr", {}).items() if text
                },
                locations=current.get("locations", []),
                obsolete=obsolete,
            )
            catalog[message.key] = message
        current = {}
        field_name = None
        plural_index = 0
        obsolete = False

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()

        if not line:
            flush()
            continue

        if line.startswith("#:"):
            current.setdefault("locations", []).extend(line[2:].split())
            continue
        if line.startswith("#") and not line.startswith("#~"):
            continue

        match = _PO_TOKEN_RE.match(line)
        if match:
            if match.group(1):
                obsolete = True
            field_name = match.group(2)
            plural_index = int(match.group(3) or 0)
            text = _po_unquote(match.group(4))
            if field_name == "msgstr":
                current.setdefault("msgstr", {})[plural_index] = text
            else:
                current[field_name] = text
            continue

        # Продолжение многострочного значения.
        if line.startswith('"') or line.startswith('#~ "'):
            text = _po_unquote(line.removeprefix("#~ "))
            if field_name == "msgstr":
                current.setdefault("msgstr", {})
                current["msgstr"][plural_index] = current["msgstr"].get(plural_index, "") + text
            elif field_name:
                current[field_name] = current.get(field_name, "") + text

    flush()
    # Служебная запись заголовка каталога имеет пустой msgid и в словарь не
    # попадает: она формируется заново при каждой записи.
    return catalog


def _po_quote(value: str) -> list[str]:
    """Оформляет строку в синтаксисе .po."""
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t").replace("\r", "\\r")
    )
    if "\n" not in escaped:
        return [f'"{escaped}"']

    parts = escaped.split("\n")
    lines = ['""']
    for index, part in enumerate(parts):
        suffix = "\\n" if index < len(parts) - 1 else ""
        if part or suffix:
            lines.append(f'"{part}{suffix}"')
    return lines


PO_HEADER = """\
# Каталог перевода Reservix.
#
# Файл собирается сценарием scripts/i18n.py: набор GNU gettext в окружении
# проекта не требуется. Переводы правятся вручную, места использования и
# состав сообщений обновляются командой `just messages`.
#
msgid ""
msgstr ""
"Project-Id-Version: Reservix\\n"
"Report-Msgid-Bugs-To: \\n"
"MIME-Version: 1.0\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Content-Transfer-Encoding: 8bit\\n"
"Language: {language}\\n"
"Plural-Forms: {plural_forms}\\n"
"""

PLURAL_FORMS = {
    "ru": "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && "
    "(n%100<12 || n%100>14) ? 1 : 2);",
    "en": "nplurals=2; plural=(n != 1);",
}

PLURAL_COUNT = {"ru": 3, "en": 2}


def write_po(path: Path, language: str, messages: list[Message]) -> None:
    """Записывает каталог перевода."""
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks = [PO_HEADER.format(language=language, plural_forms=PLURAL_FORMS[language])]
    forms = PLURAL_COUNT[language]

    for message in messages:
        block: list[str] = []
        for index in range(0, len(message.locations), 4):
            block.append("#: " + " ".join(message.locations[index : index + 4]))
        if message.context:
            block.append("msgctxt " + "\n".join(_po_quote(message.context)))
        block.append("msgid " + "\n".join(_po_quote(message.msgid)))

        if message.msgid_plural:
            block.append("msgid_plural " + "\n".join(_po_quote(message.msgid_plural)))
            for index in range(forms):
                text = message.translations.get(index, "")
                block.append(f"msgstr[{index}] " + "\n".join(_po_quote(text)))
        else:
            text = message.translations.get(0, "")
            block.append("msgstr " + "\n".join(_po_quote(text)))

        # Устаревшие сообщения помечаются целиком: gettext их пропускает,
        # а перевод остаётся под рукой, если строка вернётся.
        if message.obsolete:
            body = "\n".join(f"#~ {line}" for line in "\n".join(block).splitlines())
            chunks.append("#. строка больше не встречается в исходных текстах\n" + body)
        else:
            chunks.append("\n".join(block))

    path.write_text("\n\n".join(chunks) + "\n", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# Сборка .mo
# ---------------------------------------------------------------------------
def write_mo(path: Path, messages: list[Message], language: str) -> int:
    """Собирает двоичный каталог в формате GNU gettext."""
    entries: dict[str, str] = {
        "": "Content-Type: text/plain; charset=UTF-8\n"
        f"Language: {language}\n"
        f"Plural-Forms: {PLURAL_FORMS[language]}\n"
    }

    for message in messages:
        if message.obsolete or not message.is_translated:
            continue
        key = message.msgid
        if message.context:
            key = f"{message.context}\x04{key}"
        if message.msgid_plural:
            key = f"{key}\x00{message.msgid_plural}"
            forms = PLURAL_COUNT[language]
            value = "\x00".join(message.translations.get(index, "") for index in range(forms))
        else:
            value = message.translations[0]
        entries[key] = value

    items = sorted(entries.items())
    count = len(items)
    keys_offset = 7 * 4
    values_offset = keys_offset + count * 8
    strings_offset = values_offset + count * 8

    key_table = bytearray()
    value_table = bytearray()
    payload = bytearray()

    for key, _value in items:
        key_bytes = key.encode("utf-8")
        key_table += struct.pack("<II", len(key_bytes), strings_offset + len(payload))
        payload += key_bytes + b"\x00"

    for _key, value in items:
        value_bytes = value.encode("utf-8")
        value_table += struct.pack("<II", len(value_bytes), strings_offset + len(payload))
        payload += value_bytes + b"\x00"

    header = struct.pack(
        "<Iiiiiii",
        0x950412DE,  # магическое число формата, порядок байтов little-endian
        0,  # версия формата
        count,
        keys_offset,
        values_offset,
        0,  # размер хеш-таблицы: поиск выполняется перебором по сортировке
        strings_offset,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + bytes(key_table) + bytes(value_table) + bytes(payload))
    return count


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------
def merge(found: OrderedDict, existing: OrderedDict, language: str) -> list[Message]:
    """Совмещает найденные сообщения с уже переведёнными."""
    result: list[Message] = []
    for key, source in found.items():
        # Копия на каждый язык: один и тот же набор найденных сообщений раскладывается по
        # нескольким каталогам, и перевод одного языка не должен протекать в другой.
        message = Message(
            msgid=source.msgid,
            msgid_plural=source.msgid_plural,
            context=source.context,
            locations=list(source.locations),
        )
        previous = existing.get(key)
        if previous is not None:
            message.translations = dict(previous.translations)
        elif language == SOURCE_LANGUAGE:
            # Каталог языка оригинала заполняется тождественно: перевод там не нужен, но пустой
            # msgstr заставил бы gettext возвращать msgid и скрыл бы настоящие пропуски в других
            # языках.
            if message.msgid_plural:
                message.translations = {
                    index: message.msgid_plural if index else message.msgid
                    for index in range(PLURAL_COUNT[language])
                }
            else:
                message.translations = {0: message.msgid}
        result.append(message)

    for key, message in existing.items():
        if key not in found and message.is_translated:
            message.obsolete = True
            message.locations = []
            result.append(message)

    return result


def command_extract(_: argparse.Namespace) -> int:
    found = scan_sources()
    for language in LANGUAGES:
        path = LOCALE_DIR / language / "LC_MESSAGES" / "django.po"
        existing = read_po(path)
        merged = merge(found, existing, language)
        write_po(path, language, merged)
        translated = sum(1 for message in merged if not message.obsolete and message.is_translated)
        active = sum(1 for message in merged if not message.obsolete)
        print(f"  {language}: {translated}/{active} переведено → {path.relative_to(BASE_DIR)}")
    return 0


def command_compile(_: argparse.Namespace) -> int:
    for language in LANGUAGES:
        po_path = LOCALE_DIR / language / "LC_MESSAGES" / "django.po"
        if not po_path.exists():
            print(f"  {language}: каталог не найден, пропущен", file=sys.stderr)
            continue
        messages = list(read_po(po_path).values())
        mo_path = po_path.with_suffix(".mo")
        count = write_mo(mo_path, messages, language)
        print(f"  {language}: {count} сообщений → {mo_path.relative_to(BASE_DIR)}")
    return 0


def command_check(args: argparse.Namespace) -> int:
    status = 0
    for language in LANGUAGES:
        po_path = LOCALE_DIR / language / "LC_MESSAGES" / "django.po"
        messages = [m for m in read_po(po_path).values() if not m.obsolete]
        if not messages:
            print(f"  {language}: каталог пуст", file=sys.stderr)
            status = 1
            continue
        missing = [m for m in messages if not m.is_translated]
        share = 100 * (len(messages) - len(missing)) / len(messages)
        print(f"  {language}: {share:.1f} % ({len(missing)} без перевода)")
        if args.verbose:
            for message in missing[: args.limit]:
                print(f"      {message.msgid[:90]}")
        if missing:
            status = 1
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="Каталоги перевода Reservix")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("extract", help="обновить .po по исходным текстам")
    subparsers.add_parser("compile", help="собрать .mo из .po")
    check = subparsers.add_parser("check", help="проверить полноту перевода")
    check.add_argument("--verbose", action="store_true", help="показать непереведённые строки")
    check.add_argument("--limit", type=int, default=40, help="сколько строк показать")

    args = parser.parse_args()

    # Сборка .mo обходится без Django: она выполняется и при сборке образа,
    # где зависимостей окружения разработки нет.
    if args.command != "compile":
        import django

        sys.path.insert(0, str(BASE_DIR))
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
        django.setup()

    handlers = {"extract": command_extract, "compile": command_compile, "check": command_check}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
