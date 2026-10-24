"""Переводимые поля моделей приложения."""

from modeltranslation.translator import TranslationOptions, register

from apps.portal.models import Article, ArticleCategory, FaqCategory, FaqItem, GlossaryTerm, Page


@register(ArticleCategory)
class ArticleCategoryTranslationOptions(TranslationOptions):
    fields = ("name", "description")


@register(Article)
class ArticleTranslationOptions(TranslationOptions):
    fields = ("title", "summary", "body", "tags")


@register(GlossaryTerm)
class GlossaryTermTranslationOptions(TranslationOptions):
    fields = ("term", "definition")


@register(FaqCategory)
class FaqCategoryTranslationOptions(TranslationOptions):
    fields = ("name", "audience")


@register(FaqItem)
class FaqItemTranslationOptions(TranslationOptions):
    fields = ("question", "answer")


@register(Page)
class PageTranslationOptions(TranslationOptions):
    fields = ("title", "subtitle", "body", "meta_description")
