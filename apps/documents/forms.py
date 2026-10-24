"""
Формы файлового хранилища.
"""

from __future__ import annotations

import datetime as dt
from typing import cast

from django import forms
from django.conf import settings
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.text import format_lazy
from django.utils.translation import gettext_lazy as _

from apps.documents.models import AccessLevel, Document, DocumentKind, DocumentQuerySet
from apps.people.models import Employee
from core.forms import BaseForm, FilterForm, model_choice
from core.validators import validate_safe_filename


class DocumentUploadForm(BaseForm):
    """Приём документа в хранилище."""

    file = forms.FileField(
        label=_("Файл"),
        # format_lazy, а не оператор %: подстановка через % вычисляет отложенный перевод
        # немедленно, при загрузке модуля.
        help_text=format_lazy(
            _(
                "Допустимые форматы: PDF, документы Word и Excel, изображения, "
                "архив ZIP. Предельный размер — {size} МБ"
            ),
            size=settings.MAX_UPLOAD_SIZE // (1024 * 1024),
        ),
    )
    title = forms.CharField(label=_("Наименование"), max_length=255)
    kind = forms.ChoiceField(
        label=_("Вид документа"),
        choices=DocumentKind.choices,
        initial=DocumentKind.OTHER,
    )
    access_level = forms.ChoiceField(
        label=_("Доступ"),
        choices=AccessLevel.choices,
        initial=AccessLevel.RESTRICTED,
        help_text=_("Кадровые документы по умолчанию доступны только кадровой службе"),
    )
    employee = forms.ModelChoiceField(
        label=_("Сотрудник"),
        queryset=Employee.objects.none(),
        required=False,
        help_text=_("Заполняется, если документ относится к конкретному сотруднику"),
    )
    description = forms.CharField(
        label=_("Описание"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=2000,
    )
    valid_until = forms.DateField(
        label=_("Действителен до"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("Для сертификатов и допусков с ограниченным сроком"),
    )
    supersedes = forms.ModelChoiceField(
        label=_("Замещает документ"),
        queryset=Document.objects.none(),
        required=False,
        help_text=_("Прежняя версия останется в системе и будет помечена как замещённая"),
    )

    def __init__(self, *args, user=None, **kwargs) -> None:
        self.user = user
        super().__init__(*args, **kwargs)

        employees = Employee.objects.active().select_related("position").order_by("last_name")
        if user is not None:
            from core.permissions import apply_employee_scope

            employees = apply_employee_scope(employees, user)
        model_choice(self, "employee").queryset = employees

        # Замещать разрешено только действующую версию: цепочка версий должна
        # оставаться линейной, иначе «текущих» документов окажется несколько.
        model_choice(self, "supersedes").queryset = Document.objects.current_versions().order_by(
            "-created_at"
        )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        validate_safe_filename(upload.name or "")
        return upload

    def clean_valid_until(self) -> dt.date | None:
        value = self.cleaned_data.get("valid_until")
        if value and value < timezone.localdate():
            raise forms.ValidationError(_("Срок действия уже истёк"))
        return value


class DocumentFilterForm(FilterForm):
    """Отбор документов по виду, доступу и наименованию."""

    q = forms.CharField(
        label=_("Поиск"),
        required=False,
        widget=forms.TextInput(
            attrs={"type": "search", "placeholder": _("Наименование, описание, сотрудник")}
        ),
    )
    kind = forms.ChoiceField(
        label=_("Вид"), choices=[("", _("Любой")), *DocumentKind.choices], required=False
    )
    access_level = forms.ChoiceField(
        label=_("Доступ"), choices=[("", _("Любой")), *AccessLevel.choices], required=False
    )
    only_current = forms.BooleanField(label=_("Только действующие версии"), required=False)

    def filter(self, queryset: QuerySet) -> QuerySet:
        # Подпись задана базовой формой; приведение выражает то, что
        # известно вызывающей стороне: сюда приходит выборка документов.
        documents = cast(DocumentQuerySet, queryset)
        query = self.cleaned_data.get("q")
        if query:
            documents = documents.filter(
                Q(title__icontains=query)
                | Q(description__icontains=query)
                | Q(employee__last_name__icontains=query)
            )

        for name in ("kind", "access_level"):
            value = self.cleaned_data.get(name)
            if value:
                documents = documents.filter(**{name: value})

        if self.cleaned_data.get("only_current"):
            documents = documents.current_versions()

        return documents
