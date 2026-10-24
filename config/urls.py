"""Корневая схема маршрутов."""

from __future__ import annotations

from django.conf import settings
from django.conf.urls.i18n import i18n_patterns
from django.conf.urls.static import static
from django.urls import include, path
from django.views.generic import RedirectView

from apps.api.api import api
from core.views import health_check, set_language

# ---------------------------------------------------------------------------
# Маршруты вне языковых префиксов
# ---------------------------------------------------------------------------
# Служебные адреса не должны зависеть от языка интерфейса: проверка состояния опрашивается
# контейнерным окружением, переключение языка обрабатывает Django, а файлы отдаются одинаково при
# любой локали.
urlpatterns = [
    path("health/", health_check, name="health"),
    # Переключатель языка — собственный: штатный не переводит адрес возврата
    # при обратном переходе. Подробности в core.views.set_language.
    path("i18n/setlang/", set_language, name="set_language"),
    path("favicon.ico", RedirectView.as_view(url=f"{settings.STATIC_URL}img/favicon.svg")),
    # Программный интерфейс вынесен за языковые префиксы: потребитель получает данные, а не
    # оформленные страницы, и язык интерфейса на состав ответа не влияет.
    path("api/v1/", api.urls),
]

# ---------------------------------------------------------------------------
# Локализованные маршруты
# ---------------------------------------------------------------------------
# prefix_default_language=False оставляет русскую версию по адресам без
# префикса: система внедряется в российской организации, и адрес вида
# ``/ru/reserve/`` для основной аудитории избыточен.
urlpatterns += i18n_patterns(
    path("account/", include("apps.accounts.urls", namespace="accounts")),
    path("manage/", include("apps.administration.urls", namespace="administration")),
    # Рабочие места ролей собраны под общим префиксом: он объявлен в описании адресного
    # пространства выше и позволяет обратному прокси-серверу применять ограничения ко всему разделу
    # сразу.
    path("workspace/", include("apps.dashboard.urls", namespace="dashboard")),
    path("workspace/people/", include("apps.people.urls", namespace="people")),
    path(
        "workspace/competencies/",
        include("apps.competencies.urls", namespace="competencies"),
    ),
    path("workspace/assessment/", include("apps.assessment.urls", namespace="assessment")),
    path("workspace/reserve/", include("apps.reserve.urls", namespace="reserve")),
    path("workspace/succession/", include("apps.succession.urls", namespace="succession")),
    path("workspace/analytics/", include("apps.analytics.urls", namespace="analytics")),
    path("workspace/development/", include("apps.development.urls", namespace="development")),
    path("workspace/documents/", include("apps.documents.urls", namespace="documents")),
    path("workspace/reports/", include("apps.reporting.urls", namespace="reporting")),
    path("workspace/notifications/", include("apps.notifications.urls", namespace="notifications")),
    path("", include("apps.portal.urls", namespace="portal")),
    prefix_default_language=False,
)

# ---------------------------------------------------------------------------
# Обработчики ошибок
# ---------------------------------------------------------------------------
handler400 = "core.views.bad_request"
handler403 = "core.views.permission_denied"
handler404 = "core.views.page_not_found"
handler500 = "core.views.server_error"

# ---------------------------------------------------------------------------
# Разработка
# ---------------------------------------------------------------------------
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATICFILES_DIRS[0])
