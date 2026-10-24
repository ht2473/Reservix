"""Кадровые данные сотрудников."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Self

from django.conf import settings
from django.core.validators import MaxValueValidator, MinLengthValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import (
    SoftDeleteManager,
    SoftDeleteModel,
    SoftDeleteQuerySet,
    TimeStampedModel,
    UUIDModel,
)
from core.utils import (
    calculate_age,
    calculate_tenure_months,
    format_tenure,
    full_name,
    initials,
    section_url,
)
from core.utils import short_name as short_name_of
from core.validators import name_validator, normalize_phone, phone_validator, validate_birth_date

if TYPE_CHECKING:
    from apps.org.models import PositionProfile


class EmployeeStatus(models.TextChoices):
    """Состояние трудовых отношений."""

    ACTIVE = "active", _("Работает")
    PROBATION = "probation", _("Испытательный срок")
    LEAVE = "leave", _("Длительный отпуск")
    SUSPENDED = "suspended", _("Отстранён")
    DISMISSED = "dismissed", _("Уволен")


#: Статусы, при которых сотрудник участвует в формировании резерва.
RESERVE_ELIGIBLE_STATUSES = frozenset({EmployeeStatus.ACTIVE, EmployeeStatus.PROBATION})


class EducationLevel(models.TextChoices):
    """Уровень образования. Порядок значений соответствует рангу в `EDUCATION_RANK`."""

    SECONDARY = "secondary", _("Среднее")
    VOCATIONAL = "vocational", _("Среднее профессиональное")
    BACHELOR = "bachelor", _("Бакалавриат")
    SPECIALIST = "specialist", _("Специалитет")
    MASTER = "master", _("Магистратура")
    POSTGRADUATE = "postgraduate", _("Аспирантура и выше")


#: Числовой ранг образования для сравнения с требованием профиля должности.
EDUCATION_RANK: dict[str, int] = {
    EducationLevel.SECONDARY: 1,
    EducationLevel.VOCATIONAL: 2,
    EducationLevel.BACHELOR: 3,
    EducationLevel.SPECIALIST: 4,
    EducationLevel.MASTER: 5,
    EducationLevel.POSTGRADUATE: 6,
}


class Mobility(models.TextChoices):
    """Готовность к перемещению."""

    NONE = "none", _("Не готов к перемещению")
    CITY = "city", _("В пределах города")
    REGION = "region", _("В пределах региона")
    COUNTRY = "country", _("По всей стране")


class AttritionRisk(models.TextChoices):
    """Риск ухода сотрудника."""

    LOW = "low", _("Низкий")
    MEDIUM = "medium", _("Средний")
    HIGH = "high", _("Высокий")


class EmployeeQuerySet(SoftDeleteQuerySet):
    """Типовые выборки кадрового состава."""

    def active(self) -> Self:
        """Сотрудники в действующих трудовых отношениях."""
        return self.filter(status__in=RESERVE_ELIGIBLE_STATUSES)

    def dismissed(self) -> Self:
        return self.filter(status=EmployeeStatus.DISMISSED)

    def reserve_eligible(self) -> Self:
        """Кандидаты, пригодные к включению в резерв."""
        return self.filter(status__in=RESERVE_ELIGIBLE_STATUSES, is_excluded_from_reserve=False)

    def internal(self) -> Self:
        """Сотрудники штата: внешние кандидаты исключаются."""
        return self.filter(is_external=False)

    def external(self) -> Self:
        """Внешние кандидаты — источник пополнения резерва извне."""
        return self.filter(is_external=True)

    def in_branch(self, branch_id: int) -> Self:
        return self.filter(branch_id=branch_id)

    def in_department(self, department_id: int) -> Self:
        return self.filter(department_id=department_id)

    def in_department_tree(self, department_id: int) -> Self:
        """Сотрудники подразделения и всех вложенных в него."""
        from apps.org.models import Department

        return self.filter(department_id__in=Department.objects.descendant_ids(department_id))

    def with_org(self) -> Self:
        """Подгружает оргструктуру: списки сотрудников иначе порождают N+1."""
        return self.select_related("branch", "department", "position", "position__grade")

    def with_user(self) -> Self:
        return self.select_related("user")

    def key_talent(self) -> Self:
        return self.filter(is_key_talent=True)

    def at_risk(self) -> Self:
        """Сотрудники с высоким риском ухода — материал для планов преемственности."""
        return self.filter(attrition_risk=AttritionRisk.HIGH).exclude(
            status=EmployeeStatus.DISMISSED
        )

    def search(self, term: str) -> Self:
        """Поиск по фамилии, имени, табельному номеру и адресу почты."""
        term = (term or "").strip()
        if not term:
            return self
        return self.filter(
            Q(last_name__icontains=term)
            | Q(first_name__icontains=term)
            | Q(middle_name__icontains=term)
            | Q(personnel_number__icontains=term)
            | Q(email__icontains=term)
        )

    def hired_before(self, moment: dt.date) -> Self:
        return self.filter(hire_date__lte=moment)


class Employee(UUIDModel, SoftDeleteModel, TimeStampedModel):
    """Карточка сотрудника."""

    # -- Связь с учётной записью -------------------------------------------
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("учётная запись"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employee_card",
        help_text=_("Пусто, если сотруднику не требуется доступ в систему"),
    )

    # -- Персональные данные ------------------------------------------------
    personnel_number = models.CharField(
        _("табельный номер"),
        max_length=20,
        unique=True,
        help_text=_("Уникальный номер сотрудника в кадровом учёте"),
    )
    last_name = models.CharField(
        _("фамилия"), max_length=100, validators=[name_validator, MinLengthValidator(2)]
    )
    first_name = models.CharField(
        _("имя"), max_length=100, validators=[name_validator, MinLengthValidator(2)]
    )
    middle_name = models.CharField(
        _("отчество"), max_length=100, blank=True, validators=[name_validator]
    )
    birth_date = models.DateField(
        _("дата рождения"), null=True, blank=True, validators=[validate_birth_date]
    )
    email = models.EmailField(_("рабочая почта"), max_length=255, blank=True, db_index=True)
    phone = models.CharField(_("телефон"), max_length=20, blank=True, validators=[phone_validator])
    photo = models.ImageField(
        _("фотография"),
        upload_to="employees/photos/%Y/%m/",
        null=True,
        blank=True,
        help_text=_("Портрет для карточки сотрудника и списков резерва"),
    )

    # -- Образование --------------------------------------------------------
    education_level = models.CharField(
        _("уровень образования"),
        max_length=20,
        choices=EducationLevel.choices,
        default=EducationLevel.BACHELOR,
        db_index=True,
    )
    education = models.TextField(
        _("образование"),
        blank=True,
        help_text=_("Учебное заведение, специальность, год окончания"),
    )

    # -- Место в организации ------------------------------------------------
    branch = models.ForeignKey(
        "org.Branch",
        verbose_name=_("филиал"),
        on_delete=models.PROTECT,
        related_name="employees",
        null=True,
        blank=True,
        help_text=_("Пусто у внешнего кандидата: он не входит в штат организации"),
    )
    department = models.ForeignKey(
        "org.Department",
        verbose_name=_("подразделение"),
        on_delete=models.PROTECT,
        related_name="employees",
        null=True,
        blank=True,
        help_text=_("Пусто у внешнего кандидата"),
    )
    position = models.ForeignKey(
        "org.Position",
        verbose_name=_("должность"),
        on_delete=models.PROTECT,
        related_name="employees",
        null=True,
        blank=True,
    )
    position_title = models.CharField(
        _("наименование должности"),
        max_length=255,
        blank=True,
        help_text=_("Заполняется, если должности нет в штатном расписании"),
    )
    manager = models.ForeignKey(
        "self",
        verbose_name=_("непосредственный руководитель"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subordinates",
    )

    # -- Трудовые отношения -------------------------------------------------
    status = models.CharField(
        _("статус"),
        max_length=20,
        choices=EmployeeStatus.choices,
        default=EmployeeStatus.ACTIVE,
        db_index=True,
    )
    hire_date = models.DateField(_("дата приёма"), db_index=True)
    position_since = models.DateField(
        _("в должности с"),
        null=True,
        blank=True,
        help_text=_("Дата назначения на текущую должность"),
    )
    dismissal_date = models.DateField(_("дата увольнения"), null=True, blank=True)

    # -- Опыт и потенциал ---------------------------------------------------
    prior_experience_months = models.PositiveSmallIntegerField(
        _("стаж до приёма, месяцев"),
        default=0,
        help_text=_("Профессиональный опыт, полученный вне организации"),
    )
    management_experience_months = models.PositiveSmallIntegerField(
        _("управленческий стаж, месяцев"),
        default=0,
        help_text=_("Суммарный опыт руководства людьми"),
    )
    max_subordinates = models.PositiveSmallIntegerField(
        _("наибольшее число подчинённых"),
        default=0,
        help_text=_("Максимум за карьеру; проверяется требованием профиля должности"),
    )
    mobility = models.CharField(
        _("мобильность"),
        max_length=20,
        choices=Mobility.choices,
        default=Mobility.CITY,
    )
    attrition_risk = models.CharField(
        _("риск ухода"),
        max_length=20,
        choices=AttritionRisk.choices,
        default=AttritionRisk.LOW,
        db_index=True,
    )
    is_external = models.BooleanField(
        _("внешний кандидат"),
        default=False,
        db_index=True,
        help_text=_(
            "Кандидат вне штата организации: рассматривается в резерв, "
            "но не имеет подразделения и трудового стажа в организации"
        ),
    )
    is_key_talent = models.BooleanField(
        _("ключевой сотрудник"),
        default=False,
        db_index=True,
        help_text=_("Отмечается по итогам сессии калибровки"),
    )
    is_excluded_from_reserve = models.BooleanField(
        _("исключён из рассмотрения"),
        default=False,
        help_text=_("Решение HR-специалиста: сотрудник не рассматривается в резерв"),
    )
    exclusion_note = models.CharField(_("основание исключения"), max_length=255, blank=True)
    notes = models.TextField(_("примечания"), blank=True)

    # -- Наследие исходной базы --------------------------------------------
    legacy_id = models.PositiveIntegerField(
        _("идентификатор в исходной базе"),
        null=True,
        blank=True,
        unique=True,
        editable=False,
        help_text=_("Ссылка на строку таблицы candidate исходной базы данных"),
    )

    # Менеджер по умолчанию скрывает мягко удалённые карточки: признак удаления и признак
    # увольнения различны — уволенный сотрудник остаётся в истории и отчётности, удалённая карточка
    # не должна попадать никуда.
    objects = SoftDeleteManager.from_queryset(EmployeeQuerySet)()
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("сотрудник")
        verbose_name_plural = _("сотрудники")
        ordering = ["last_name", "first_name", "middle_name"]
        indexes = [
            models.Index(fields=["branch", "department"], name="employee_org_idx"),
            models.Index(fields=["status", "is_key_talent"], name="employee_status_talent_idx"),
            models.Index(fields=["last_name", "first_name"], name="employee_name_idx"),
            models.Index(fields=["position", "status"], name="employee_position_idx"),
        ]
        constraints = [
            # Дата увольнения обязательна для уволенного и бессмысленна для работающего: без этого
            # правила в отчётах появляются «уволенные без даты», по которым невозможно посчитать
            # текучесть.
            models.CheckConstraint(
                condition=(
                    Q(status="dismissed", dismissal_date__isnull=False)
                    | ~Q(status="dismissed") & Q(dismissal_date__isnull=True)
                ),
                name="employee_dismissal_date_consistent",
            ),
            models.CheckConstraint(
                condition=(
                    Q(dismissal_date__isnull=True) | Q(dismissal_date__gte=models.F("hire_date"))
                ),
                name="employee_dismissal_after_hire",
            ),
        ]

    # -- Представление ------------------------------------------------------
    def __str__(self) -> str:
        return self.short_name

    def clean(self) -> None:
        super().clean()
        if self.phone:
            self.phone = normalize_phone(self.phone)
        self.email = (self.email or "").lower().strip()

    def get_absolute_url(self) -> str:
        return section_url("people:employee_detail", public_id=self.public_id)

    @property
    def full_name(self) -> str:
        return full_name(self.last_name, self.first_name, self.middle_name)

    @property
    def short_name(self) -> str:
        return short_name_of(self.last_name, self.first_name, self.middle_name)

    @property
    def initials(self) -> str:
        return initials(self.first_name, self.middle_name)

    @property
    def title(self) -> str:
        """Наименование должности: из штатного расписания либо из карточки."""
        if self.position is not None:
            return self.position.name
        return self.position_title or str(_("Должность не указана"))

    # -- Производные характеристики ----------------------------------------
    @property
    def age(self) -> int | None:
        return calculate_age(self.birth_date) if self.birth_date else None

    @property
    def tenure_months(self) -> int:
        """Стаж в организации на сегодня или на дату увольнения."""
        if self.is_external:
            return 0
        return calculate_tenure_months(self.hire_date, self.dismissal_date)

    @property
    def tenure_display(self) -> str:
        return format_tenure(self.tenure_months)

    @property
    def position_tenure_months(self) -> int:
        """Стаж в текущей должности."""
        if self.is_external:
            return 0
        start = self.position_since or self.hire_date
        return calculate_tenure_months(start, self.dismissal_date)

    @property
    def total_experience_months(self) -> int:
        """Совокупный профессиональный опыт: до приёма и в организации."""
        return self.prior_experience_months + self.tenure_months

    @property
    def is_internal(self) -> bool:
        """Состоит ли кандидат в штате организации."""
        return not self.is_external

    @property
    def total_experience_years(self) -> int:
        return self.total_experience_months // 12

    @property
    def education_rank(self) -> int:
        return EDUCATION_RANK.get(self.education_level, 0)

    @property
    def has_management_experience(self) -> bool:
        return self.management_experience_months > 0

    @property
    def is_employed(self) -> bool:
        return self.status in RESERVE_ELIGIBLE_STATUSES

    @property
    def can_join_reserve(self) -> bool:
        """Пригодность к включению в резерв по кадровым основаниям."""
        return self.is_employed and not self.is_excluded_from_reserve and not self.is_deleted

    def matches_profile(self, profile: PositionProfile) -> dict[str, bool]:
        """Сопоставляет кадровые данные с формальными требованиями профиля."""
        return {
            "education": self.education_rank >= profile.education_rank,
            "total_experience": (
                self.total_experience_months >= profile.min_total_experience_years * 12
            ),
            "relevant_experience": (
                self.position_tenure_months >= profile.min_relevant_experience_years * 12
            ),
            "company_tenure": self.tenure_months >= profile.min_company_tenure_months,
            "management": (
                not profile.requires_management_experience or self.has_management_experience
            ),
            "team_size": self.max_subordinates >= profile.min_subordinates,
        }

    def profile_match_ratio(self, profile: PositionProfile) -> float:
        """Доля выполненных формальных требований профиля, 0–1."""
        checks = self.matches_profile(profile)
        return sum(checks.values()) / len(checks) if checks else 0.0

    def dismiss(self, *, on_date: dt.date | None = None, reason: str = "") -> None:
        """Оформляет увольнение."""
        moment = on_date or timezone.localdate()
        self.status = EmployeeStatus.DISMISSED
        self.dismissal_date = moment
        if reason:
            self.exclusion_note = reason[:255]
        self.save(update_fields=["status", "dismissal_date", "exclusion_note", "updated_at"])

        current = self.employment_history.filter(end_date__isnull=True).first()
        if current is not None:
            current.end_date = moment
            current.change_type = EmploymentChange.DISMISSAL
            current.save(update_fields=["end_date", "change_type", "updated_at"])


class EmploymentChange(models.TextChoices):
    """Основание записи кадровой истории."""

    HIRE = "hire", _("Приём на работу")
    PROMOTION = "promotion", _("Повышение")
    TRANSFER = "transfer", _("Перевод")
    ROTATION = "rotation", _("Ротация")
    DEMOTION = "demotion", _("Понижение")
    RENAME = "rename", _("Переименование должности")
    DISMISSAL = "dismissal", _("Увольнение")


#: Основания, которые считаются вертикальным ростом при анализе траектории.
UPWARD_CHANGES = frozenset({EmploymentChange.PROMOTION})


class EmploymentHistoryQuerySet(models.QuerySet):
    """Выборки по кадровой истории."""

    def current(self) -> Self:
        """Незакрытые записи — текущие назначения."""
        return self.filter(end_date__isnull=True)

    def promotions(self) -> Self:
        return self.filter(change_type__in=UPWARD_CHANGES)

    def for_period(self, start: dt.date, end: dt.date) -> Self:
        return self.filter(start_date__lte=end).filter(
            Q(end_date__isnull=True) | Q(end_date__gte=start)
        )


class EmploymentHistory(TimeStampedModel):
    """Запись кадровой истории сотрудника."""

    employee = models.ForeignKey(
        Employee,
        verbose_name=_("сотрудник"),
        on_delete=models.CASCADE,
        related_name="employment_history",
    )
    position = models.ForeignKey(
        "org.Position",
        verbose_name=_("должность"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employment_records",
    )
    position_title = models.CharField(
        _("наименование должности"),
        max_length=255,
        help_text=_("Сохраняется текстом: должность могла быть переименована или упразднена"),
    )
    department = models.ForeignKey(
        "org.Department",
        verbose_name=_("подразделение"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employment_records",
    )
    branch = models.ForeignKey(
        "org.Branch",
        verbose_name=_("филиал"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employment_records",
    )
    grade_level = models.PositiveSmallIntegerField(
        _("уровень грейда"),
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(20)],
        help_text=_("Фиксируется на момент назначения: грейд должности мог измениться"),
    )
    change_type = models.CharField(
        _("основание"),
        max_length=20,
        choices=EmploymentChange.choices,
        default=EmploymentChange.HIRE,
        db_index=True,
    )
    start_date = models.DateField(_("дата начала"), db_index=True)
    end_date = models.DateField(
        _("дата окончания"),
        null=True,
        blank=True,
        help_text=_("Пусто у текущего назначения"),
    )
    subordinates_count = models.PositiveSmallIntegerField(_("число подчинённых"), default=0)
    order_number = models.CharField(
        _("номер приказа"),
        max_length=50,
        blank=True,
        help_text=_("Реквизит кадрового документа, подтверждающего назначение"),
    )
    achievements = models.TextField(
        _("результаты периода"),
        blank=True,
        help_text=_("Достижения на должности: материал для оценки потенциала"),
    )

    objects = EmploymentHistoryQuerySet.as_manager()

    class Meta:
        verbose_name = _("запись кадровой истории")
        verbose_name_plural = _("кадровая история")
        ordering = ["-start_date", "-id"]
        indexes = [
            models.Index(fields=["employee", "-start_date"], name="emphist_employee_idx"),
            models.Index(fields=["change_type"], name="emphist_change_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=models.F("start_date")),
                name="emphist_period_valid",
            ),
            # Незакрытая запись у сотрудника может быть только одна: две
            # «текущие» должности означают ошибку кадрового оформления.
            models.UniqueConstraint(
                fields=["employee"],
                condition=Q(end_date__isnull=True),
                name="emphist_single_current",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.employee.short_name} — {self.position_title}"

    @property
    def is_current(self) -> bool:
        return self.end_date is None

    @property
    def duration_months(self) -> int:
        """Продолжительность пребывания в должности в месяцах."""
        return calculate_tenure_months(self.start_date, self.end_date)
