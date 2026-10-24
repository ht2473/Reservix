"""Валидаторы предметной области."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _

from core.settings_types import get_reservix_settings

# ---------------------------------------------------------------------------
# Персональные данные
# ---------------------------------------------------------------------------
#: Телефон в российском формате.
phone_validator = RegexValidator(
    regex=r"^\+?[78][\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}$",
    message=_("Укажите телефон в формате +7 900 111-22-33"),
)

#: Фамилия, имя, отчество. Кириллица, дефис и апостроф; латиница допускается
#: для сотрудников иностранных представительств.
name_validator = RegexValidator(
    regex=r"^[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-' ]{0,98}[А-Яа-яЁёA-Za-z.]$",
    message=_("Допустимы буквы, дефис, апостроф и пробел"),
)

#: Код элемента справочника: латиница, цифры, дефис и подчёркивание.
code_validator = RegexValidator(
    regex=r"^[a-z][a-z0-9_\-]{1,62}[a-z0-9]$",
    message=_("Код начинается с латинской буквы и содержит только строчные буквы, цифры, дефис"),
)


def normalize_phone(value: str) -> str:
    """Приводит телефон к каноническому виду ``+7XXXXXXXXXX``."""
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return ""
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return f"+{digits}"


def validate_birth_date(value) -> None:
    """Проверяет правдоподобность даты рождения."""
    from django.utils import timezone

    today = timezone.localdate()
    age = (today - value).days / 365.25

    if value > today:
        raise ValidationError(_("Дата рождения не может быть в будущем"))
    if age < 14:
        raise ValidationError(_("Возраст сотрудника не может быть меньше 14 лет"))
    if age > 100:
        raise ValidationError(_("Проверьте дату рождения: возраст превышает 100 лет"))


def validate_period(start, end, *, field_name: str = "end_date") -> None:
    """Проверяет, что окончание периода не раньше начала."""
    if start and end and end < start:
        raise ValidationError({field_name: _("Дата окончания не может быть раньше даты начала")})


def validate_score(value: Any, *, minimum: float = 0, maximum: float = 100) -> None:
    """Проверяет попадание оценки в допустимый диапазон."""
    if value is None:
        return
    if not (minimum <= float(value) <= maximum):
        raise ValidationError(
            _("Значение должно быть в диапазоне от %(min)s до %(max)s"),
            params={"min": minimum, "max": maximum},
        )


def validate_competency_level(value: Any) -> None:
    """Проверяет уровень владения компетенцией по шкале из настроек."""
    options = get_reservix_settings()
    minimum = options["COMPETENCY_SCALE_MIN"]
    maximum = options["COMPETENCY_SCALE_MAX"]
    if value is None:
        return
    if not (minimum <= float(value) <= maximum):
        raise ValidationError(
            _("Уровень компетенции указывается по шкале от %(min)s до %(max)s"),
            params={"min": minimum, "max": maximum},
        )


def validate_weight(value: Any) -> None:
    """Проверяет вес критерия: доля в диапазоне от 0 до 1."""
    if value is None:
        return
    if not (0 <= float(value) <= 1):
        raise ValidationError(_("Вес критерия указывается долей в диапазоне от 0 до 1"))


# ---------------------------------------------------------------------------
# Файлы
# ---------------------------------------------------------------------------
@deconstructible
class FileSizeValidator:
    """Ограничение размера загружаемого файла."""

    def __init__(self, max_bytes: int | None = None) -> None:
        self.max_bytes = max_bytes or settings.MAX_UPLOAD_SIZE

    def __call__(self, value) -> None:
        if value.size > self.max_bytes:
            raise ValidationError(
                _("Размер файла %(actual)s МБ превышает допустимые %(limit)s МБ"),
                params={
                    "actual": round(value.size / 1024 / 1024, 1),
                    "limit": round(self.max_bytes / 1024 / 1024, 1),
                },
            )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FileSizeValidator) and self.max_bytes == other.max_bytes


@deconstructible
class FileExtensionValidator:
    """Ограничение расширения загружаемого файла."""

    def __init__(self, allowed: list[str] | None = None) -> None:
        self.allowed = [ext.lower() for ext in (allowed or settings.ALLOWED_UPLOAD_EXTENSIONS)]

    def __call__(self, value) -> None:
        suffix = Path(value.name).suffix.lower()
        if suffix not in self.allowed:
            raise ValidationError(
                _("Формат «%(ext)s» не разрешён. Допустимые форматы: %(allowed)s"),
                params={
                    "ext": suffix or _("без расширения"),
                    "allowed": ", ".join(self.allowed),
                },
            )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FileExtensionValidator) and self.allowed == other.allowed


def validate_safe_filename(name: str) -> None:
    """Отклоняет имена файлов, пригодные для выхода за пределы каталога хранения."""
    if not name or name in {".", ".."}:
        raise ValidationError(_("Некорректное имя файла"))
    if "/" in name or "\\" in name or "\x00" in name:
        raise ValidationError(_("Имя файла содержит недопустимые символы"))
    if name.startswith("."):
        raise ValidationError(_("Имя файла не может начинаться с точки"))
