"""Типы настроек предметной области."""

from __future__ import annotations

from typing import TypedDict


class ReservixSettings(TypedDict):
    """Настройки предметной области."""

    #: Границы шкалы оценки компетенций.
    COMPETENCY_SCALE_MIN: int
    COMPETENCY_SCALE_MAX: int

    #: Пороговые значения композитного индекса готовности, баллы 0–100.
    READINESS_THRESHOLD_HIGH: int
    READINESS_THRESHOLD_MEDIUM: int

    #: Границы зон матрицы 9-box по осям результативности и потенциала.
    NINE_BOX_LOW_MAX: int
    NINE_BOX_MID_MAX: int

    #: Предельное отношение согласованности матрицы парных сравнений в методе анализа иерархий.
    AHP_MAX_CONSISTENCY_RATIO: float

    #: Горизонт планирования преемственности, месяцев.
    SUCCESSION_HORIZON_MONTHS: int

    #: За сколько дней система предупреждает о приближении срока мероприятия.
    ACTIVITY_DUE_SOON_DAYS: int

    #: Постраничная навигация.
    PAGE_SIZE: int
    PAGE_SIZE_CHOICES: list[int]

    #: Срок хранения записей журнала аудита, дней.
    AUDIT_RETENTION_DAYS: int


def get_reservix_settings() -> ReservixSettings:
    """Возвращает типизированные настройки предметной области."""
    from django.conf import settings

    return settings.RESERVIX
