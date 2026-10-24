"""Проверка локализации."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

import pytest

from django.conf import settings
from django.template import Context, Template
from django.utils import translation
from django.utils.formats import date_format, number_format

from core.navigation import PUBLIC_MENU, ROLE_MENUS
from core.templatetags.reservix import date_ru, percent, score, spaced

pytestmark = pytest.mark.unit

#: Каталоги переводов проекта.
LOCALE_ROOT = Path(settings.BASE_DIR) / "locale"

#: Строки, которые допустимо писать в шаблоне без перевода: единицы измерения,
#: знаки препинания и обозначения, одинаковые во всех языках.
UNIVERSAL_TOKENS = frozenset({"%", "—", "·", "…", "9-box", "360°", "ID", "OK"})


class TestConfiguration:
    """Настройка подсистемы локализации."""

    def test_two_languages_declared(self):
        codes = {code for code, _name in settings.LANGUAGES}

        assert codes == {"ru", "en"}

    def test_russian_is_default(self):
        """Русский — язык по умолчанию, и он же отдаётся по адресам без префикса."""
        assert settings.LANGUAGE_CODE == "ru"

    def test_timezone_and_localization_enabled(self):
        assert settings.USE_I18N is True
        assert settings.USE_L10N is True
        assert settings.USE_TZ is True

    def test_locale_path_declared(self):
        assert LOCALE_ROOT in [Path(path) for path in settings.LOCALE_PATHS]


class TestNumberAndDateFormats:
    """Форматы чисел и дат в русском интерфейсе."""

    def test_date_uses_russian_order(self):
        """Дата выводится как «30.08.2026», а не «08/30/2026»."""
        with translation.override("ru"):
            rendered = date_format(dt.date(2026, 8, 30), "SHORT_DATE_FORMAT")

        assert rendered == "30.08.2026"

    def test_date_filter_matches_russian_order(self):
        assert date_ru(dt.date(2026, 8, 30)) == "30.08.2026"

    def test_decimal_separator_is_comma(self):
        """Разделитель дробной части — запятая."""
        with translation.override("ru"):
            rendered = number_format(Decimal("62.5"), decimal_pos=1)

        assert rendered == "62,5"

    def test_score_filter_uses_comma(self):
        assert score(Decimal("62.50"), 1) == "62,5"

    def test_percent_filter_uses_comma(self):
        """Доля выводится с запятой и неразрывным пробелом перед знаком процента."""
        assert percent(10.65, 1) == "10,7 %"

    def test_thousands_are_separated_by_space(self):
        """Разряды разделяются пробелом, а не запятой."""
        assert spaced(1234567) == "1 234 567"

    def test_english_locale_keeps_period_separator(self):
        """В английском интерфейсе разделитель дробной части — точка."""
        with translation.override("en"):
            rendered = number_format(Decimal("62.5"), decimal_pos=1)

        assert "." in rendered


class TestTranslationMarking:
    """Разметка строк для перевода."""

    def test_navigation_labels_are_lazy(self):
        """Подписи пунктов меню объявлены отложенным переводом."""
        from django.utils.functional import Promise

        for menu in (PUBLIC_MENU, *ROLE_MENUS.values()):
            for item in menu:
                assert isinstance(item.label, Promise), (
                    f"Подпись «{item.label}» не объявлена через gettext_lazy"
                )

    def test_templates_load_i18n_when_they_translate(self):
        """Шаблон, использующий теги перевода, загружает библиотеку `i18n`."""
        offenders: list[str] = []
        translate_tag = re.compile(r"{%\s*(translate|trans|blocktranslate|blocktrans)\b")

        for template in Path(settings.BASE_DIR, "templates").rglob("*.html"):
            source = template.read_text(encoding="utf-8")
            if translate_tag.search(source) and "{% load" not in source:
                offenders.append(str(template))

        assert not offenders, "Шаблоны с тегами перевода без {% load i18n %}: " + ", ".join(
            offenders
        )

    def test_page_titles_are_translatable(self):
        """Заголовки страниц объявлены через отложенный перевод."""
        from django.utils.functional import Promise

        from apps.dashboard.views import (
            EmployeeDashboardView,
            HrDashboardView,
            ManagerDashboardView,
        )
        from apps.people.views import EmployeeListView, TeamView

        for view in (
            HrDashboardView,
            ManagerDashboardView,
            EmployeeDashboardView,
            EmployeeListView,
            TeamView,
        ):
            assert isinstance(view.page_title, Promise), (
                f"Заголовок {view.__name__} не объявлен через gettext_lazy"
            )


class TestCatalogs:
    """Полнота каталогов перевода."""

    @staticmethod
    def catalog(language: str) -> Path:
        return LOCALE_ROOT / language / "LC_MESSAGES" / "django.po"

    @pytest.mark.parametrize("language", ["ru", "en"])
    def test_catalog_has_no_empty_translations(self, language: str):
        path = self.catalog(language)
        if not path.exists():
            pytest.skip(
                f"Каталог {path.relative_to(settings.BASE_DIR)} не собран. "
                "Соберите: just messages (требуются утилиты GNU gettext)"
            )

        source = path.read_text(encoding="utf-8")
        entries = re.findall(r'msgid "((?:[^"\\]|\\.)+)"\nmsgstr ""\n(?!")', source)

        assert not entries, f"Непереведённых строк в каталоге {language}: {len(entries)}"

    @pytest.mark.parametrize("language", ["ru", "en"])
    def test_catalog_has_no_fuzzy_entries(self, language: str):
        """В каталоге нет строк, помеченных как приблизительные."""
        path = self.catalog(language)
        if not path.exists():
            pytest.skip("Каталог не собран")

        source = path.read_text(encoding="utf-8")
        fuzzy = [line for line in source.splitlines() if line.startswith("#, fuzzy")]

        assert not fuzzy, f"Приблизительных переводов в каталоге {language}: {len(fuzzy)}"


class TestLanguageSwitching:
    """Переключение языка интерфейса."""

    def test_template_renders_in_selected_language(self):
        """Один и тот же шаблон отрисовывается по-разному в двух локалях."""
        template = Template("{% load i18n %}{% translate 'Yes' %}")

        with translation.override("ru"):
            russian = template.render(Context())
        with translation.override("en"):
            english = template.render(Context())

        assert english == "Yes"
        assert russian != english

    @pytest.mark.django_db
    def test_language_code_reaches_template_context(self, client):
        """Код языка попадает в контекст и проставляется в атрибут `lang`."""
        response = client.get("/")

        assert 'lang="ru"' in response.content.decode()

    def test_switching_endpoint_is_connected(self):
        from django.urls import reverse

        assert reverse("set_language") == "/i18n/setlang/"

    @pytest.mark.django_db
    def test_switch_to_english_adds_prefix(self, client):
        """Переход на английский уводит на адрес с префиксом языка."""
        response = client.post("/i18n/setlang/", {"language": "en", "next": "/readiness/"})

        assert response.status_code == 302
        assert response["Location"] == "/en/readiness/"

    @pytest.mark.django_db
    def test_switch_back_to_russian_drops_prefix(self, client):
        """Возврат к русскому убирает префикс языка из адреса."""
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"

        response = client.post("/i18n/setlang/", {"language": "ru", "next": "/en/readiness/"})

        assert response.status_code == 302
        assert response["Location"] == "/readiness/"

    @pytest.mark.django_db
    def test_switching_sets_language_cookie(self, client):
        response = client.post("/i18n/setlang/", {"language": "en", "next": "/"})

        assert response.cookies[settings.LANGUAGE_COOKIE_NAME].value == "en"

    @pytest.mark.django_db
    def test_unknown_language_is_ignored(self, client):
        """Неизвестный код языка не меняет ни адрес, ни cookie."""
        response = client.post("/i18n/setlang/", {"language": "xx", "next": "/readiness/"})

        assert response.status_code == 302
        assert response["Location"] == "/readiness/"
        assert settings.LANGUAGE_COOKIE_NAME not in response.cookies

    @pytest.mark.django_db
    def test_external_next_is_rejected(self, client):
        """Адрес возврата на чужой домен отбрасывается."""
        response = client.post("/i18n/setlang/", {"language": "en", "next": "https://example.com/"})

        assert response["Location"] == "/en/"


class TestLocaleAwareFormatting:
    """Форматы, зависящие от языка интерфейса."""

    def test_number_separators_follow_language(self):
        with translation.override("ru"):
            assert spaced(1234567) == "1 234 567"
        with translation.override("en"):
            assert spaced(1234567) == "1,234,567"

    def test_decimal_separator_follows_language(self):
        with translation.override("ru"):
            assert score(Decimal("62.50"), 1) == "62,5"
        with translation.override("en"):
            assert score(Decimal("62.50"), 1) == "62.5"

    def test_date_order_follows_language(self):
        with translation.override("ru"):
            assert date_ru(dt.date(2026, 8, 30)) == "30.08.2026"
        with translation.override("en"):
            assert date_ru(dt.date(2026, 8, 30)) == "08/30/2026"

    def test_plural_forms_follow_language(self):
        """Число форм слова зависит от языка: в русском три, в английском две."""
        from core.templatetags.reservix import counted
        from core.utils import MONTH_FORMS

        with translation.override("ru"):
            forms = str(MONTH_FORMS)
            assert counted(1, forms) == "1 месяц"
            assert counted(3, forms) == "3 месяца"
            assert counted(5, forms) == "5 месяцев"

        with translation.override("en"):
            forms = str(MONTH_FORMS)
            assert counted(1, forms) == "1 month"
            assert counted(3, forms) == "3 months"
