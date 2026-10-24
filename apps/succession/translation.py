"""Переводимые поля моделей приложения."""

from modeltranslation.translator import TranslationOptions, register

from apps.succession.models import Appointment, KeyPosition, SuccessionPlan, SuccessionSlate


@register(SuccessionPlan)
class SuccessionPlanTranslationOptions(TranslationOptions):
    fields = ("title", "risks")


@register(KeyPosition)
class KeyPositionTranslationOptions(TranslationOptions):
    fields = ("impact_of_loss", "justification")


@register(SuccessionSlate)
class SuccessionSlateTranslationOptions(TranslationOptions):
    fields = ("development_focus",)


@register(Appointment)
class AppointmentTranslationOptions(TranslationOptions):
    fields = ("comment",)
