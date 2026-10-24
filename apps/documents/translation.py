"""Переводимые поля моделей приложения."""

from modeltranslation.translator import TranslationOptions, register

from apps.documents.models import Document


@register(Document)
class DocumentTranslationOptions(TranslationOptions):
    fields = ("title", "description")
