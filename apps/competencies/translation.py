"""Переводимые поля моделей приложения."""

from modeltranslation.translator import TranslationOptions, register

from apps.competencies.models import (
    Competency,
    CompetencyCategory,
    CompetencyModel,
    CompetencyRequirement,
    CompetencyScaleLevel,
)


@register(CompetencyCategory)
class CompetencyCategoryTranslationOptions(TranslationOptions):
    fields = ("name", "description")


@register(Competency)
class CompetencyTranslationOptions(TranslationOptions):
    fields = ("name", "description", "definition", "development_hint")


@register(CompetencyScaleLevel)
class CompetencyScaleLevelTranslationOptions(TranslationOptions):
    fields = ("name", "indicators")


@register(CompetencyModel)
class CompetencyModelTranslationOptions(TranslationOptions):
    fields = ("name", "description")


@register(CompetencyRequirement)
class CompetencyRequirementTranslationOptions(TranslationOptions):
    fields = ("comment",)
