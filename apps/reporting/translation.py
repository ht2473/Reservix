"""Переводимые поля моделей приложения."""

from modeltranslation.translator import TranslationOptions, register

from apps.reporting.models import ReportTemplate


@register(ReportTemplate)
class ReportTemplateTranslationOptions(TranslationOptions):
    fields = ("name", "description", "header_note")
