"""Модель пользователя и связанные с доступом сущности."""

from __future__ import annotations

import datetime as dt
from typing import Self

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import MinLengthValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.constants import PRIVILEGED_ROLES, Role, ScopeLevel
from core.models import TimeStampedModel
from core.utils import full_name, initials, short_name
from core.validators import name_validator, normalize_phone, phone_validator


class UserQuerySet(models.QuerySet):
    """Типовые выборки пользователей."""

    def active(self) -> Self:
        return self.filter(is_active=True)

    def with_role(self, *roles: str) -> Self:
        return self.filter(role__in=roles)

    def privileged(self) -> Self:
        """Учётные записи с доступом к персональным данным всех сотрудников."""
        return self.filter(role__in=PRIVILEGED_ROLES)

    def department_heads(self) -> Self:
        return self.filter(is_department_head=True, is_active=True)

    def in_branch(self, branch_id: int) -> Self:
        return self.filter(branch_id=branch_id)

    def in_department(self, department_id: int) -> Self:
        return self.filter(department_id=department_id)

    def with_org(self) -> Self:
        """Подгружает связи оргструктуры: избавляет списки от лишних запросов."""
        return self.select_related("branch", "department")

    def inactive_since(self, days: int) -> Self:
        """Учётные записи без активности дольше указанного срока."""
        threshold = timezone.now() - dt.timedelta(days=days)
        return self.filter(
            models.Q(last_activity_at__lt=threshold) | models.Q(last_activity_at__isnull=True)
        )


class UserManager(BaseUserManager.from_queryset(UserQuerySet)):
    """Менеджер учётных записей."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra) -> User:
        if not email:
            raise ValueError("Адрес электронной почты обязателен")

        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.full_clean(exclude=["password"])
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra) -> User:
        """Создаёт рядовую учётную запись."""
        extra.setdefault("role", Role.EMPLOYEE)
        extra.setdefault("scope_level", ScopeLevel.SELF)
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email: str, password: str, **extra) -> User:
        """Создаёт учётную запись администратора."""
        extra.update(
            {
                "role": Role.ADMIN,
                "scope_level": ScopeLevel.ORGANIZATION,
                "is_staff": True,
                "is_superuser": True,
                "is_active": True,
            }
        )
        extra.setdefault("last_name", "Администратор")
        extra.setdefault("first_name", "Системный")
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """Учётная запись пользователя системы."""

    # -- Идентификация ------------------------------------------------------
    email = models.EmailField(
        _("электронная почта"),
        max_length=255,
        unique=True,
        help_text=_("Используется как логин и адрес для уведомлений"),
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
    phone = models.CharField(_("телефон"), max_length=20, blank=True, validators=[phone_validator])

    # -- Права --------------------------------------------------------------
    role = models.CharField(
        _("роль"),
        max_length=20,
        choices=Role.choices,
        default=Role.EMPLOYEE,
        db_index=True,
        help_text=_("Определяет набор доступных операций"),
    )
    scope_level = models.CharField(
        _("область видимости"),
        max_length=20,
        choices=ScopeLevel.choices,
        default=ScopeLevel.SELF,
        help_text=_("Определяет, чьи данные доступны пользователю"),
    )

    # -- Место в организации ------------------------------------------------
    branch = models.ForeignKey(
        "org.Branch",
        verbose_name=_("филиал"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
    )
    department = models.ForeignKey(
        "org.Department",
        verbose_name=_("подразделение"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
    )
    is_department_head = models.BooleanField(
        _("руководитель подразделения"),
        default=False,
        help_text=_("Даёт право согласовывать заявки сотрудников подразделения"),
    )

    # -- Состояние ----------------------------------------------------------
    is_active = models.BooleanField(
        _("учётная запись активна"),
        default=True,
        db_index=True,
        help_text=_("Снятие отметки блокирует вход, сохраняя все данные"),
    )
    is_staff = models.BooleanField(
        _("технический доступ"),
        default=False,
        help_text=_("Служебный признак Django; интерфейс системы им не управляется"),
    )
    must_change_password = models.BooleanField(
        _("требуется смена пароля"),
        default=False,
        help_text=_("Пароль выдан администратором и должен быть изменён при входе"),
    )
    two_factor_enabled = models.BooleanField(
        _("двухфакторная аутентификация"),
        default=False,
        help_text=_("Обязательна для ролей администратора и HR-специалиста"),
    )
    last_activity_at = models.DateTimeField(
        _("последняя активность"), null=True, blank=True, db_index=True
    )
    password_changed_at = models.DateTimeField(_("пароль изменён"), null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["last_name", "first_name"]
    EMAIL_FIELD = "email"

    class Meta:
        verbose_name = _("пользователь")
        verbose_name_plural = _("пользователи")
        ordering = ["last_name", "first_name"]
        indexes = [
            models.Index(fields=["role", "is_active"], name="user_role_active_idx"),
            models.Index(fields=["branch", "department"], name="user_org_idx"),
            models.Index(fields=["last_name", "first_name"], name="user_name_idx"),
        ]
        constraints = [
            # Руководителем подразделения может быть только пользователь,
            # у которого подразделение указано: признак без привязки
            # не поддаётся интерпретации при согласовании заявок.
            models.CheckConstraint(
                condition=(
                    models.Q(is_department_head=False)
                    | models.Q(is_department_head=True, department__isnull=False)
                ),
                name="user_head_requires_department",
            ),
        ]

    # -- Представление ------------------------------------------------------
    def __str__(self) -> str:
        return f"{self.short_name} ({self.email})"

    def clean(self) -> None:
        """Нормализует данные перед сохранением."""
        super().clean()
        self.email = self.email.lower().strip()
        if self.phone:
            self.phone = normalize_phone(self.phone)

    def save(self, *args, **kwargs) -> None:
        # Привилегированные роли обязаны использовать второй фактор: это
        # проще выставить здесь, чем полагаться на дисциплину администратора.
        from django.conf import settings

        if self.role in getattr(settings, "OTP_REQUIRED_ROLES", []):
            self.two_factor_enabled = True
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("administration:user_detail", kwargs={"pk": self.pk})

    # -- Имена --------------------------------------------------------------
    @property
    def full_name(self) -> str:
        """Полное ФИО: «Иванова Мария Сергеевна»."""
        return full_name(self.last_name, self.first_name, self.middle_name)

    @property
    def short_name(self) -> str:
        """Сокращённая запись: «Иванова М. С.»."""
        return short_name(self.last_name, self.first_name, self.middle_name)

    @property
    def initials(self) -> str:
        """Инициалы для аватара-заглушки."""
        return initials(self.first_name, self.middle_name)

    def get_full_name(self) -> str:
        """Совместимость с интерфейсом Django."""
        return self.full_name

    def get_short_name(self) -> str:
        """Совместимость с интерфейсом Django."""
        return self.first_name

    # -- Роли ---------------------------------------------------------------
    @property
    def role_label(self) -> str:
        """Человекочитаемое наименование роли."""
        return str(Role(self.role).label)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def is_hr(self) -> bool:
        return self.role == Role.HR

    @property
    def is_manager(self) -> bool:
        return self.role == Role.MANAGER

    @property
    def is_mentor(self) -> bool:
        return self.role == Role.MENTOR

    @property
    def is_privileged(self) -> bool:
        """Доступ к персональным данным всех сотрудников."""
        return self.role in PRIVILEGED_ROLES

    def has_role(self, *roles: str) -> bool:
        return self.role in roles

    # -- Настройки пользователя --------------------------------------------
    @property
    def timezone(self) -> str:
        """Часовой пояс пользователя."""
        profile = getattr(self, "_cached_profile", None)
        if profile is None:
            profile = getattr(self, "profile", None)
            self._cached_profile = profile
        return profile.timezone if profile else ""

    @property
    def employee(self):
        """Карточка сотрудника или ``None``, если она не заведена."""
        return getattr(self, "employee_card", None)

    def touch_activity(self) -> None:
        """Фиксирует активность пользователя."""
        now = timezone.now()
        User.objects.filter(pk=self.pk).update(last_activity_at=now)
        self.last_activity_at = now

    def set_password(self, raw_password: str | None) -> None:
        super().set_password(raw_password)
        self.password_changed_at = timezone.now()
        self.must_change_password = False


class UserProfile(TimeStampedModel):
    """Пользовательские настройки интерфейса."""

    class Theme(models.TextChoices):
        SYSTEM = "system", _("Как в системе")
        LIGHT = "light", _("Светлая")
        DARK = "dark", _("Тёмная")

    class Density(models.TextChoices):
        COMFORTABLE = "comfortable", _("Просторная")
        COMPACT = "compact", _("Плотная")

    user = models.OneToOneField(
        "accounts.User",
        verbose_name=_("пользователь"),
        on_delete=models.CASCADE,
        related_name="profile",
    )
    photo = models.ImageField(_("фотография"), upload_to="avatars/%Y/%m/", null=True, blank=True)
    position_title = models.CharField(
        _("должность в подписи"),
        max_length=255,
        blank=True,
        help_text=_("Выводится в печатных формах и письмах"),
    )
    timezone = models.CharField(
        _("часовой пояс"),
        max_length=64,
        blank=True,
        help_text=_("Пустое значение означает часовой пояс организации"),
    )
    language = models.CharField(
        _("язык интерфейса"),
        max_length=8,
        choices=[("ru", _("Русский")), ("en", _("English"))],
        default="ru",
    )
    theme = models.CharField(
        _("оформление"), max_length=10, choices=Theme.choices, default=Theme.SYSTEM
    )
    density = models.CharField(
        _("плотность таблиц"),
        max_length=12,
        choices=Density.choices,
        default=Density.COMFORTABLE,
    )
    email_notifications = models.BooleanField(
        _("уведомления по почте"),
        default=True,
        help_text=_("Приближение сроков, изменение статуса, назначение задач"),
    )
    digest_notifications = models.BooleanField(
        _("еженедельная сводка"),
        default=True,
        help_text=_("Итоги недели по резерву и планам развития"),
    )

    class Meta:
        verbose_name = _("профиль пользователя")
        verbose_name_plural = _("профили пользователей")

    def __str__(self) -> str:
        return str(_("Профиль: %(name)s") % {"name": self.user.short_name})


class AccessScope(TimeStampedModel):
    """Дополнительная область видимости."""

    user = models.ForeignKey(
        "accounts.User",
        verbose_name=_("пользователь"),
        on_delete=models.CASCADE,
        related_name="access_scopes",
    )
    branch = models.ForeignKey(
        "org.Branch",
        verbose_name=_("филиал"),
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="granted_scopes",
    )
    department = models.ForeignKey(
        "org.Department",
        verbose_name=_("подразделение"),
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="granted_scopes",
    )
    granted_by = models.ForeignKey(
        "accounts.User",
        verbose_name=_("кем предоставлено"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_access_scopes",
    )
    valid_until = models.DateField(
        _("действует до"),
        null=True,
        blank=True,
        help_text=_("Пустое значение означает бессрочный доступ"),
    )
    comment = models.CharField(_("основание"), max_length=255, blank=True)

    class Meta:
        verbose_name = _("расширение доступа")
        verbose_name_plural = _("расширения доступа")
        ordering = ["user", "branch", "department"]
        constraints = [
            # Область должна указывать хотя бы на один объект оргструктуры,
            # иначе запись не несёт смысла и лишь замедляет проверку прав.
            models.CheckConstraint(
                condition=models.Q(branch__isnull=False) | models.Q(department__isnull=False),
                name="access_scope_requires_target",
            ),
            models.UniqueConstraint(
                fields=["user", "branch", "department"],
                name="access_scope_unique_target",
            ),
        ]

    def __str__(self) -> str:
        target = self.department or self.branch
        return f"{self.user.short_name} → {target}"

    @property
    def is_expired(self) -> bool:
        return bool(self.valid_until and self.valid_until < timezone.localdate())


class LoginRecord(TimeStampedModel):
    """История входов в систему."""

    class Outcome(models.TextChoices):
        SUCCESS = "success", _("Успешный вход")
        FAILED = "failed", _("Неверный пароль")
        BLOCKED = "blocked", _("Заблокировано")
        LOGOUT = "logout", _("Выход")

    user = models.ForeignKey(
        "accounts.User",
        verbose_name=_("пользователь"),
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="login_records",
    )
    email_attempted = models.CharField(
        _("введённый адрес"),
        max_length=255,
        blank=True,
        help_text=_("Сохраняется при неудачной попытке, когда пользователь не определён"),
    )
    outcome = models.CharField(
        _("результат"), max_length=10, choices=Outcome.choices, db_index=True
    )
    ip_address = models.GenericIPAddressField(_("IP-адрес"), null=True, blank=True)
    user_agent = models.CharField(_("клиент"), max_length=400, blank=True)
    session_key = models.CharField(_("ключ сессии"), max_length=40, blank=True, db_index=True)

    class Meta:
        verbose_name = _("запись о входе")
        verbose_name_plural = _("история входов")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="login_user_time_idx"),
            models.Index(fields=["outcome", "-created_at"], name="login_outcome_time_idx"),
        ]

    def __str__(self) -> str:
        who = self.user.short_name if self.user else self.email_attempted
        return f"{who}: {self.get_outcome_display()} ({self.created_at:%d.%m.%Y %H:%M})"
