"""Валидаторы паролей."""

from __future__ import annotations

import itertools
import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from django.utils.translation import ngettext


class ComplexityValidator:
    """Требование к составу символов пароля."""

    #: Значения, недопустимые независимо от состава символов.
    FORBIDDEN_SUBSTRINGS = frozenset(
        {"reservix", "kadrovyi", "rezerv", "password", "пароль", "qwerty", "admin"}
    )

    def __init__(self, min_groups: int = 3) -> None:
        self.min_groups = min_groups

    def validate(self, password: str, user=None) -> None:
        lowered = password.lower()

        for forbidden in self.FORBIDDEN_SUBSTRINGS:
            if forbidden in lowered:
                raise ValidationError(
                    _("Пароль не должен содержать «%(value)s»"),
                    code="password_forbidden_substring",
                    params={"value": forbidden},
                )

        groups = sum(
            bool(pattern.search(password))
            for pattern in (
                re.compile(r"[a-zа-яё]"),
                re.compile(r"[A-ZА-ЯЁ]"),
                re.compile(r"\d"),
                re.compile(r"[^\w\s]"),
            )
        )

        if groups < self.min_groups:
            raise ValidationError(
                ngettext(
                    "Пароль должен содержать символы не менее чем из %(count)d группы: "
                    "строчные буквы, прописные буквы, цифры, специальные символы.",
                    "Пароль должен содержать символы не менее чем из %(count)d групп: "
                    "строчные буквы, прописные буквы, цифры, специальные символы.",
                    self.min_groups,
                ),
                code="password_not_complex",
                params={"count": self.min_groups},
            )

        # Последовательности вида «12345» и «abcde» формально проходят проверку
        # на разнообразие, но подбираются словарём за секунды.
        if self._has_sequence(lowered, length=5):
            raise ValidationError(
                _("Пароль не должен содержать длинных последовательностей символов"),
                code="password_has_sequence",
            )

    @staticmethod
    def _has_sequence(value: str, length: int = 5) -> bool:
        """Определяет наличие возрастающей или убывающей последовательности кодов."""
        if len(value) < length:
            return False
        run_up = run_down = 1
        for previous, current in itertools.pairwise(value):
            delta = ord(current) - ord(previous)
            run_up = run_up + 1 if delta == 1 else 1
            run_down = run_down + 1 if delta == -1 else 1
            if run_up >= length or run_down >= length:
                return True
        return False

    def get_help_text(self) -> str:
        return _(
            "Пароль должен содержать символы не менее чем из трёх групп: "
            "строчные буквы, прописные буквы, цифры, специальные символы."
        )
