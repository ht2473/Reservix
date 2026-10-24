"""Проверка раздела уведомлений."""

from __future__ import annotations

import pytest

from django.urls import reverse

from apps.notifications.models import Notification, NotificationKind
from apps.notifications.services import mark_all_read, notify, unread_count
from tests.factories import NotificationFactory

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def inbox(employee_user):
    """Три уведомления сотрудника: два непрочитанных и одно требующее действия."""
    return [
        NotificationFactory(recipient=employee_user, kind=NotificationKind.RESERVE_INCLUDED),
        NotificationFactory(recipient=employee_user, kind=NotificationKind.ACTIVITY_DUE_SOON),
        NotificationFactory(recipient=employee_user, kind=NotificationKind.SYSTEM),
    ]


class TestInboxIsolation:
    """Чужие уведомления недоступны никому."""

    def test_лента_показывает_только_свои_сообщения(self, auth_client, inbox, hr_user):
        NotificationFactory(recipient=hr_user, title="Сообщение кадровой службе")

        response = auth_client.get(reverse("notifications:list"))
        assert response.status_code == 200
        assert "Сообщение кадровой службе" not in response.content.decode()
        assert len(response.context["notifications"]) == 3

    def test_администратор_не_читает_чужую_ленту(self, admin_client, inbox):
        """Уведомление адресовано человеку: привилегированная роль права на него не даёт."""
        response = admin_client.post(reverse("notifications:open", args=[inbox[0].pk]))
        assert response.status_code == 404


class TestReadState:
    """Отметка о прочтении."""

    def test_по_умолчанию_показываются_непрочитанные(self, auth_client, inbox):
        inbox[0].mark_read()

        response = auth_client.get(reverse("notifications:list"))
        assert len(response.context["notifications"]) == 2

    def test_отбор_показывает_все(self, auth_client, inbox):
        inbox[0].mark_read()

        response = auth_client.get(reverse("notifications:list"), {"state": "all"})
        assert len(response.context["notifications"]) == 3

    def test_отбор_требующих_действия(self, auth_client, inbox):
        response = auth_client.get(reverse("notifications:list"), {"state": "actionable"})
        titles = {item.title for item in response.context["notifications"]}
        assert titles == {inbox[1].title}

    def test_переход_по_ссылке_отмечает_прочитанным(self, auth_client, inbox):
        notification = inbox[0]
        notification.url = "/workspace/reserve/"
        notification.save(update_fields=["url"])

        response = auth_client.post(reverse("notifications:open", args=[notification.pk]))
        assert response.status_code == 302
        assert response.url == "/workspace/reserve/"

        notification.refresh_from_db()
        assert notification.is_read is True

    def test_переход_по_get_ничего_не_меняет(self, auth_client, inbox):
        """
        Изменение состояния по GET срабатывает от предварительной загрузки ссылок браузером и от
        обходчика поисковой системы.
        """
        notification = inbox[0]
        response = auth_client.get(reverse("notifications:open", args=[notification.pk]))
        assert response.status_code == 302

        notification.refresh_from_db()
        assert notification.is_read is False

    def test_уведомление_без_адреса_возвращает_в_ленту(self, auth_client, inbox):
        notification = inbox[2]
        notification.url = ""
        notification.save(update_fields=["url"])

        response = auth_client.post(reverse("notifications:open", args=[notification.pk]))
        assert response.url == reverse("notifications:list")

    def test_массовая_отметка_прочитанным(self, auth_client, employee_user, inbox):
        response = auth_client.post(reverse("notifications:mark_all_read"))
        assert response.status_code == 302
        assert unread_count(employee_user) == 0

    def test_повторная_массовая_отметка_ничего_не_меняет(self, employee_user, inbox):
        assert mark_all_read(employee_user) == 3
        assert mark_all_read(employee_user) == 0


class TestSummary:
    """Сводка по направлениям."""

    def test_счётчики_разнесены_по_направлениям(self, auth_client, employee_user):
        NotificationFactory(recipient=employee_user, kind=NotificationKind.RESERVE_INCLUDED)
        NotificationFactory(recipient=employee_user, kind=NotificationKind.PLAN_ASSIGNED)
        NotificationFactory(recipient=employee_user, kind=NotificationKind.ACTIVITY_OVERDUE)
        NotificationFactory(recipient=employee_user, kind=NotificationKind.CAMPAIGN_STARTED)

        summary = auth_client.get(reverse("notifications:list")).context["summary"]
        assert summary["unread"] == 4
        assert summary["reserve_unread"] == 1
        assert summary["development_unread"] == 2
        assert summary["assessment_unread"] == 1


class TestDeduplication:
    """Подавление повторов."""

    def test_повтор_по_тому_же_объекту_подавляется(self, employee_user, hr_user):
        """
        Пересчёт индексов способен за минуту сгенерировать десятки одинаковых сообщений об одном и
        том же событии.
        """
        notify(
            employee_user,
            NotificationKind.READINESS_CHANGED,
            "Изменился уровень готовности",
            obj=hr_user,
        )
        second = notify(
            employee_user,
            NotificationKind.READINESS_CHANGED,
            "Изменился уровень готовности",
            obj=hr_user,
        )
        assert second is None
        assert Notification.objects.for_user(employee_user).count() == 1

    def test_собственное_действие_не_порождает_уведомления(self, employee_user):
        result = notify(
            employee_user,
            NotificationKind.SYSTEM,
            "Вы изменили запись",
            actor=employee_user,
        )
        assert result is None
