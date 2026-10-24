"""
Представления публичной части ресурса.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView, FormView, ListView, TemplateView

from apps.portal.content import (
    COMPETENCY_SCALE,
    LEGAL_BASIS,
    METHODOLOGY_STEPS,
    REGULATION_TEMPLATES,
)
from apps.portal.forms import FeedbackForm
from apps.portal.models import Page
from apps.portal.services import (
    article_catalog,
    article_tags,
    faq_sections,
    get_public_summary,
    glossary_index,
    ninebox_demo,
    public_competencies,
    readiness_demo,
    regulation_document,
    render_markup,
    submit_feedback,
)
from core.breadcrumbs import BreadcrumbTrail
from core.mixins import BreadcrumbMixin, PageMetaMixin
from core.navigation import PUBLIC_MENU

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise


class PortalPageMixin(PageMetaMixin, BreadcrumbMixin):
    """Общее поведение публичных страниц: раздел, метаданные, цепочка навигации."""

    page_section = "portal"

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        if self.page_title:
            trail.add(str(self.page_title))
        return trail


class HomeView(PortalPageMixin, TemplateView):
    """Главная страница."""

    template_name = "portal/home.html"
    page_title = _("Формирование кадрового резерва")
    page_description = _(
        "Информационная система формирования и развития кадрового резерва: "
        "оценка компетенций, композитный индекс готовности, матрица 9-box, "
        "планы преемственности и индивидуальные планы развития."
    )

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        # Главная страница уже является корнем цепочки, добавлять нечего.
        return trail

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["summary"] = get_public_summary()
        context["sections"] = [item for item in PUBLIC_MENU if item.description][1:7]
        context["stages"] = STAGES
        # Матрица на главной и страница матрицы строятся по одному обезличенному распределению.
        context["ninebox_cells"] = ninebox_demo()["cells"]
        return context


class AboutView(PortalPageMixin, TemplateView):
    """Назначение, границы и устройство системы."""

    template_name = "portal/about.html"
    page_title = _("О системе")
    page_subtitle = _("Назначение, состав и границы решения")
    page_description = _(
        "Назначение системы Reservix, решаемые задачи, роли пользователей и архитектура решения."
    )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["capabilities"] = CAPABILITIES
        context["roles"] = ROLE_DESCRIPTIONS
        context["boundaries"] = BOUNDARIES
        return context


class ContactsView(PortalPageMixin, TemplateView):
    """Реквизиты и способы связи."""

    template_name = "portal/contacts.html"
    page_title = _("Контакты")
    page_subtitle = _("Как связаться со службой сопровождения системы")
    page_description = _("Контактные данные и форма обратной связи системы Reservix.")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        from apps.org.models import Branch

        context["branches"] = Branch.objects.filter(is_active=True).order_by(
            "-is_head_office", "city"
        )
        return context


class FeedbackView(PortalPageMixin, FormView):
    """Форма обратной связи."""

    template_name = "portal/feedback.html"
    form_class = FeedbackForm
    success_url = reverse_lazy("portal:feedback_sent")
    page_title = _("Обратная связь")
    page_subtitle = _("Вопрос, сообщение об ошибке или предложение")
    page_description = _("Форма обратной связи системы формирования кадрового резерва.")

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Контакты"), "portal:contacts").add(_("Обратная связь"))

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form: FeedbackForm) -> Any:
        submit_feedback(form.instance, self.request)
        messages.success(
            self.request,
            _("Обращение принято. Ответ поступит на указанный адрес электронной почты."),
        )
        return super().form_valid(form)


class FeedbackSentView(PortalPageMixin, TemplateView):
    """Подтверждение приёма обращения."""

    template_name = "portal/feedback_sent.html"
    page_title = _("Обращение принято")

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Контакты"), "portal:contacts").add(_("Обращение принято"))


class StaticPageView(PortalPageMixin, DetailView):
    """Статическая страница, содержимое которой ведёт администратор."""

    template_name = "portal/page.html"
    model = Page
    context_object_name = "page"
    slug_field = "slug"

    def get_queryset(self):
        return Page.objects.filter(is_published=True)

    def get_object(self, queryset=None) -> Page:
        return get_object_or_404(self.get_queryset(), slug=self.kwargs["slug"])

    def get_page_title(self) -> str:
        return self.object.title

    def get_page_description(self) -> str:
        return self.object.meta_description or self.object.subtitle

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(self.object.title)

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_subtitle"] = self.object.subtitle
        context["rendered_body"] = render_markup(self.object.body)
        return context


class MethodologyView(PortalPageMixin, TemplateView):
    """Методика формирования кадрового резерва."""

    template_name = "portal/methodology.html"
    page_title = _("Методология")
    page_subtitle = _("Семь этапов формирования и развития кадрового резерва")
    page_description = _(
        "Методика работы с кадровым резервом: определение критичных должностей, "
        "профили, оценка компетенций, композитный индекс готовности, развитие "
        "и назначение."
    )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["steps"] = METHODOLOGY_STEPS
        context["summary"] = get_public_summary()
        return context


class CompetenciesView(PortalPageMixin, TemplateView):
    """Публичный справочник компетенций."""

    template_name = "portal/competencies.html"
    page_title = _("Модель компетенций")
    page_subtitle = _("Справочник компетенций, корпоративная шкала и поведенческие индикаторы")
    page_description = _(
        "Корпоративная модель компетенций: категории, состав компетенций и "
        "пятиуровневая шкала владения с описанием наблюдаемого поведения."
    )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["categories"] = public_competencies()
        context["scale"] = COMPETENCY_SCALE
        return context


class ReadinessView(PortalPageMixin, TemplateView):
    """Методика расчёта индекса готовности с калькулятором."""

    template_name = "portal/readiness.html"
    page_title = _("Индекс готовности")
    page_subtitle = _("Как из шести частных оценок получается одно число")
    page_description = _(
        "Методика расчёта композитного индекса готовности резервиста: частные "
        "критерии, способы определения весов, свёртка и пороги уровней готовности."
    )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        criteria = readiness_demo()
        context["criteria"] = criteria
        context["calculator_data"] = [
            {
                "code": item.code,
                "name": item.name,
                "weight": item.weight,
                "value": item.value,
            }
            for item in criteria
        ]
        options = settings.RESERVIX
        context["threshold_high"] = options["READINESS_THRESHOLD_HIGH"]
        context["threshold_medium"] = options["READINESS_THRESHOLD_MEDIUM"]
        context["weighting_methods"] = WEIGHTING_METHODS
        return context


class NineBoxView(PortalPageMixin, TemplateView):
    """Матрица «результативность — потенциал» на обезличенной выборке."""

    template_name = "portal/ninebox.html"
    page_title = _("Матрица 9-box")
    page_subtitle = _("Распределение по результативности и потенциалу: обезличенная выборка")
    page_description = _(
        "Матрица 9-box: девять зон «результативность — потенциал», управленческое "
        "решение по каждой зоне и живое обезличенное распределение."
    )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["matrix"] = ninebox_demo()
        return context


class RegulationsView(PortalPageMixin, TemplateView):
    """Нормативная база и типовые формы документов."""

    template_name = "portal/regulations.html"
    page_title = _("Нормативная база")
    page_subtitle = _("Основания работы с кадровым резервом и типовые формы документов")
    page_description = _(
        "Нормативные основания работы с кадровым резервом и типовые формы "
        "документов: положение о резерве, форма выдвижения, бланк плана развития."
    )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["legal_basis"] = LEGAL_BASIS
        context["templates"] = REGULATION_TEMPLATES
        return context


class ArticleListView(PortalPageMixin, ListView):
    """База знаний: материалы по управлению кадровым резервом."""

    template_name = "portal/articles.html"
    context_object_name = "articles"
    paginate_by = 9
    page_title = _("База знаний")
    page_subtitle = _("Материалы по оценке, развитию и преемственности")
    page_description = _(
        "Материалы базы знаний по управлению кадровым резервом: методики оценки, "
        "развитие сотрудников, планирование преемственности."
    )

    def get_queryset(self):
        return article_catalog(
            query=self.request.GET.get("q", "").strip(),
            category=self.request.GET.get("category", "").strip(),
            tag=self.request.GET.get("tag", "").strip(),
        )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        from apps.portal.models import ArticleCategory

        context = super().get_context_data(**kwargs)
        context["categories"] = ArticleCategory.objects.filter(is_active=True).order_by(
            "sort_order", "name"
        )
        context["tags"] = article_tags()
        context["query"] = self.request.GET.get("q", "")
        context["active_category"] = self.request.GET.get("category", "")
        context["active_tag"] = self.request.GET.get("tag", "")

        params = self.request.GET.copy()
        params.pop("page", None)
        context["pagination_query"] = params.urlencode()
        return context


class ArticleDetailView(PortalPageMixin, DetailView):
    """Материал базы знаний."""

    template_name = "portal/article_detail.html"
    context_object_name = "article"

    def get_queryset(self):
        from apps.portal.models import Article

        return Article.objects.published().select_related("category", "author")

    def get_object(self, queryset=None):
        article = get_object_or_404(self.get_queryset(), slug=self.kwargs["slug"])
        article.register_view()
        return article

    def get_page_title(self) -> str:
        return self.object.title

    def get_page_description(self) -> str:
        return self.object.summary

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("База знаний"), "portal:articles").add(self.object.title)

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        article = self.object
        context["rendered_body"] = render_markup(article.body)
        context["related"] = (
            self.get_queryset()
            .filter(category=article.category)
            .exclude(pk=article.pk)
            .order_by("-published_at")[:3]
        )
        return context


class GlossaryView(PortalPageMixin, TemplateView):
    """Глоссарий терминов управления кадровым резервом."""

    template_name = "portal/glossary.html"
    page_title = _("Глоссарий")
    page_subtitle = _("Термины управления кадровым резервом и оценкой персонала")
    page_description = _(
        "Глоссарий терминов: кадровый резерв, компетенция, индекс готовности, "
        "матрица 9-box, преемственность, талант-пул."
    )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        query = self.request.GET.get("q", "").strip()
        groups = glossary_index(query)
        context["groups"] = groups
        context["letters"] = [letter for letter, _terms in groups]
        context["query"] = query
        context["total"] = sum(len(terms) for _letter, terms in groups)
        return context


class FaqView(PortalPageMixin, TemplateView):
    """Справка и частые вопросы."""

    template_name = "portal/faq.html"
    page_title = _("Справка")
    page_subtitle = _("Ответы на частые вопросы по работе с системой")
    page_description = _(
        "Справка по работе с системой кадрового резерва: вопросы сотрудников, "
        "руководителей и кадровой службы."
    )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["sections"] = faq_sections()
        return context


class ApiDocsView(PortalPageMixin, TemplateView):
    """Описание программного интерфейса."""

    template_name = "portal/api_docs.html"
    page_title = _("Программный интерфейс")
    page_subtitle = _("REST API системы: назначение, аутентификация, спецификация")
    page_description = _(
        "Программный интерфейс системы кадрового резерва: назначение, способ "
        "аутентификации, версионирование и спецификация OpenAPI."
    )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["api_version"] = settings.API_VERSION
        context["api_title"] = settings.API_TITLE
        context["endpoints"] = API_SECTIONS
        return context


def regulation_download_view(request, code: str) -> HttpResponse:
    """Выдаёт типовую форму документа в формате .docx."""
    from apps.reporting.renderers import CONTENT_TYPES, render

    try:
        document = regulation_document(code)
    except KeyError as error:
        raise Http404(_("Типовая форма не найдена")) from error

    content = render(document, "docx")
    response = HttpResponse(content, content_type=CONTENT_TYPES["docx"])
    filename = f"{code}.docx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


#: Разделы программного интерфейса для страницы описания API.
API_SECTIONS: list[dict[str, StrOrPromise]] = [
    {
        "name": _("Кадровый состав"),
        "path": "/api/v1/employees/",
        "text": _(
            "Карточки сотрудников, организационная структура, история "
            "должностей. Выдача ограничена областью видимости учётной записи."
        ),
    },
    {
        "name": _("Компетенции"),
        "path": "/api/v1/competencies/",
        "text": _(
            "Справочник компетенций, модели требований и профили соответствия "
            "с указанием полноты оценки."
        ),
    },
    {
        "name": _("Кадровый резерв"),
        "path": "/api/v1/reserve/",
        "text": _(
            "Записи резерва, статусы, заявки на включение и ранжирование "
            "кандидатов на целевую должность."
        ),
    },
    {
        "name": _("Аналитика"),
        "path": "/api/v1/analytics/",
        "text": _(
            "Индекс готовности с разбором по критериям, сравнение схем "
            "оценивания, анализ устойчивости ранжирования, матрица 9-box."
        ),
    },
]

#: Способы определения весов критериев — для страницы методики.
WEIGHTING_METHODS: list[dict[str, StrOrPromise]] = [
    {
        "name": _("Экспертный"),
        "text": _(
            "Веса назначаются кадровой комиссией прямо. Способ прозрачен и не "
            "требует вычислений, но воспроизводимость его невелика: другой "
            "состав комиссии назначит другие веса, и обосновать разницу нечем."
        ),
    },
    {
        "name": _("Метод анализа иерархий"),
        "text": _(
            "Эксперт сравнивает критерии попарно по девятибалльной шкале, веса "
            "получаются как компоненты главного собственного вектора матрицы "
            "сравнений. Ключевое отличие — проверка согласованности: "
            "противоречивые суждения выявляются числом, а не спором."
        ),
    },
    {
        "name": _("Энтропийный"),
        "text": _(
            "Веса выводятся из фактического разброса данных: критерий, по "
            "которому все кандидаты одинаковы, ничего не различает и получает "
            "малый вес. Способ не зависит от мнения, но и не знает о важности: "
            "он измеряет различающую способность, а не значимость."
        ),
    },
]


# ---------------------------------------------------------------------------
# Содержимое, не требующее ведения через базу данных
# ---------------------------------------------------------------------------
# Тексты ниже описывают саму систему и меняются вместе с её функциональностью, поэтому хранятся в
# коде: их редактирование через панель администратора привело бы к расхождению описания и
# фактических возможностей.

#: Этапы работы с кадровым резервом, реализованные в системе.
STAGES: list[dict[str, StrOrPromise]] = [
    {
        "number": "01",
        "title": _("Определение критичных должностей"),
        "text": _(
            "Выявляются должности, уход с которых создаёт риск для деятельности "
            "организации. Для каждой оцениваются вероятность освобождения и "
            "последствия незакрытой вакансии."
        ),
    },
    {
        "number": "02",
        "title": _("Формирование профилей должностей"),
        "text": _(
            "Для целевых должностей описываются требования: образование, стаж, "
            "управленческий опыт и модель компетенций с целевыми уровнями и весами."
        ),
    },
    {
        "number": "03",
        "title": _("Выдвижение кандидатов"),
        "text": _(
            "Кандидаты поступают тремя путями: выдвижение руководителем, "
            "самовыдвижение сотрудника и отбор кадровой службой по результатам "
            "оценки. Каждая заявка проходит согласование."
        ),
    },
    {
        "number": "04",
        "title": _("Оценка компетенций и потенциала"),
        "text": _(
            "Проводятся кампании оценки: тестирование, экспертное интервью, "
            "круговая оценка 360°. Результаты сводятся по единой шкале и "
            "сопоставляются с профилем целевой должности."
        ),
    },
    {
        "number": "05",
        "title": _("Расчёт индекса готовности"),
        "text": _(
            "По шести критериям вычисляется композитный индекс. Веса критериев "
            "определяются экспертно, методом анализа иерархий или энтропийным "
            "методом; ранжирование выполняется взвешенной суммой и методом TOPSIS."
        ),
    },
    {
        "number": "06",
        "title": _("Развитие резервистов"),
        "text": _(
            "Для каждого резервиста формируется индивидуальный план развития, "
            "закрывающий выявленные разрывы компетенций. Назначается наставник, "
            "контролируются сроки и результаты мероприятий."
        ),
    },
    {
        "number": "07",
        "title": _("Назначение и оценка результата"),
        "text": _(
            "При освобождении должности система предлагает ранжированный список "
            "преемников. После назначения фиксируется результат — он служит "
            "обратной связью для проверки качества методики отбора."
        ),
    },
]

#: Ключевые возможности системы.
CAPABILITIES: list[dict[str, StrOrPromise]] = [
    {
        "icon": "layers",
        "title": _("Модель компетенций"),
        "text": _(
            "Справочник компетенций с пятиуровневой шкалой и поведенческими "
            "индикаторами. Модели привязываются к должностям и семействам "
            "должностей, требования имеют целевые уровни и веса."
        ),
    },
    {
        "icon": "gauge",
        "title": _("Композитный индекс готовности"),
        "text": _(
            "Шесть частных критериев сводятся к единой шкале 0–100. Методика "
            "открыта: пользователь видит вклад каждого критерия и может "
            "проследить, из чего сложилась итоговая оценка."
        ),
    },
    {
        "icon": "grid",
        "title": _("Матрица 9-box"),
        "text": _(
            "Автоматическое размещение сотрудников по осям результативности и "
            "потенциала с последующей калибровкой руководителями. История "
            "перемещений сохраняется вместе с обоснованием."
        ),
    },
    {
        "icon": "route",
        "title": _("Планы преемственности"),
        "text": _(
            "Для критичных должностей ведутся списки преемников с рангами и "
            "сроками достижения готовности. Рассчитываются глубина скамейки и "
            "коэффициент покрытия критичных позиций."
        ),
    },
    {
        "icon": "target",
        "title": _("Индивидуальные планы развития"),
        "text": _(
            "Мероприятия связаны с конкретными разрывами компетенций: план "
            "закрывает выявленные пробелы, а не составляется формально. "
            "Контролируются сроки, результаты и подтверждающие документы."
        ),
    },
    {
        "icon": "chart",
        "title": _("Аналитика и отчётность"),
        "text": _(
            "Метрики здоровья резерва, анализ устойчивости ранжирования, "
            "выгрузка отчётов в форматы .docx, .xlsx и .pdf для представления "
            "руководству и кадрового делопроизводства."
        ),
    },
]

#: Роли пользователей и решаемые ими задачи.
ROLE_DESCRIPTIONS: list[dict[str, StrOrPromise]] = [
    {
        "role": _("Администратор"),
        "text": _(
            "Ведёт учётные записи и права, организационную структуру и "
            "справочники, настраивает модель расчёта индекса, контролирует "
            "журнал аудита и резервное копирование."
        ),
    },
    {
        "role": _("HR-специалист"),
        "text": _(
            "Управляет составом резерва, проводит кампании оценки, ведёт "
            "калибровку матрицы 9-box, формирует планы преемственности и "
            "отчётность для руководства."
        ),
    },
    {
        "role": _("Руководитель"),
        "text": _(
            "Выдвигает кандидатов, оценивает сотрудников подразделения, "
            "согласовывает планы развития и заявки, отслеживает готовность "
            "своих резервистов."
        ),
    },
    {
        "role": _("Наставник"),
        "text": _(
            "Сопровождает закреплённых резервистов, подтверждает выполнение "
            "мероприятий развития и предоставляет обратную связь о прогрессе."
        ),
    },
    {
        "role": _("Сотрудник"),
        "text": _(
            "Видит свой профиль компетенций и разрывы относительно целевой "
            "должности, работает с индивидуальным планом развития, подаёт "
            "заявку на включение в резерв."
        ),
    },
]

#: Осознанные границы решения. Честное перечисление того, чего система не
#: делает, избавляет от завышенных ожиданий при внедрении.
BOUNDARIES: list[StrOrPromise] = [
    _(
        "Система не ведёт кадровое делопроизводство и не заменяет учётную систему "
        "по труду: приказы, трудовые договоры и табельный учёт остаются вне контура."
    ),
    _("Система не рассчитывает заработную плату и не хранит сведения о доходах."),
    _("Система не подбирает внешних кандидатов: её предмет — внутренний резерв организации."),
    _(
        "Решение о назначении принимает человек. Система готовит обоснованный "
        "ранжированный список, но не выполняет назначение автоматически."
    ),
]
