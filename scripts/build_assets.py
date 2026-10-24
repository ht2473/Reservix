#!/usr/bin/env python
"""
Сборка статических ресурсов Reservix.

Использование::

    uv run python scripts/build_assets.py            # сборка
    uv run python scripts/build_assets.py --minify   # сборка со сжатием
    uv run python scripts/build_assets.py --check    # проверка актуальности
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CSS_SRC = BASE_DIR / "static" / "src" / "css"
JS_SRC = BASE_DIR / "static" / "src" / "js"
CSS_OUT = BASE_DIR / "static" / "css" / "app.css"
JS_OUT = BASE_DIR / "static" / "js" / "app.js"

BANNER = """/*!
 * Reservix — система формирования и развития кадрового резерва
 *
 * ВНИМАНИЕ: файл собран автоматически из static/src/{kind}/.
 * Не редактируйте его напрямую — правки будут потеряны при следующей сборке.
 * Источники: {sources}
 */
"""


def collect(source_dir: Path, suffix: str) -> list[Path]:
    """Возвращает исходные файлы в порядке числового префикса."""
    files = sorted(source_dir.glob(f"*{suffix}"))
    prefixed = [path for path in files if re.match(r"^\d{2}-", path.name)]
    rest = [path for path in files if path not in prefixed]
    return prefixed + rest


def bundle(sources: list[Path], kind: str) -> str:
    """Объединяет файлы в один текст с разделителями-комментариями."""
    names = ", ".join(path.name for path in sources)
    parts = [BANNER.format(kind=kind, sources=names)]

    for path in sources:
        parts.append(f"\n/* ===== {path.name} ".ljust(76, "=") + " */\n")
        parts.append(path.read_text(encoding="utf-8").strip())
        parts.append("\n")

    return "".join(parts)


def minify_css(source: str) -> str:
    """Упрощённое сжатие CSS."""
    banner_match = re.match(r"/\*!.*?\*/", source, flags=re.DOTALL)
    banner = banner_match.group(0) if banner_match else ""
    body = source[len(banner) :]

    body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    body = re.sub(r"\s+", " ", body)
    body = re.sub(r"\s*([{}:;,>~])\s*", r"\1", body)
    body = re.sub(r";}", "}", body)
    # Пробел после двоеточия сохраняется внутри значений вида "0 0 0 3px": предыдущее правило его
    # уже убрало, восстанавливаем читаемость там, где двоеточие является частью селектора
    # псевдокласса.
    body = re.sub(r"(?<=[a-z-])\):", "):", body)

    return banner + body.strip()


def minify_js(source: str) -> str:
    """Упрощённое сжатие JavaScript: удаление комментариев целой строки."""
    lines = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") and not stripped.startswith("//!"):
            continue
        if not stripped:
            continue
        lines.append(line)
    return "\n".join(lines)


def write_bundle(content: str, target: Path, *, check: bool) -> bool:
    """Записывает бандл. В режиме проверки только сравнивает содержимое."""
    target.parent.mkdir(parents=True, exist_ok=True)

    if check:
        if not target.exists():
            print(f"[отсутствует] {target.relative_to(BASE_DIR)}")
            return False
        if target.read_text(encoding="utf-8") != content:
            print(f"[устарел]     {target.relative_to(BASE_DIR)}")
            return False
        print(f"[актуален]    {target.relative_to(BASE_DIR)}")
        return True

    target.write_text(content, encoding="utf-8", newline="\n")
    size_kb = len(content.encode("utf-8")) / 1024
    print(f"[собран]      {target.relative_to(BASE_DIR)} — {size_kb:.1f} КБ")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Сборка стилей и сценариев Reservix")
    parser.add_argument("--minify", action="store_true", help="сжать результат")
    parser.add_argument(
        "--check",
        action="store_true",
        help="проверить актуальность бандлов без записи (используется в CI)",
    )
    args = parser.parse_args()

    css_sources = collect(CSS_SRC, ".css")
    js_sources = collect(JS_SRC, ".js")

    if not css_sources:
        print(f"Не найдены исходные стили в {CSS_SRC}", file=sys.stderr)
        return 1

    css = bundle(css_sources, "css")
    js = bundle(js_sources, "js") if js_sources else ""

    if args.minify:
        css = minify_css(css)
        js = minify_js(js) if js else ""

    ok = write_bundle(css, CSS_OUT, check=args.check)
    if js:
        ok = write_bundle(js, JS_OUT, check=args.check) and ok

    if args.check and not ok:
        print(
            "\nБандлы не соответствуют исходным файлам. "
            "Выполните: uv run python scripts/build_assets.py",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
