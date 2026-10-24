"""Панель администрирования системы."""

from __future__ import annotations

import contextlib
import datetime as dt
from typing import Any

from django.contrib import messages
from django.db.models import Avg, Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView, ListView, TemplateView, UpdateView
from django.views.generic.edit import CreateView

from apps.accounts.models import LoginRecord, User
from apps.administration.forms import FeedbackAnswerForm, UserFilterForm, UserForm
from apps.analytics.models import ScoringCriterion, ScoringScheme
from apps.assessment.models import AssessmentCampaign, AssessmentScore, AssessmentSession
from apps.audit.models import AuditLog
from apps.competencies.models import Competency, CompetencyCategory, CompetencyModel
from apps.development.models import (
    DevelopmentActivity,
    DevelopmentPlan,
    LearningResource,
    Mentorship,
)
from apps.org.models import Branch, Department, Position, PositionGrade, PositionProfile
from apps.people.models import Employee, EmploymentHistory
from apps.portal.models import Article, FaqItem, Feedback, GlossaryTerm, Page
from apps.reserve.models import ReserveEntry, ReserveGroup, ReserveNomination
from apps.succession.models import Appointment, KeyPosition, SuccessionPlan
from core.constants import Role
from core.mixins import (
    BreadcrumbMixin,
    FilterMixin,
    PageMetaMixin,
    RoleRequiredMixin,
    SortableMixin,
    WindowedPaginationMixin,
)

#: Количество записей на странице в списках панели.
ADMIN_PAGE_SIZE = 50


class AdminBaseMixin(RoleRequiredMixin, PageMetaMixin, BreadcrumbMixin):
    """Общее поведение страниц панели: доступ, заголовки, цепочка навигации."""

    allowed_roles = (Role.ADMIN,)
    page_section = "administration"
    permission_denied_message = _("Раздел администрирования доступен только администратору")

    def get_breadcrumbs(self, trail):
        """Добавляет корень раздела администрирования перед заголовком страницы."""
        trail.add(_("Администрирование"), "administration:index")
        title = getattr(self, "page_title", "")
        if title and self.request.path != reverse("administration:index"):
            trail.add(str(title))
        return trail

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_section_label"] = _("Администрирование")
        return context


# ---------------------------------------------------------------------------
# 1. Сводка состояния системы
# ---------------------------------------------------------------------------
class AdminIndexView(AdminBaseMixin, TemplateView):
    """Сводка состояния системы."""

    template_name = "administration/index.html"
    page_title = _("Панель администратора")
    page_subtitle = _("Состояние системы, накопленные данные и требующие внимания разделы")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        week_ago = timezone.now() - dt.timedelta(days=7)

        users = User.objects.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(is_active=True)),
            blocked=Count("id", filter=Q(is_active=False)),
            admins=Count("id", filter=Q(role=Role.ADMIN)),
            without_password_change=Count("id", filter=Q(must_change_password=True)),
        )

        context["user_stats"] = users
        context["employee_total"] = Employee.objects.count()
        context["employee_active"] = Employee.objects.active().count()
        context["reserve_total"] = ReserveEntry.objects.in_reserve().count()
        context["pending_nominations"] = ReserveNomination.objects.pending().count()

        # Разделы, требующие внимания. Каждый пункт — не просто число, а
        # ссылка на список записей, с которыми нужно что-то сделать.
        context["attention"] = [
            {
                "label": _("Должности без профиля требований"),
                "count": Position.objects.filter(profile__isnull=True, is_active=True).count(),
                "url": reverse("administration:org"),
                "hint": _("Без профиля соответствие кандидата должности не рассчитывается"),
            },
            {
                "label": _("Компетенции с неполной шкалой"),
                # Подсчёт выполняется одним запросом с группировкой, а не
                # перебором справочника: обращение к свойству каждой записи
                # дало бы по запросу на компетенцию.
                "count": (
                    Competency.objects.active()
                    .annotate(levels=Count("scale_levels", distinct=True))
                    .exclude(levels=Competency.expected_scale_size())
                    .count()
                ),
                "url": reverse("administration:references"),
                "hint": _("Оценка по компетенции без описанных уровней невоспроизводима"),
            },
            {
                "label": _("Схемы оценивания, не готовые к расчёту"),
                "count": sum(1 for scheme in ScoringScheme.objects.all() if not scheme.is_ready),
                "url": reverse("administration:scoring"),
                "hint": _("Веса не образуют распределение либо суждения несогласованны"),
            },
            {
                "label": _("Необработанные обращения"),
                "count": Feedback.objects.filter(status=Feedback.Status.NEW).count(),
                "url": reverse("administration:feedback"),
                "hint": _("Обращения с формы обратной связи ожидают ответа"),
            },
            {
                "label": _("Учётные записи без смены выданного пароля"),
                "count": users["without_password_change"],
                "url": reverse("administration:users"),
                "hint": _("Пароль выдан администратором и до сих пор не изменён"),
            },
        ]

        context["recent_events"] = AuditLog.objects.select_related("actor").order_by("-created_at")[
            :12
        ]
        context["recent_logins"] = LoginRecord.objects.select_related("user").order_by(
            "-created_at"
        )[:8]
        context["failed_logins_week"] = (
            LoginRecord.objects.filter(created_at__gte=week_ago)
            .exclude(outcome=LoginRecord.Outcome.SUCCESS)
            .count()
        )
        context["today"] = today

        return context


# ---------------------------------------------------------------------------
# 2. Учётные записи и роли
# ---------------------------------------------------------------------------
class UserListView(AdminBaseMixin, FilterMixin, SortableMixin, WindowedPaginationMixin, ListView):
    """Список учётных записей с отбором по роли, филиалу и состоянию."""

    template_name = "administration/users.html"
    model = User
    context_object_name = "users"
    paginate_by = ADMIN_PAGE_SIZE
    filter_form_class = UserFilterForm
    page_title = _("Пользователи и роли")
    page_subtitle = _("Учётные записи, роли и области видимости")

    sort_fields = {
        "name": "last_name",
        "email": "email",
        "role": "role",
        "activity": "last_activity_at",
    }
    default_sort = "name"

    def get_queryset(self):
        queryset = User.objects.with_org().select_related("employee_card")
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return self.apply_sort(queryset)

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["role_counts"] = {
            item["role"]: item["total"]
            for item in User.objects.values("role").annotate(total=Count("id"))
        }
        context["roles"] = Role.choices
        return context


class UserDetailView(AdminBaseMixin, DetailView):
    """Карточка учётной записи с историей входов и последними действиями."""

    template_name = "administration/user_detail.html"
    model = User
    context_object_name = "account"

    def get_queryset(self):
        return User.objects.with_org().select_related("employee_card")

    def get_page_title(self) -> str:
        return self.object.full_name

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        account = self.object
        context["page_subtitle"] = f"{account.role_label} · {account.email}"
        context["login_records"] = account.login_records.order_by("-created_at")[:15]
        context["audit_records"] = AuditLog.objects.filter(actor=account).order_by("-created_at")[
            :15
        ]
        context["employee"] = getattr(account, "employee_card", None)
        return context

    def get_breadcrumbs(self, trail):
        trail.add(_("Администрирование"), "administration:index")
        trail.add(_("Пользователи и роли"), "administration:users")
        trail.add(self.object.short_name)
        return trail


class UserCreateView(AdminBaseMixin, CreateView):
    """Создание учётной записи."""

    template_name = "administration/user_form.html"
    model = User
    form_class = UserForm
    page_title = _("Новая учётная запись")
    page_subtitle = _("Пароль назначается отдельно и выдаётся одноразовым")

    def form_valid(self, form) -> HttpResponse:
        """Сохраняет запись с непригодным для входа паролем."""
        user = form.save(commit=False)
        user.set_unusable_password()
        user.must_change_password = True
        user.save()
        messages.success(
            self.request,
            _("Учётная запись создана. Пароль сотрудник задаёт через восстановление доступа."),
        )
        return redirect(user.get_absolute_url())


class UserUpdateView(AdminBaseMixin, UpdateView):
    """Изменение учётной записи."""

    template_name = "administration/user_form.html"
    model = User
    form_class = UserForm
    page_title = _("Изменение учётной записи")

    def get_page_subtitle(self) -> str:
        return self.object.email

    def form_valid(self, form) -> HttpResponse:
        response = super().form_valid(form)
        messages.success(self.request, _("Изменения сохранены"))
        return response

    def get_success_url(self) -> str:
        return self.object.get_absolute_url()


def toggle_user_active(request: HttpRequest, pk: int) -> HttpResponse:
    """Блокирует или разблокирует учётную запись."""
    if not request.user.is_authenticated or request.user.role != Role.ADMIN:
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied(_("Действие доступно только администратору"))

    account = get_object_or_404(User, pk=pk)

    if account.pk == request.user.pk:
        messages.error(request, _("Нельзя заблокировать собственную учётную запись"))
    else:
        account.is_active = not account.is_active
        account.save(update_fields=["is_active", "updated_at"])
        messages.success(
            request,
            _("Учётная запись разблокирована")
            if account.is_active
            else _("Учётная запись заблокирована"),
        )

    return redirect("administration:user_detail", pk=account.pk)


# ---------------------------------------------------------------------------
# 3. Организационная структура
# ---------------------------------------------------------------------------
class OrgStructureView(AdminBaseMixin, TemplateView):
    """Организационная структура: филиалы, дерево подразделений, должности."""

    template_name = "administration/org.html"
    page_title = _("Организационная структура")
    page_subtitle = _("Филиалы, подразделения, грейды и штатное расписание")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)

        context["branches"] = Branch.objects.annotate(
            department_count=Count("departments", distinct=True),
            employee_count=Count(
                "employees",
                filter=Q(employees__status__in=["active", "probation"]),
                distinct=True,
            ),
        )

        context["departments"] = (
            Department.objects.select_related("branch", "head")
            .annotate(
                employee_count=Count(
                    "employees",
                    filter=Q(employees__status__in=["active", "probation"]),
                    distinct=True,
                ),
                position_count=Count("positions", distinct=True),
            )
            .order_by("path")
        )

        positions = (
            Position.objects.select_related("grade", "department", "profile")
            .annotate(
                employee_count=Count(
                    "employees",
                    filter=Q(employees__status__in=["active", "probation"]),
                    distinct=True,
                )
            )
            .order_by("-grade__level", "name")
        )
        context["positions"] = positions
        context["grades"] = PositionGrade.objects.annotate(
            position_count=Count("positions", distinct=True)
        )

        context["headcount_plan"] = sum(item.headcount_plan for item in positions)
        context["headcount_fact"] = sum(item.employee_count for item in positions)
        context["profiles_defined"] = PositionProfile.objects.count()
        context["positions_without_profile"] = positions.filter(profile__isnull=True).count()

        return context


class BranchDetailView(AdminBaseMixin, DetailView):
    """Карточка филиала с составом подразделений."""

    template_name = "administration/branch_detail.html"
    model = Branch
    context_object_name = "branch"

    def get_page_title(self) -> str:
        return self.object.name

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_subtitle"] = self.object.city
        context["departments"] = (
            self.object.departments.select_related("head")
            .annotate(employee_count=Count("employees", distinct=True))
            .order_by("path")
        )
        context["employee_count"] = self.object.employees.active().count()
        return context


class DepartmentDetailView(AdminBaseMixin, DetailView):
    """Карточка подразделения с иерархией и штатом."""

    template_name = "administration/department_detail.html"
    model = Department
    context_object_name = "department"

    def get_queryset(self):
        return Department.objects.select_related("branch", "head", "parent")

    def get_page_title(self) -> str:
        return self.object.name

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        department = self.object
        context["page_subtitle"] = department.full_path_name
        context["children"] = department.children.order_by("name")
        context["positions"] = department.positions.select_related("grade").order_by(
            "-grade__level"
        )
        context["employees"] = (
            Employee.objects.with_org().filter(department=department).active()[:50]
        )
        context["employee_count"] = Employee.objects.filter(department=department).active().count()
        return context


class PositionDetailView(AdminBaseMixin, DetailView):
    """Карточка должности с профилем требований и занятыми ставками."""

    template_name = "administration/position_detail.html"
    model = Position
    context_object_name = "position"

    def get_queryset(self):
        return Position.objects.select_related("grade", "department", "profile")

    def get_page_title(self) -> str:
        return self.object.name

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        position = self.object
        context["page_subtitle"] = str(position.grade)
        context["profile"] = getattr(position, "profile", None)
        context["employees"] = Employee.objects.with_org().filter(position=position).active()
        context["reserve_entries"] = (
            ReserveEntry.objects.with_related().for_position(position.pk).in_reserve()
        )
        context["competency_models"] = position.competency_models.all()
        return context


# ---------------------------------------------------------------------------
# 4. Справочники
# ---------------------------------------------------------------------------
class ReferencesView(AdminBaseMixin, TemplateView):
    """Состояние справочников предметной области."""

    template_name = "administration/references.html"
    page_title = _("Справочники")
    page_subtitle = _("Компетенции, модели, группы резерва и учебные ресурсы")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)

        competencies = list(
            Competency.objects.with_category().prefetch_related("scale_levels").order_by("name")
        )
        context["categories"] = CompetencyCategory.objects.annotate(
            competency_count=Count("competencies", distinct=True)
        )
        context["competencies"] = competencies
        context["competencies_incomplete"] = [
            item for item in competencies if not item.has_full_scale
        ]

        models = list(CompetencyModel.objects.with_requirements())
        context["competency_models"] = models
        context["models_unbalanced"] = [item for item in models if not item.is_weight_balanced]

        context["reserve_groups"] = ReserveGroup.objects.annotate(
            entry_count=Count("entries", distinct=True)
        )
        context["learning_resources"] = LearningResource.objects.all()[:50]
        context["learning_total"] = LearningResource.objects.count()
        context["grades"] = PositionGrade.objects.all()

        return context


# ---------------------------------------------------------------------------
# 5. Модель скоринга
# ---------------------------------------------------------------------------
class ScoringView(AdminBaseMixin, TemplateView):
    """Настройка расчёта готовности: критерии и схемы весов."""

    template_name = "administration/scoring.html"
    page_title = _("Модель скоринга")
    page_subtitle = _("Критерии готовности, схемы весов и параметры расчёта")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        from django.conf import settings

        context["criteria"] = ScoringCriterion.objects.all()
        context["schemes"] = ScoringScheme.objects.with_weights()
        context["default_scheme"] = ScoringScheme.objects.default()
        options = settings.RESERVIX
        scale = f"{options['COMPETENCY_SCALE_MIN']}–{options['COMPETENCY_SCALE_MAX']}"
        context["settings_values"] = [
            (_("Порог «готов к назначению»"), options["READINESS_THRESHOLD_HIGH"]),
            (_("Порог «готов в течение года»"), options["READINESS_THRESHOLD_MEDIUM"]),
            (_("Граница низкой зоны матрицы 9-box"), options["NINE_BOX_LOW_MAX"]),
            (_("Граница средней зоны матрицы 9-box"), options["NINE_BOX_MID_MAX"]),
            (
                _("Предельное отношение согласованности (метод анализа иерархий)"),
                options["AHP_MAX_CONSISTENCY_RATIO"],
            ),
            (
                _("Горизонт планирования преемственности, месяцев"),
                options["SUCCESSION_HORIZON_MONTHS"],
            ),
            (_("Шкала оценки компетенций"), scale),
        ]
        return context


# ---------------------------------------------------------------------------
# 6. Журнал аудита
# ---------------------------------------------------------------------------
class AuditLogView(AdminBaseMixin, WindowedPaginationMixin, ListView):
    """Журнал действий пользователей."""

    template_name = "administration/audit.html"
    model = AuditLog
    context_object_name = "records"
    paginate_by = ADMIN_PAGE_SIZE
    page_title = _("Журнал аудита")
    page_subtitle = _("Кто, что и когда изменил в системе")

    def get_queryset(self):
        queryset = AuditLog.objects.select_related("actor", "content_type").order_by("-created_at")

        if action := self.request.GET.get("action"):
            queryset = queryset.filter(action=action)
        if severity := self.request.GET.get("severity"):
            queryset = queryset.filter(severity=severity)
        if actor := self.request.GET.get("actor"):
            queryset = queryset.filter(actor_id=actor)
        if term := self.request.GET.get("q"):
            queryset = queryset.filter(
                Q(object_repr__icontains=term)
                | Q(message__icontains=term)
                | Q(actor_repr__icontains=term)
            )
        # Дата в параметре адреса приходит от пользователя и может быть искажена: некорректное
        # значение отбрасывается, а список остаётся работоспособным — отказ страницы из-за опечатки
        # в адресе избыточен.
        if since := self.request.GET.get("since"):
            with contextlib.suppress(ValueError):
                queryset = queryset.filter(created_at__date__gte=dt.date.fromisoformat(since))

        return queryset

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        from apps.audit.models import AuditAction, AuditSeverity

        context["actions"] = AuditAction.choices
        context["severities"] = AuditSeverity.choices
        context["total_records"] = AuditLog.objects.count()
        context["actors"] = (
            User.objects.filter(audit_records__isnull=False).distinct().order_by("last_name")[:50]
        )
        context["current_filters"] = {
            key: value for key, value in self.request.GET.items() if key != "page"
        }
        params = self.request.GET.copy()
        params.pop("page", None)
        context["querystring"] = params.urlencode()
        return context


# ---------------------------------------------------------------------------
# 7. Данные системы
# ---------------------------------------------------------------------------
class SystemDataView(AdminBaseMixin, TemplateView):
    """Состав накопленных данных."""

    template_name = "administration/data.html"
    page_title = _("Данные системы")
    page_subtitle = _("Объём накопленных сведений по разделам предметной области")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)

        context["sections"] = [
            {
                "title": _("Организационная структура"),
                "rows": [
                    (_("Филиалы"), Branch.objects.count(), None),
                    (_("Подразделения"), Department.objects.count(), None),
                    (_("Грейды"), PositionGrade.objects.count(), None),
                    (_("Должности"), Position.objects.count(), None),
                    (_("Профили должностей"), PositionProfile.objects.count(), None),
                ],
            },
            {
                "title": _("Люди и доступ"),
                "rows": [
                    (_("Учётные записи"), User.objects.count(), None),
                    (
                        _("Карточки сотрудников"),
                        Employee.objects.count(),
                        Employee.objects.filter(legacy_id__isnull=False).count(),
                    ),
                    (_("Записи кадровой истории"), EmploymentHistory.objects.count(), None),
                    (_("Записи о входах"), LoginRecord.objects.count(), None),
                ],
            },
            {
                "title": _("Компетенции и оценка"),
                "rows": [
                    (_("Категории компетенций"), CompetencyCategory.objects.count(), None),
                    (_("Компетенции"), Competency.objects.count(), None),
                    (_("Модели компетенций"), CompetencyModel.objects.count(), None),
                    (_("Кампании оценки"), AssessmentCampaign.objects.count(), None),
                    (_("Сессии оценки"), AssessmentSession.objects.count(), None),
                    (
                        _("Оценки по компетенциям"),
                        AssessmentScore.objects.count(),
                        AssessmentScore.objects.filter(legacy_id__isnull=False).count(),
                    ),
                ],
            },
            {
                "title": _("Резерв и преемственность"),
                "rows": [
                    (_("Группы резерва"), ReserveGroup.objects.count(), None),
                    (
                        _("Записи резерва"),
                        ReserveEntry.objects.count(),
                        ReserveEntry.objects.filter(legacy_id__isnull=False).count(),
                    ),
                    (_("Заявки на выдвижение"), ReserveNomination.objects.count(), None),
                    (_("Критичные должности"), KeyPosition.objects.count(), None),
                    (_("Планы преемственности"), SuccessionPlan.objects.count(), None),
                    (_("Назначения"), Appointment.objects.count(), None),
                ],
            },
            {
                "title": _("Развитие"),
                "rows": [
                    (
                        _("Планы развития"),
                        DevelopmentPlan.objects.count(),
                        DevelopmentPlan.objects.filter(legacy_id__isnull=False).count(),
                    ),
                    (
                        _("Мероприятия развития"),
                        DevelopmentActivity.objects.count(),
                        DevelopmentActivity.objects.filter(legacy_id__isnull=False).count(),
                    ),
                    (_("Наставнические пары"), Mentorship.objects.count(), None),
                    (_("Учебные ресурсы"), LearningResource.objects.count(), None),
                ],
            },
            {
                "title": _("Публичная часть и служебные данные"),
                "rows": [
                    (_("Страницы"), Page.objects.count(), None),
                    (_("Статьи"), Article.objects.count(), None),
                    (_("Термины глоссария"), GlossaryTerm.objects.count(), None),
                    (_("Вопросы и ответы"), FaqItem.objects.count(), None),
                    (_("Обращения"), Feedback.objects.count(), None),
                    (_("Записи журнала аудита"), AuditLog.objects.count(), None),
                ],
            },
        ]

        context["total_records"] = sum(
            row[1] for section in context["sections"] for row in section["rows"]
        )
        context["legacy_records"] = sum(
            row[2] or 0 for section in context["sections"] for row in section["rows"]
        )
        context["employee_average_tenure"] = Employee.objects.active().aggregate(
            mean=Avg("prior_experience_months")
        )["mean"]

        return context


# ---------------------------------------------------------------------------
# 8. Содержимое публичной части
# ---------------------------------------------------------------------------
class ContentView(AdminBaseMixin, TemplateView):
    """Содержимое публичной части: страницы, статьи, глоссарий, вопросы и ответы."""

    template_name = "administration/content.html"
    page_title = _("Контент портала")
    page_subtitle = _("Страницы, статьи, глоссарий и раздел вопросов")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)

        context["pages"] = Page.objects.order_by("title")
        context["articles"] = Article.objects.select_related("category", "author").order_by(
            "-published_at"
        )[:40]
        context["article_total"] = Article.objects.count()
        context["article_drafts"] = Article.objects.filter(is_published=False).count()
        context["terms"] = GlossaryTerm.objects.order_by("term")
        context["faq_items"] = FaqItem.objects.select_related("category").order_by(
            "category__sort_order", "sort_order"
        )
        context["unpublished"] = {
            "pages": Page.objects.filter(is_published=False).count(),
            "articles": Article.objects.filter(is_published=False).count(),
            "terms": GlossaryTerm.objects.filter(is_published=False).count(),
            "faq": FaqItem.objects.filter(is_published=False).count(),
        }
        return context


# ---------------------------------------------------------------------------
# 9. Обращения с формы обратной связи
# ---------------------------------------------------------------------------
class FeedbackListView(AdminBaseMixin, WindowedPaginationMixin, ListView):
    """Обращения посетителей публичной части сайта."""

    template_name = "administration/feedback.html"
    model = Feedback
    context_object_name = "items"
    paginate_by = ADMIN_PAGE_SIZE
    page_title = _("Обратная связь")
    page_subtitle = _("Обращения с публичной части сайта и ответы на них")

    def get_queryset(self):
        queryset = Feedback.objects.select_related("assigned_to", "answered_by").order_by(
            "-created_at"
        )
        if status := self.request.GET.get("status"):
            queryset = queryset.filter(status=status)
        if topic := self.request.GET.get("topic"):
            queryset = queryset.filter(topic=topic)
        return queryset

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = Feedback.Status.choices
        context["topics"] = Feedback.Topic.choices
        context["status_counts"] = {
            item["status"]: item["total"]
            for item in Feedback.objects.values("status").annotate(total=Count("id"))
        }
        return context


class FeedbackDetailView(AdminBaseMixin, UpdateView):
    """Обращение с формой ответа."""

    template_name = "administration/feedback_detail.html"
    model = Feedback
    form_class = FeedbackAnswerForm
    context_object_name = "item"
    success_url = reverse_lazy("administration:feedback")

    def get_page_title(self) -> str:
        return self.object.subject

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_subtitle"] = f"{self.object.name} · {self.object.get_topic_display()}"
        return context

    def form_valid(self, form) -> HttpResponse:
        """Фиксирует автора и момент ответа."""
        feedback = form.save(commit=False)
        if feedback.status == Feedback.Status.ANSWERED and feedback.answered_at is None:
            feedback.answered_at = timezone.now()
            feedback.answered_by = self.request.user
        if feedback.assigned_to is None:
            feedback.assigned_to = self.request.user
        feedback.save()
        messages.success(self.request, _("Обращение сохранено"))
        return redirect(self.success_url)

    def get_breadcrumbs(self, trail):
        trail.add(_("Администрирование"), "administration:index")
        trail.add(_("Обратная связь"), "administration:feedback")
        trail.add(self.object.subject[:60])
        return trail
