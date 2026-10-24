"""Переводимые поля моделей приложения."""

from modeltranslation.translator import TranslationOptions, register

from apps.accounts.models import User


@register(User)
class UserTranslationOptions(TranslationOptions):
    fields = ("last_name", "first_name", "middle_name")
