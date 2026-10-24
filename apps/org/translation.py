"""Переводимые поля моделей приложения."""

from modeltranslation.translator import TranslationOptions, register

from apps.org.models import Branch, Department, Position, PositionGrade, PositionProfile


@register(Branch)
class BranchTranslationOptions(TranslationOptions):
    fields = ("name", "description", "city", "address")


@register(Department)
class DepartmentTranslationOptions(TranslationOptions):
    fields = ("name",)


@register(PositionGrade)
class PositionGradeTranslationOptions(TranslationOptions):
    fields = ("name",)


@register(Position)
class PositionTranslationOptions(TranslationOptions):
    fields = ("name", "description")


@register(PositionProfile)
class PositionProfileTranslationOptions(TranslationOptions):
    fields = ("purpose", "responsibilities")
