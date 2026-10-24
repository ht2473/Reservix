"""Организационная структура."""

from __future__ import annotations

from typing import Self

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from core.models import (
    ActivatableModel,
    ActiveQuerySet,
    DescribedModel,
    OrderedModel,
    TimeStampedModel,
)
from core.utils import section_url
from core.validators import code_validator

#: Разделитель уровней в материализованном пути подразделения.
PATH_SEPARATOR = "."
#: Ширина одного уровня пути. Четыре знака допускают 9999 подразделений
#: на уровне — с запасом для организации любого размера.
PATH_STEP_WIDTH = 4


class Branch(DescribedModel, ActivatableModel, TimeStampedModel):
    """Филиал организации."""

    code = models.SlugField(
        _("код"),
        max_length=32,
        unique=True,
        validators=[code_validator],
        help_text=_("Используется в выгрузках и печатных формах"),
    )
    city = models.CharField(_("город"), max_length=100, db_index=True)
    address = models.TextField(_("адрес"), blank=True)
    timezone = models.CharField(
        _("часовой пояс"),
        max_length=64,
        default="Europe/Moscow",
        help_text=_("Например, Asia/Novosibirsk"),
    )
    is_head_office = models.BooleanField(
        _("головной офис"),
        default=False,
        help_text=_("В организации может быть только один головной офис"),
    )
    phone = models.CharField(_("телефон"), max_length=20, blank=True)
    email = models.EmailField(_("электронная почта"), blank=True)

    class Meta:
        verbose_name = _("филиал")
        verbose_name_plural = _("филиалы")
        ordering = ["-is_head_office", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_head_office"],
                condition=models.Q(is_head_office=True),
                name="branch_single_head_office",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("administration:branch_detail", kwargs={"pk": self.pk})


class DepartmentQuerySet(ActiveQuerySet):
    """Выборки по иерархии подразделений."""

    def roots(self) -> Self:
        """Подразделения верхнего уровня."""
        return self.filter(parent__isnull=True)

    def in_branch(self, branch_id: int) -> Self:
        return self.filter(branch_id=branch_id)

    def descendants_of(self, department: Department) -> Self:
        """Все вложенные подразделения, включая само подразделение."""
        return self.filter(path__startswith=department.path)

    def ancestors_of(self, department: Department) -> Self:
        """Цепочка родительских подразделений от корня до указанного."""
        codes = department.ancestor_paths()
        return self.filter(path__in=codes).order_by("path")


class DepartmentManager(models.Manager.from_queryset(DepartmentQuerySet)):
    """Менеджер подразделений с операциями над иерархией."""

    def descendant_ids(self, department_id: int) -> list[int]:
        """Идентификаторы подразделения и всех вложенных в него."""
        from django.core.cache import cache

        cache_key = f"dept:descendants:{department_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        department = self.filter(pk=department_id).only("path").first()
        if department is None:
            return []

        ids = list(self.filter(path__startswith=department.path).values_list("id", flat=True))
        cache.set(cache_key, ids, timeout=600)
        return ids


class Department(DescribedModel, ActivatableModel, TimeStampedModel):
    """Подразделение организации."""

    code = models.SlugField(_("код"), max_length=32, unique=True, validators=[code_validator])
    branch = models.ForeignKey(
        Branch,
        verbose_name=_("филиал"),
        on_delete=models.CASCADE,
        related_name="departments",
    )
    parent = models.ForeignKey(
        "self",
        verbose_name=_("вышестоящее подразделение"),
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
    )
    path = models.CharField(
        _("путь в иерархии"),
        max_length=255,
        db_index=True,
        editable=False,
        help_text=_("Служебное поле: положение узла в дереве подразделений"),
    )
    depth = models.PositiveSmallIntegerField(_("уровень вложенности"), default=0, editable=False)
    head = models.ForeignKey(
        "accounts.User",
        verbose_name=_("руководитель"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="headed_departments",
    )

    objects = DepartmentManager()

    class Meta:
        verbose_name = _("подразделение")
        verbose_name_plural = _("подразделения")
        ordering = ["path"]
        indexes = [
            models.Index(fields=["branch", "path"], name="dept_branch_path_idx"),
            models.Index(fields=["parent"], name="dept_parent_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("administration:department_detail", kwargs={"pk": self.pk})

    # -- Иерархия -----------------------------------------------------------
    def save(self, *args, **kwargs) -> None:
        """Пересчитывает путь узла и, при необходимости, всего поддерева."""
        is_new = self.pk is None
        old_path = None

        if not is_new:
            old_path = Department.objects.filter(pk=self.pk).values_list("path", flat=True).first()

        self._assign_path()
        super().save(*args, **kwargs)

        if is_new:
            # Ключ получен — путь достраивается окончательно.
            self._assign_path()
            Department.objects.filter(pk=self.pk).update(path=self.path, depth=self.depth)
        elif old_path and old_path != self.path:
            self._rebuild_subtree(old_path)

        self._invalidate_cache()

    def _assign_path(self) -> None:
        """Формирует материализованный путь на основании родителя."""
        if self.parent_id is None:
            prefix = ""
            self.depth = 0
        else:
            parent = self.parent or Department.objects.get(pk=self.parent_id)
            prefix = parent.path + PATH_SEPARATOR
            self.depth = parent.depth + 1

        node = str(self.pk).zfill(PATH_STEP_WIDTH) if self.pk else "0" * PATH_STEP_WIDTH
        self.path = prefix + node

    def _rebuild_subtree(self, old_path: str) -> None:
        """Обновляет пути всех вложенных подразделений после переноса."""
        children = Department.objects.filter(path__startswith=old_path + PATH_SEPARATOR)
        for child in children:
            child.path = self.path + child.path[len(old_path) :]
            child.depth = child.path.count(PATH_SEPARATOR)
            Department.objects.filter(pk=child.pk).update(path=child.path, depth=child.depth)

    def _invalidate_cache(self) -> None:
        """Сбрасывает кэш иерархии: структура изменилась."""
        from django.core.cache import cache

        cache.delete(f"dept:descendants:{self.pk}")
        for ancestor_path in self.ancestor_paths():
            ancestor_id = int(ancestor_path.split(PATH_SEPARATOR)[-1])
            cache.delete(f"dept:descendants:{ancestor_id}")

    def ancestor_paths(self) -> list[str]:
        """Пути всех родительских узлов, от корня до непосредственного родителя."""
        parts = self.path.split(PATH_SEPARATOR)
        return [PATH_SEPARATOR.join(parts[: i + 1]) for i in range(len(parts) - 1)]

    @property
    def full_path_name(self) -> str:
        """Полное наименование с указанием вышестоящих подразделений."""
        chain = list(Department.objects.ancestors_of(self).values_list("name", flat=True))
        return " / ".join([*chain, self.name])

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    @property
    def has_children(self) -> bool:
        return self.children.exists()


class PositionGrade(OrderedModel, ActivatableModel, TimeStampedModel):
    """Грейд должности."""

    code = models.SlugField(_("код"), max_length=32, unique=True, validators=[code_validator])
    name = models.CharField(_("наименование"), max_length=100)
    level = models.PositiveSmallIntegerField(
        _("уровень"),
        unique=True,
        validators=[MinValueValidator(1), MaxValueValidator(20)],
        help_text=_("Большее значение соответствует более высокому положению"),
    )
    description = models.TextField(_("описание"), blank=True)
    is_managerial = models.BooleanField(
        _("управленческий грейд"),
        default=False,
        help_text=_("Должности этого грейда предполагают руководство людьми"),
    )

    class Meta:
        verbose_name = _("грейд")
        verbose_name_plural = _("грейды")
        ordering = ["level"]

    def __str__(self) -> str:
        return str(_("%(name)s (уровень %(level)s)") % {"name": self.name, "level": self.level})


class PositionQuerySet(ActiveQuerySet):
    """Выборки должностей."""

    def key_positions(self) -> Self:
        """Должности, отнесённые к критичным для непрерывности деятельности."""
        return self.filter(is_key_position=True, is_active=True)

    def managerial(self) -> Self:
        return self.filter(grade__is_managerial=True)

    def with_profile(self) -> Self:
        return self.select_related("grade", "department", "profile")


class Position(DescribedModel, ActivatableModel, TimeStampedModel):
    """Должность в штатном расписании."""

    code = models.SlugField(_("код"), max_length=48, unique=True, validators=[code_validator])
    grade = models.ForeignKey(
        PositionGrade,
        verbose_name=_("грейд"),
        on_delete=models.PROTECT,
        related_name="positions",
    )
    department = models.ForeignKey(
        Department,
        verbose_name=_("подразделение"),
        on_delete=models.CASCADE,
        related_name="positions",
        null=True,
        blank=True,
        help_text=_("Пусто для типовых должностей, встречающихся в разных подразделениях"),
    )
    headcount_plan = models.PositiveSmallIntegerField(
        _("штатных единиц"),
        default=1,
        help_text=_("Плановая численность по штатному расписанию"),
    )
    is_key_position = models.BooleanField(
        _("критичная должность"),
        default=False,
        db_index=True,
        help_text=_("Должность, уход с которой создаёт риск для деятельности организации"),
    )
    reports_to = models.ForeignKey(
        "self",
        verbose_name=_("подчиняется должности"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subordinate_positions",
    )

    objects = PositionQuerySet.as_manager()

    class Meta:
        verbose_name = _("должность")
        verbose_name_plural = _("должности")
        ordering = ["-grade__level", "name"]
        indexes = [
            models.Index(fields=["is_key_position", "is_active"], name="position_key_active_idx"),
            models.Index(fields=["grade", "department"], name="position_grade_dept_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("administration:position_detail", kwargs={"pk": self.pk})

    def is_promotion_from(self, other: Position) -> bool:
        """Определяет, является ли переход с указанной должности повышением."""
        return self.grade.level > other.grade.level


class PositionProfile(TimeStampedModel):
    """Профиль должности — формализованные требования к кандидату."""

    class Education(models.TextChoices):
        SECONDARY = "secondary", _("Среднее")
        VOCATIONAL = "vocational", _("Среднее профессиональное")
        BACHELOR = "bachelor", _("Бакалавриат")
        SPECIALIST = "specialist", _("Специалитет")
        MASTER = "master", _("Магистратура")
        POSTGRADUATE = "postgraduate", _("Аспирантура и выше")

    position = models.OneToOneField(
        Position,
        verbose_name=_("должность"),
        on_delete=models.CASCADE,
        related_name="profile",
    )
    purpose = models.TextField(
        _("назначение должности"),
        blank=True,
        help_text=_("Зачем должность существует и какой результат обеспечивает"),
    )
    responsibilities = models.TextField(
        _("основные обязанности"),
        blank=True,
        help_text=_("Перечень ключевых зон ответственности"),
    )
    min_education = models.CharField(
        _("минимальное образование"),
        max_length=20,
        choices=Education.choices,
        default=Education.BACHELOR,
    )
    min_total_experience_years = models.PositiveSmallIntegerField(_("общий стаж, лет"), default=0)
    min_relevant_experience_years = models.PositiveSmallIntegerField(
        _("профильный стаж, лет"), default=0
    )
    min_company_tenure_months = models.PositiveSmallIntegerField(
        _("стаж в организации, месяцев"),
        default=0,
        help_text=_("Требование к внутренним кандидатам на замещение"),
    )
    requires_management_experience = models.BooleanField(
        _("требуется управленческий опыт"), default=False
    )
    min_subordinates = models.PositiveSmallIntegerField(
        _("минимальное число подчинённых в опыте"), default=0
    )
    additional_requirements = models.TextField(_("дополнительные требования"), blank=True)
    is_published = models.BooleanField(
        _("опубликован"),
        default=False,
        help_text=_("Опубликованные профили видны сотрудникам в разделе карьерных возможностей"),
    )

    class Meta:
        verbose_name = _("профиль должности")
        verbose_name_plural = _("профили должностей")
        ordering = ["position__name"]

    def __str__(self) -> str:
        return str(_("Профиль: %(name)s") % {"name": self.position.name})

    def get_absolute_url(self) -> str:
        return section_url("org:profile_detail", pk=self.pk)

    @property
    def education_rank(self) -> int:
        """Числовой ранг требуемого образования."""
        order: dict[str, int] = {
            self.Education.SECONDARY: 1,
            self.Education.VOCATIONAL: 2,
            self.Education.BACHELOR: 3,
            self.Education.SPECIALIST: 4,
            self.Education.MASTER: 5,
            self.Education.POSTGRADUATE: 6,
        }
        return order.get(self.min_education, 0)
