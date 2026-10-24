"""Переводимые поля моделей приложения."""

from modeltranslation.translator import TranslationOptions, register

from apps.reserve.models import (
    ReserveEntry,
    ReserveGroup,
    ReserveMembership,
    ReserveNomination,
    ReserveStatusHistory,
)


@register(ReserveGroup)
class ReserveGroupTranslationOptions(TranslationOptions):
    fields = ("name", "description")


@register(ReserveEntry)
class ReserveEntryTranslationOptions(TranslationOptions):
    fields = ("justification", "exclusion_reason")


@register(ReserveStatusHistory)
class ReserveStatusHistoryTranslationOptions(TranslationOptions):
    fields = ("reason",)


@register(ReserveNomination)
class ReserveNominationTranslationOptions(TranslationOptions):
    fields = ("justification", "manager_comment", "hr_comment", "rejection_reason")


@register(ReserveMembership)
class ReserveMembershipTranslationOptions(TranslationOptions):
    fields = ("leave_reason",)
