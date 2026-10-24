"""Содержимое публичной части ресурса."""

from __future__ import annotations

from typing import Self

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import ActivatableModel, OrderedModel, TimeStampedModel
from core.utils import slugify_ru


class PublishedQuerySet(models.QuerySet):
    """Выборки публикуемого содержимого."""

    def published(self) -> Self:
        """Материалы, опубликованные и доступные посетителю."""
        return self.filter(is_published=True, published_at__lte=timezone.now())

    def drafts(self) -> Self:
        return self.filter(is_published=False)


class ArticleCategory(OrderedModel, ActivatableModel, TimeStampedModel):
    """Раздел базы знаний."""

    name = models.CharField(_("наименование"), max_length=120)
    slug = models.SlugField(_("адрес"), max_length=120, unique=True)
    description = models.CharField(_("описание"), max_length=300, blank=True)
    icon = models.CharField(
        _("значок"),
        max_length=40,
        default="book",
        help_text=_("Идентификатор значка из набора интерфейса"),
    )

    class Meta:
        verbose_name = _("раздел базы знаний")
        verbose_name_plural = _("разделы базы знаний")
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("portal:articles") + f"?category={self.slug}"


class Article(TimeStampedModel):
    """Материал базы знаний."""

    title = models.CharField(_("заголовок"), max_length=250)
    slug = models.SlugField(_("адрес"), max_length=250, unique=True, blank=True)
    category = models.ForeignKey(
        ArticleCategory,
        verbose_name=_("раздел"),
        on_delete=models.PROTECT,
        related_name="articles",
    )
    summary = models.CharField(
        _("краткое описание"),
        max_length=400,
        help_text=_("Выводится в списке материалов и в описании страницы"),
    )
    body = models.TextField(
        _("текст"),
        help_text=_("Разметка: ## заголовок, * список, > цитата, пустая строка — абзац"),
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("автор"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="articles",
    )
    tags = models.CharField(
        _("метки"),
        max_length=300,
        blank=True,
        help_text=_("Через запятую: резерв, оценка, преемственность"),
    )
    reading_minutes = models.PositiveSmallIntegerField(
        _("время чтения, мин"), default=0, editable=False
    )
    view_count = models.PositiveIntegerField(_("просмотров"), default=0, editable=False)
    is_published = models.BooleanField(_("опубликован"), default=False, db_index=True)
    published_at = models.DateTimeField(_("дата публикации"), null=True, blank=True, db_index=True)

    objects = PublishedQuerySet.as_manager()

    class Meta:
        verbose_name = _("материал базы знаний")
        verbose_name_plural = _("материалы базы знаний")
        ordering = ["-published_at", "-created_at"]
        indexes = [
            models.Index(fields=["is_published", "-published_at"], name="article_pub_idx"),
            models.Index(fields=["category", "-published_at"], name="article_cat_pub_idx"),
        ]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs) -> None:
        if not self.slug:
            self.slug = self._unique_slug()
        # Скорость чтения 180 слов в минуту — усреднённый показатель для
        # текстов профессиональной тематики на русском языке.
        self.reading_minutes = max(1, round(len(self.body.split()) / 180))
        if self.is_published and self.published_at is None:
            self.published_at = timezone.now()
        super().save(*args, **kwargs)

    def _unique_slug(self) -> str:
        """Формирует адрес, добавляя суффикс при совпадении."""
        base = slugify_ru(self.title) or "article"
        candidate = base
        counter = 2
        while Article.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate

    def get_absolute_url(self) -> str:
        return reverse("portal:article_detail", kwargs={"slug": self.slug})

    @property
    def tag_list(self) -> list[str]:
        return [tag.strip() for tag in self.tags.split(",") if tag.strip()]

    def register_view(self) -> None:
        """Увеличивает счётчик просмотров."""
        Article.objects.filter(pk=self.pk).update(view_count=models.F("view_count") + 1)


class GlossaryTerm(TimeStampedModel):
    """Термин глоссария."""

    term = models.CharField(_("термин"), max_length=150, unique=True)
    slug = models.SlugField(_("адрес"), max_length=150, unique=True, blank=True)
    english = models.CharField(
        _("английский эквивалент"),
        max_length=150,
        blank=True,
        help_text=_("Принятый в профессиональной литературе термин"),
    )
    definition = models.TextField(_("определение"))
    example = models.TextField(
        _("пример употребления"), blank=True, help_text=_("Как термин используется на практике")
    )
    related = models.ManyToManyField(
        "self",
        verbose_name=_("смежные термины"),
        blank=True,
        symmetrical=True,
    )
    is_published = models.BooleanField(_("опубликован"), default=True, db_index=True)

    class Meta:
        verbose_name = _("термин глоссария")
        verbose_name_plural = _("глоссарий")
        ordering = ["term"]
        indexes = [models.Index(fields=["term"], name="glossary_term_idx")]

    def __str__(self) -> str:
        return self.term

    def save(self, *args, **kwargs) -> None:
        if not self.slug:
            self.slug = slugify_ru(self.term)
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("portal:glossary") + f"#{self.slug}"

    @property
    def letter(self) -> str:
        """Первая буква термина — используется для алфавитного указателя."""
        return self.term[0].upper() if self.term else "#"


class FaqCategory(OrderedModel, ActivatableModel, TimeStampedModel):
    """Раздел справки."""

    name = models.CharField(_("наименование"), max_length=120)
    slug = models.SlugField(_("адрес"), max_length=120, unique=True)
    audience = models.CharField(
        _("аудитория"),
        max_length=100,
        blank=True,
        help_text=_("Например: сотрудникам, руководителям, HR-специалистам"),
    )

    class Meta:
        verbose_name = _("раздел справки")
        verbose_name_plural = _("разделы справки")
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name


class FaqItem(OrderedModel, TimeStampedModel):
    """Вопрос и ответ в разделе справки."""

    category = models.ForeignKey(
        FaqCategory,
        verbose_name=_("раздел"),
        on_delete=models.CASCADE,
        related_name="items",
    )
    question = models.CharField(_("вопрос"), max_length=300)
    answer = models.TextField(_("ответ"))
    is_published = models.BooleanField(_("опубликован"), default=True, db_index=True)
    helpful_count = models.PositiveIntegerField(_("отмечен полезным"), default=0, editable=False)

    class Meta:
        verbose_name = _("вопрос справки")
        verbose_name_plural = _("вопросы справки")
        ordering = ["category__sort_order", "sort_order"]

    def __str__(self) -> str:
        return self.question


class Page(TimeStampedModel):
    """Статическая страница портала."""

    slug = models.SlugField(_("адрес"), max_length=120, unique=True)
    title = models.CharField(_("заголовок"), max_length=250)
    subtitle = models.CharField(_("подзаголовок"), max_length=300, blank=True)
    body = models.TextField(_("содержимое"))
    meta_description = models.CharField(
        _("описание для поисковых систем"), max_length=300, blank=True
    )
    is_published = models.BooleanField(_("опубликована"), default=True, db_index=True)
    show_in_footer = models.BooleanField(_("ссылка в подвале"), default=False)

    class Meta:
        verbose_name = _("страница портала")
        verbose_name_plural = _("страницы портала")
        ordering = ["title"]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("portal:page", kwargs={"slug": self.slug})


class Feedback(TimeStampedModel):
    """Обращение через форму обратной связи."""

    class Topic(models.TextChoices):
        QUESTION = "question", _("Вопрос по работе системы")
        RESERVE = "reserve", _("Вопрос о кадровом резерве")
        ACCESS = "access", _("Проблема с доступом")
        ERROR = "error", _("Сообщение об ошибке")
        SUGGESTION = "suggestion", _("Предложение по развитию")
        OTHER = "other", _("Другое")

    class Status(models.TextChoices):
        NEW = "new", _("Новое")
        IN_PROGRESS = "in_progress", _("В работе")
        ANSWERED = "answered", _("Отвечено")
        CLOSED = "closed", _("Закрыто")
        SPAM = "spam", _("Спам")

    name = models.CharField(_("имя"), max_length=150)
    email = models.EmailField(_("электронная почта"), max_length=255)
    phone = models.CharField(_("телефон"), max_length=20, blank=True)
    organization = models.CharField(_("организация"), max_length=200, blank=True)
    topic = models.CharField(
        _("тема"), max_length=20, choices=Topic.choices, default=Topic.QUESTION
    )
    subject = models.CharField(_("заголовок"), max_length=250)
    message = models.TextField(_("сообщение"))
    consent_given = models.BooleanField(
        _("согласие на обработку персональных данных"),
        default=False,
        help_text=_("Обязательно в соответствии с требованиями законодательства"),
    )

    status = models.CharField(
        _("статус"),
        max_length=15,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("ответственный"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_feedback",
    )
    answer = models.TextField(_("ответ"), blank=True)
    answered_at = models.DateTimeField(_("дата ответа"), null=True, blank=True)
    answered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("ответил"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="answered_feedback",
    )
    internal_note = models.TextField(
        _("служебная заметка"), blank=True, help_text=_("Не отправляется заявителю")
    )

    ip_address = models.GenericIPAddressField(_("IP-адрес"), null=True, blank=True)
    user_agent = models.CharField(_("клиент"), max_length=400, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("авторизованный пользователь"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_feedback",
        help_text=_("Заполняется, если обращение отправлено из личного кабинета"),
    )

    class Meta:
        verbose_name = _("обращение")
        verbose_name_plural = _("обращения обратной связи")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="feedback_status_time_idx"),
            models.Index(fields=["topic", "-created_at"], name="feedback_topic_time_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.subject} — {self.name}"

    def get_absolute_url(self) -> str:
        return reverse("administration:feedback_detail", kwargs={"pk": self.pk})

    @property
    def is_open(self) -> bool:
        return self.status in {self.Status.NEW, self.Status.IN_PROGRESS}

    @property
    def response_time_hours(self) -> float | None:
        """Время реакции на обращение — показатель качества поддержки."""
        if self.answered_at is None:
            return None
        return round((self.answered_at - self.created_at).total_seconds() / 3600, 1)
