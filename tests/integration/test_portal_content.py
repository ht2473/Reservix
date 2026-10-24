"""Проверка содержательных страниц публичной части."""

from __future__ import annotations

import pytest

from django.urls import reverse

from apps.portal.content import (
    COMPETENCY_SCALE,
    LEGAL_BASIS,
    METHODOLOGY_STEPS,
    NINE_BOX_ZONES,
    REGULATION_TEMPLATES,
)
from apps.portal.models import GlossaryTerm
from apps.portal.services import (
    article_catalog,
    article_tags,
    glossary_index,
    ninebox_demo,
    readiness_demo,
    regulation_document,
)
from tests.factories import ArticleCategoryFactory, ArticleFactory, GlossaryTermFactory

pytestmark = [pytest.mark.django_db, pytest.mark.integration]


# ---------------------------------------------------------------------------
# Методология и справочники
# ---------------------------------------------------------------------------
class TestMethodology:
    """Страница методики."""

    def test_описаны_все_семь_этапов(self, client):
        response = client.get(reverse("portal:methodology"))
        assert response.status_code == 200
        assert len(response.context["steps"]) == 7

    def test_каждый_этап_называет_вход_и_результат(self):
        """
        Этап, описанный перечнем действий без результата, читается как регламент: проверить его
        выполнение нечем.
        """
        for step in METHODOLOGY_STEPS:
            assert step["input"], step["title"]
            assert step["output"], step["title"]


class TestCompetenciesPage:
    """Публичный справочник компетенций."""

    def test_шкала_описана_поведением(self, client):
        response = client.get(reverse("portal:competencies"))
        assert response.status_code == 200
        assert len(COMPETENCY_SCALE) == 5

        body = response.content.decode()
        assert "Уверенное применение" in body

    def test_страница_не_раскрывает_оценки_работников(self, client, db, department):
        """
        Набор компетенций организации закрытым сведением не является, а уровни конкретных людей —
        являются.
        """
        from tests.factories.domain import CompetencyFactory, EmployeeFactory

        EmployeeFactory(department=department, last_name="Одинцов")
        CompetencyFactory(name="Делегирование")

        body = client.get(reverse("portal:competencies")).content.decode()
        assert "Делегирование" in body
        assert "Одинцов" not in body


# ---------------------------------------------------------------------------
# Индекс готовности
# ---------------------------------------------------------------------------
class TestReadinessPage:
    """Методика расчёта и калькулятор."""

    def test_без_настроенной_схемы_калькулятор_скрыт(self, client, db):
        """
        Показывать выдуманные веса недопустимо: страница описывает методику, по которой система
        действительно считает.
        """
        response = client.get(reverse("portal:readiness"))
        assert response.status_code == 200
        assert response.context["criteria"] == []
        assert "data-readiness-calculator" not in response.content.decode()

    def test_веса_берутся_из_действующей_схемы(self, client, db):
        from apps.analytics.services import ensure_criteria, ensure_default_scheme

        ensure_criteria()
        scheme = ensure_default_scheme()

        criteria = readiness_demo()
        assert len(criteria) == scheme.weights.count()
        assert sum(item.weight for item in criteria) == pytest.approx(1.0, abs=0.01)

        response = client.get(reverse("portal:readiness"))
        assert "data-readiness-calculator" in response.content.decode()

    def test_пороги_совпадают_с_настройками_организации(self, client, db, settings):
        response = client.get(reverse("portal:readiness"))
        assert response.context["threshold_high"] == settings.RESERVIX["READINESS_THRESHOLD_HIGH"]
        assert (
            response.context["threshold_medium"] == settings.RESERVIX["READINESS_THRESHOLD_MEDIUM"]
        )

    def test_описаны_все_три_способа_определения_весов(self, client, db):
        """
        Сравнение схем между собой составляет предмет исследовательской части работы, и умолчание
        об альтернативах исказило бы изложение.
        """
        response = client.get(reverse("portal:readiness"))
        assert len(response.context["weighting_methods"]) == 3


class TestNineBoxPage:
    """Демонстрация матрицы на обезличенной выборке."""

    def test_выводятся_девять_зон(self, client, db):
        response = client.get(reverse("portal:ninebox"))
        assert response.status_code == 200
        assert len(response.context["matrix"]["cells"]) == 9
        assert len(NINE_BOX_ZONES) == 9

    def test_каждой_зоне_отвечает_решение(self):
        """
        Одинаковая работа со «звездой» и с «нераскрытым потенциалом» обесценивает саму процедуру
        размещения.
        """
        for zone in NINE_BOX_ZONES:
            assert zone["decision"], zone["code"]

    def test_фамилии_на_страницу_не_попадают(self, client, db, department):
        """
        Страница открыта поисковым обходчикам, и связка «человек — зона матрицы» попала бы в
        поисковый индекс.
        """
        from apps.analytics.models import NineBoxPlacement
        from tests.factories.domain import EmployeeFactory, NineBoxPlacementFactory

        employee = EmployeeFactory(department=department, last_name="Тихомиров")
        NineBoxPlacementFactory(employee=employee)
        assert NineBoxPlacement.objects.count() == 1

        response = client.get(reverse("portal:ninebox"))
        assert "Тихомиров" not in response.content.decode()
        assert response.context["matrix"]["total"] == 1

    def test_показатель_концентрации_считается(self, client, db):
        matrix = ninebox_demo()
        assert "concentration" in matrix
        assert "is_discriminating" in matrix

    def test_сетка_снабжена_подписями_осей(self, client, db):
        """
        Подписи осей — часть самой сетки, а не соседние элементы: разметка рассчитана на колонку
        подписи, и без них девять ячеек растекаются по четырём колонкам, превращая матрицу в
        полосу.
        """
        body = client.get(reverse("portal:ninebox")).content.decode()
        assert 'class="ninebox__axis-y"' in body
        assert 'class="ninebox__axis-x"' in body
        assert body.count("ninebox__cell ninebox__cell--zone-") == 9


# ---------------------------------------------------------------------------
# База знаний и глоссарий
# ---------------------------------------------------------------------------
class TestArticles:
    """Материалы базы знаний."""

    @pytest.fixture
    def articles(self, db):
        category = ArticleCategoryFactory(slug="reserve", name="Кадровый резерв")
        other = ArticleCategoryFactory(slug="assessment", name="Оценка")
        return [
            ArticleFactory(
                category=category,
                title="Глубина скамейки запасных",
                tags="резерв, метрики",
                is_published=True,
            ),
            ArticleFactory(
                category=other,
                title="Круговая оценка",
                tags="оценка",
                is_published=True,
            ),
            ArticleFactory(category=other, title="Черновик материала", is_published=False),
        ]

    def test_неопубликованный_материал_скрыт(self, client, articles):
        response = client.get(reverse("portal:articles"))
        body = response.content.decode()
        assert "Черновик материала" not in body
        assert article_catalog().count() == 2

    def test_отбор_по_разделу(self, client, articles):
        response = client.get(reverse("portal:articles"), {"category": "reserve"})
        titles = {item.title for item in response.context["articles"]}
        assert titles == {"Глубина скамейки запасных"}

    def test_поиск_по_заголовку(self, client, articles):
        response = client.get(reverse("portal:articles"), {"q": "скамейки"})
        assert len(response.context["articles"]) == 1

    def test_метки_считаются_с_числом_упоминаний(self, articles):
        """Метка на одной статье и метка на пятнадцати требуют разного внимания читателя."""
        tags = dict(article_tags())
        assert tags["резерв"] == 1
        assert tags["оценка"] == 1

    def test_счётчик_просмотров_растёт_при_открытии(self, client, articles):
        article = articles[0]
        client.get(article.get_absolute_url())

        article.refresh_from_db()
        assert article.view_count == 1

    def test_неопубликованный_материал_отдаёт_404(self, client, articles):
        response = client.get(articles[2].get_absolute_url())
        assert response.status_code == 404

    def test_разметка_материала_преобразуется_безопасно(self, client, db):
        """
        Готовый HTML в базе не хранится: администратор контента не должен иметь возможности
        поместить сценарий на общедоступную страницу.
        """
        article = ArticleFactory(
            title="Материал с разметкой",
            body="## Заголовок\n\n<script>alert(1)</script>\n\n* пункт",
            is_published=True,
        )
        response = client.get(article.get_absolute_url())
        body = response.content.decode()

        assert "<h2>Заголовок</h2>" in body
        assert "<script>alert(1)</script>" not in body


class TestGlossary:
    """Глоссарий терминов."""

    @pytest.fixture
    def terms(self, db):
        return [
            GlossaryTermFactory(term="Компетенция", english="Competency"),
            GlossaryTermFactory(term="Кадровый резерв", english="Talent pool"),
            GlossaryTermFactory(term="Ротация", english="Job rotation"),
        ]

    def test_термины_сгруппированы_по_буквам(self, client, terms):
        response = client.get(reverse("portal:glossary"))
        assert response.status_code == 200

        letters = response.context["letters"]
        assert letters == ["К", "Р"]
        assert response.context["total"] == 3

    def test_поиск_сужает_словарь(self, client, terms):
        response = client.get(reverse("portal:glossary"), {"q": "ротац"})
        assert response.context["total"] == 1

    def test_поиск_учитывает_английское_соответствие(self, terms):
        """
        Значительная часть литературы по управлению талантами существует только на английском, и
        читателю нужен мостик к ней.
        """
        groups = glossary_index("talent pool")
        assert sum(len(items) for _letter, items in groups) == 1

    def test_неопубликованный_термин_скрыт(self, client, terms):
        GlossaryTermFactory(term="Черновой термин", is_published=False)
        assert GlossaryTerm.objects.count() == 4

        response = client.get(reverse("portal:glossary"))
        assert response.context["total"] == 3

    def test_наполнение_даёт_словарь_рабочего_объёма(self, db):
        """
        Глоссарий порядка восьмидесяти терминов: меньший словарь не покрывает предметную область.
        """
        from django.core.management import call_command

        call_command("seed_portal", verbosity=0)
        assert GlossaryTerm.objects.filter(is_published=True).count() >= 80


class TestFaq:
    """Справка по ролям."""

    def test_вопросы_сгруппированы_по_аудитории(self, client, db):
        from django.core.management import call_command

        call_command("seed_portal", verbosity=0)

        response = client.get(reverse("portal:faq"))
        sections = list(response.context["sections"])
        assert len(sections) >= 4
        assert all(section.audience for section in sections)


# ---------------------------------------------------------------------------
# Нормативная база
# ---------------------------------------------------------------------------
class TestRegulations:
    """Нормативные основания и типовые формы."""

    def test_основания_приведены_с_содержанием(self):
        """Перечень номеров статей без указания, что из них следует, читателю бесполезен."""
        for item in LEGAL_BASIS:
            assert item["reference"]
            assert len(str(item["meaning"])) > 80

    def test_форма_выгружается_в_docx(self, client, db):
        template = REGULATION_TEMPLATES[0]
        response = client.get(reverse("portal:regulation_download", args=[template["code"]]))
        assert response.status_code == 200
        assert response.content[:2] == b"PK"
        assert "attachment" in response["Content-Disposition"]

    def test_содержимое_формы_попадает_в_документ(self, db):
        document = regulation_document("reserve-policy")
        assert document.tables[0].row_count == 5
        assert "Общие положения" in {row["section"] for row in document.tables[0].rows}

    def test_несуществующая_форма_отдаёт_404(self, client, db):
        response = client.get(reverse("portal:regulation_download", args=["unknown-form"]))
        assert response.status_code == 404

    def test_документ_помечен_как_образец(self, db):
        """
        Форма не является нормативным актом организации, и документ обязан это сообщать: иначе
        выгруженный образец подшивают как действующий.
        """
        document = regulation_document("nomination-form")
        assert any("не является нормативным актом" in note for note in document.notes)


# ---------------------------------------------------------------------------
# Программный интерфейс
# ---------------------------------------------------------------------------
class TestApiDocs:
    """Страница описания программного интерфейса."""

    def test_версия_совпадает_с_настройками(self, client, db, settings):
        response = client.get(reverse("portal:api_docs"))
        assert response.context["api_version"] == settings.API_VERSION

    def test_страница_ведёт_к_машиночитаемой_спецификации(self, client, db):
        """
        Перечень методов на странице не дублируется: он изменится вместе с программой, а страница —
        нет.
        """
        body = client.get(reverse("portal:api_docs")).content.decode()
        assert "/api/v1/openapi.json" in body
        assert "/api/v1/docs" in body


class TestPublicSummary:
    """Показатели главной страницы обезличены."""

    def test_сводка_содержит_только_численности(self, db, department):
        from apps.portal.services import get_public_summary
        from tests.factories.domain import EmployeeFactory

        EmployeeFactory(department=department)
        summary = get_public_summary(use_cache=False)

        assert summary.employees == 1
        assert isinstance(summary.coverage_percent, float)
        # В сводке нет ни одного поля со сведениями о конкретном человеке.
        assert not hasattr(summary, "employee")
