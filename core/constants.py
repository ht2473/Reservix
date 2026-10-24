"""Сквозные перечисления предметной области."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    """Роли пользователей системы."""

    ADMIN = "admin", _("Администратор")
    HR = "hr", _("HR-специалист")
    MANAGER = "manager", _("Руководитель")
    MENTOR = "mentor", _("Наставник")
    EMPLOYEE = "employee", _("Сотрудник")


#: Роли, обладающие доступом к персональным данным всех сотрудников.
PRIVILEGED_ROLES = frozenset({Role.ADMIN, Role.HR})

#: Роли, для которых доступна панель администрирования системы.
ADMIN_ROLES = frozenset({Role.ADMIN})

#: Роли, которые могут принимать решения о составе кадрового резерва.
DECISION_ROLES = frozenset({Role.ADMIN, Role.HR})

#: Роли, наделённые правом оценивать сотрудников.
ASSESSOR_ROLES = frozenset({Role.ADMIN, Role.HR, Role.MANAGER, Role.MENTOR})


class ScopeLevel(models.TextChoices):
    """Область видимости данных, назначаемая пользователю."""

    ORGANIZATION = "organization", _("Вся организация")
    BRANCH = "branch", _("Филиал")
    DEPARTMENT = "department", _("Подразделение")
    SUBORDINATES = "subordinates", _("Непосредственные подчинённые")
    SELF = "self", _("Только свои данные")


class ReadinessLevel(models.TextChoices):
    """Уровень готовности резервиста к назначению."""

    HIGH = "high", _("Готов к назначению")
    MEDIUM = "medium", _("Готов в течение года")
    LOW = "low", _("Требуется развитие")

    @classmethod
    def from_index(cls, value: float, thresholds: tuple[int, int] = (75, 55)) -> str:
        """Определяет уровень готовности по значению композитного индекса."""
        high, medium = thresholds
        if value >= high:
            return cls.HIGH
        if value >= medium:
            return cls.MEDIUM
        return cls.LOW


class NineBoxCell(models.TextChoices):
    """Ячейки матрицы 9-box."""

    # Верхний ряд — высокая результативность
    STAR = "star", _("Звезда")
    HIGH_PERFORMER = "high_performer", _("Высокая результативность")
    TRUSTED_PROFESSIONAL = "trusted_professional", _("Надёжный профессионал")
    # Средний ряд
    HIGH_POTENTIAL = "high_potential", _("Высокий потенциал")
    CORE_PLAYER = "core_player", _("Ключевой сотрудник")
    SOLID_CONTRIBUTOR = "solid_contributor", _("Стабильный исполнитель")
    # Нижний ряд — низкая результативность
    ENIGMA = "enigma", _("Нераскрытый потенциал")
    INCONSISTENT = "inconsistent", _("Нестабильный результат")
    UNDERPERFORMER = "underperformer", _("Зона риска")


#: Соответствие координат матрицы (строка результативности, столбец потенциала)
#: и ячейки. Индексы 0 — низкий уровень, 1 — средний, 2 — высокий.
NINE_BOX_MATRIX: dict[tuple[int, int], str] = {
    (2, 0): NineBoxCell.TRUSTED_PROFESSIONAL,
    (2, 1): NineBoxCell.HIGH_PERFORMER,
    (2, 2): NineBoxCell.STAR,
    (1, 0): NineBoxCell.SOLID_CONTRIBUTOR,
    (1, 1): NineBoxCell.CORE_PLAYER,
    (1, 2): NineBoxCell.HIGH_POTENTIAL,
    (0, 0): NineBoxCell.UNDERPERFORMER,
    (0, 1): NineBoxCell.INCONSISTENT,
    (0, 2): NineBoxCell.ENIGMA,
}

#: Ячейки, сотрудники которых рассматриваются как источник кадрового резерва.
TALENT_POOL_CELLS = frozenset(
    {
        NineBoxCell.STAR,
        NineBoxCell.HIGH_POTENTIAL,
        NineBoxCell.HIGH_PERFORMER,
    }
)


class Signal(models.TextChoices):
    """Сигнальная семантика для интерфейса."""

    READY = "ready", _("Норма")
    DEVELOP = "develop", _("Требует внимания")
    RISK = "risk", _("Риск")
    NEUTRAL = "neutral", _("Нейтрально")


class Weekday(models.IntegerChoices):
    """Дни недели для расписаний мероприятий развития."""

    MONDAY = 1, _("Понедельник")
    TUESDAY = 2, _("Вторник")
    WEDNESDAY = 3, _("Среда")
    THURSDAY = 4, _("Четверг")
    FRIDAY = 5, _("Пятница")
    SATURDAY = 6, _("Суббота")
    SUNDAY = 7, _("Воскресенье")


#: MIME-типы, разрешённые к загрузке в модуль документов. Проверяются вместе
#: с расширением файла: расширение подделывается тривиально, тип — сложнее.
ALLOWED_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/rtf",
        "application/zip",
        "text/plain",
        "text/csv",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)
