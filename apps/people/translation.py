"""Переводимые поля моделей приложения."""

from modeltranslation.translator import TranslationOptions, register

from apps.people.models import Employee, EmploymentHistory


@register(Employee)
class EmployeeTranslationOptions(TranslationOptions):
    fields = ("last_name", "first_name", "middle_name", "education")


@register(EmploymentHistory)
class EmploymentHistoryTranslationOptions(TranslationOptions):
    fields = ("position_title",)
