"""Переводимые поля моделей приложения."""

from modeltranslation.translator import TranslationOptions, register

from apps.development.models import (
    ActivityEvidence,
    DevelopmentActivity,
    DevelopmentPlan,
    LearningResource,
    Mentorship,
)


@register(DevelopmentPlan)
class DevelopmentPlanTranslationOptions(TranslationOptions):
    fields = ("title", "goal", "summary")


@register(DevelopmentActivity)
class DevelopmentActivityTranslationOptions(TranslationOptions):
    fields = ("title", "description", "result")


@register(ActivityEvidence)
class ActivityEvidenceTranslationOptions(TranslationOptions):
    fields = ("title", "comment")


@register(LearningResource)
class LearningResourceTranslationOptions(TranslationOptions):
    fields = ("name", "description", "provider")


@register(Mentorship)
class MentorshipTranslationOptions(TranslationOptions):
    fields = ("goal", "result")
