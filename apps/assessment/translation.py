"""Переводимые поля моделей приложения."""

from modeltranslation.translator import TranslationOptions, register

from apps.assessment.models import (
    AssessmentCampaign,
    AssessmentSession,
    Test,
    TestOption,
    TestQuestion,
)


@register(AssessmentCampaign)
class AssessmentCampaignTranslationOptions(TranslationOptions):
    fields = ("name", "description", "instructions")


@register(Test)
class TestTranslationOptions(TranslationOptions):
    fields = ("name", "description", "instructions")


@register(TestQuestion)
class TestQuestionTranslationOptions(TranslationOptions):
    fields = ("text", "explanation")


@register(TestOption)
class TestOptionTranslationOptions(TranslationOptions):
    fields = ("text",)


@register(AssessmentSession)
class AssessmentSessionTranslationOptions(TranslationOptions):
    fields = ("summary",)
