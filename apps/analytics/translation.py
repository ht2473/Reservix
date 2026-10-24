"""Переводимые поля моделей приложения."""

from modeltranslation.translator import TranslationOptions, register

from apps.analytics.models import (
    CalibrationSession,
    NineBoxPlacement,
    ScoringCriterion,
    ScoringScheme,
    ScoringWeight,
)


@register(ScoringCriterion)
class ScoringCriterionTranslationOptions(TranslationOptions):
    fields = ("name", "description", "unit", "methodology")


@register(ScoringScheme)
class ScoringSchemeTranslationOptions(TranslationOptions):
    fields = ("name", "description", "source_note")


@register(CalibrationSession)
class CalibrationSessionTranslationOptions(TranslationOptions):
    fields = ("name", "description", "agenda", "decisions")


@register(ScoringWeight)
class ScoringWeightTranslationOptions(TranslationOptions):
    fields = ("comment",)


@register(NineBoxPlacement)
class NineBoxPlacementTranslationOptions(TranslationOptions):
    fields = ("comment",)
