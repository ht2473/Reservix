"""Фабрики тестовых объектов."""

from __future__ import annotations

import factory
from factory.django import DjangoModelFactory

from django.contrib.auth import get_user_model
from django.db.models import Max

from apps.audit.models import AuditAction, AuditLog
from apps.notifications.models import Notification, NotificationKind
from apps.org.models import Branch, Department, Position, PositionGrade, PositionProfile
from apps.portal.models import Article, ArticleCategory, Feedback, GlossaryTerm

User = get_user_model()

#: Правдоподобные компоненты ФИО. Собственный набор вместо генератора Faker:
#: нужны отчества, которых в стандартных наборах нет, и согласованность рода.
LAST_NAMES = [
    "Смирнов",
    "Кузнецов",
    "Волков",
    "Зайцев",
    "Лебедев",
    "Яковлев",
    "Попов",
    "Морозов",
    "Новиков",
    "Козлов",
    "Петров",
    "Фёдоров",
]
FIRST_NAMES = ["Игорь", "Андрей", "Артём", "Кирилл", "Максим", "Дмитрий", "Алексей", "Сергей"]
MIDDLE_NAMES = [
    "Владимирович",
    "Дмитриевич",
    "Александрович",
    "Сергеевич",
    "Юрьевич",
    "Игоревич",
    "Олегович",
    "Петрович",
]


class BranchFactory(DjangoModelFactory):
    """Филиал организации."""

    class Meta:
        model = Branch
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"branch-{n}")
    name = factory.Sequence(lambda n: f"Филиал №{n}")
    city = factory.Iterator(["Москва", "Санкт-Петербург", "Новосибирск", "Екатеринбург"])
    address = factory.Sequence(lambda n: f"ул. Примерная, д. {n + 1}")
    timezone = "Europe/Moscow"
    is_active = True


class DepartmentFactory(DjangoModelFactory):
    """Подразделение. Филиал создаётся автоматически, если не передан."""

    class Meta:
        model = Department
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"dept-{n}")
    name = factory.Sequence(lambda n: f"Подразделение №{n}")
    branch = factory.SubFactory(BranchFactory)
    parent = None
    is_active = True


class PositionGradeFactory(DjangoModelFactory):
    """Грейд должности."""

    class Meta:
        model = PositionGrade
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"grade-{n}")
    name = factory.Sequence(lambda n: f"Грейд {n}")
    # Уровень уникален по ограничению модели.
    level = factory.LazyFunction(
        lambda: (PositionGrade.objects.aggregate(top=Max("level"))["top"] or 0) + 1
    )
    is_managerial = False


class PositionFactory(DjangoModelFactory):
    """Должность штатного расписания."""

    class Meta:
        model = Position
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"position-{n}")
    name = factory.Sequence(lambda n: f"Должность №{n}")
    grade = factory.SubFactory(PositionGradeFactory)
    department = factory.SubFactory(DepartmentFactory)
    headcount_plan = 1
    is_key_position = False
    is_active = True


class PositionProfileFactory(DjangoModelFactory):
    """Профиль требований к должности."""

    class Meta:
        model = PositionProfile

    position = factory.SubFactory(PositionFactory)
    purpose = "Обеспечение результата подразделения по закреплённому направлению"
    responsibilities = "Планирование работ, распределение задач, контроль качества"
    min_education = PositionProfile.Education.BACHELOR
    min_total_experience_years = 3
    min_relevant_experience_years = 2
    min_company_tenure_months = 12
    requires_management_experience = False
    is_published = True


class UserFactory(DjangoModelFactory):
    """Учётная запись пользователя."""

    class Meta:
        model = User
        django_get_or_create = ("email",)
        skip_postgeneration_save = True

    last_name = factory.Iterator(LAST_NAMES)
    first_name = factory.Iterator(FIRST_NAMES)
    middle_name = factory.Iterator(MIDDLE_NAMES)
    email = factory.Sequence(lambda n: f"user{n}@meridian-soft.ru")
    phone = factory.Sequence(lambda n: f"+7900{n:07d}")
    role = "employee"
    scope_level = "self"
    is_active = True
    is_staff = False
    is_superuser = False
    branch = factory.SubFactory(BranchFactory)
    department = factory.SubFactory(DepartmentFactory)
    password = factory.PostGenerationMethodCall("set_password", "Nadezhnyi-Parol-2026")

    @factory.post_generation
    def save_password(obj, create, extracted, **kwargs):  # noqa: N805
        """Сохраняет хеш пароля, установленный на предыдущем шаге."""
        if create:
            obj.save(update_fields=["password"])


class ArticleCategoryFactory(DjangoModelFactory):
    """Раздел базы знаний."""

    class Meta:
        model = ArticleCategory
        django_get_or_create = ("slug",)

    name = factory.Sequence(lambda n: f"Раздел {n}")
    slug = factory.Sequence(lambda n: f"category-{n}")
    description = "Материалы по управлению кадровым резервом"


class ArticleFactory(DjangoModelFactory):
    """Материал базы знаний."""

    class Meta:
        model = Article

    title = factory.Sequence(lambda n: f"Материал о кадровом резерве №{n}")
    category = factory.SubFactory(ArticleCategoryFactory)
    summary = "Краткое изложение подхода к формированию кадрового резерва"
    body = (
        "## Введение\n\n"
        "Кадровый резерв — это подготовленные сотрудники организации.\n\n"
        "* Первый тезис\n"
        "* Второй тезис\n"
    )
    is_published = True


class GlossaryTermFactory(DjangoModelFactory):
    """Термин глоссария."""

    class Meta:
        model = GlossaryTerm
        django_get_or_create = ("term",)

    term = factory.Sequence(lambda n: f"Термин {n}")
    english = factory.Sequence(lambda n: f"Term {n}")
    definition = "Определение термина в контексте управления кадровым резервом"
    is_published = True


class FeedbackFactory(DjangoModelFactory):
    """Обращение обратной связи."""

    class Meta:
        model = Feedback

    name = "Иванов Иван Иванович"
    email = "ivanov@meridian-soft.ru"
    topic = Feedback.Topic.QUESTION
    subject = factory.Sequence(lambda n: f"Вопрос по работе системы №{n}")
    message = "Не удаётся найти раздел с индивидуальным планом развития сотрудника."
    consent_given = True
    status = Feedback.Status.NEW


class NotificationFactory(DjangoModelFactory):
    """Уведомление пользователя."""

    class Meta:
        model = Notification

    recipient = factory.SubFactory(UserFactory)
    kind = NotificationKind.SYSTEM
    title = factory.Sequence(lambda n: f"Системное уведомление №{n}")
    body = "Текст уведомления"
    should_email = False


class AuditLogFactory(DjangoModelFactory):
    """Запись журнала аудита."""

    class Meta:
        model = AuditLog

    actor = factory.SubFactory(UserFactory)
    actor_repr = factory.LazyAttribute(lambda obj: obj.actor.short_name)
    action = AuditAction.UPDATE
    message = "Изменение тестовой записи"


__all__ = [
    "ArticleCategoryFactory",
    "ArticleFactory",
    "AuditLogFactory",
    "BranchFactory",
    "DepartmentFactory",
    "FeedbackFactory",
    "GlossaryTermFactory",
    "NotificationFactory",
    "PositionFactory",
    "PositionGradeFactory",
    "PositionProfileFactory",
    "UserFactory",
]
