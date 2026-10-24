"""Проверка файлового хранилища: приём, разграничение доступа, выдача."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.documents.models import AccessLevel, Document, DocumentKind
from apps.documents.services import (
    DocumentError,
    can_access,
    documents_for,
    storage_statistics,
    store_document,
    supersede,
    verify_integrity,
    visible_documents,
)
from tests.factories.domain import EmployeeFactory

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


def _upload(name: str = "приказ.pdf", content: bytes = b"%PDF-1.4 test") -> SimpleUploadedFile:
    """Загружаемый файл с русским именем: имена документов кириллические."""
    return SimpleUploadedFile(name, content, content_type="application/pdf")


@pytest.fixture
def employee(department):
    return EmployeeFactory(department=department)


@pytest.fixture
def document(hr_user, employee):
    """Документ ограниченного доступа, привязанный к сотруднику."""
    return store_document(
        _upload(),
        title="Приказ о зачислении в резерв",
        uploader=hr_user,
        kind=DocumentKind.ORDER,
        employee=employee,
        access_level=AccessLevel.RESTRICTED,
    )


# ---------------------------------------------------------------------------
# Приём
# ---------------------------------------------------------------------------
class TestStorage:
    """Приём файла в хранилище."""

    def test_имя_на_диске_обезличено(self, document: Document):
        """
        Файл «Иванов_приказ_об_увольнении.pdf» раскрывал бы персональные данные любому, кто получил
        доступ к каталогу хранения.
        """
        assert "приказ" not in document.file.name
        assert document.file.name.endswith(".pdf")
        assert document.original_name == "приказ.pdf"

    def test_контрольная_сумма_вычисляется_при_приёме(self, document: Document):
        assert len(document.checksum) == 64
        assert verify_integrity(document) is True

    def test_подмена_файла_обнаруживается(self, document: Document):
        """
        Хранилище обязано отвечать не только на вопрос «что лежит», но и на вопрос «то ли лежит,
        что положили».
        """
        Path(document.file.path).write_bytes("%PDF-1.4 подменено".encode())

        assert verify_integrity(document) is False

    def test_замещение_сохраняет_прежнюю_версию(self, document: Document, hr_user, employee):
        replacement = store_document(
            _upload("приказ-новый.pdf"),
            title="Приказ о зачислении в резерв (новая редакция)",
            uploader=hr_user,
            employee=employee,
        )
        supersede(document, replacement)

        document.refresh_from_db()
        assert document.superseded_by == replacement
        assert document.is_current is False
        assert Document.objects.current_versions().count() == 1
        # Прежняя редакция остаётся в базе: она могла быть основанием уже
        # принятых решений.
        assert Document.objects.count() == 2

    def test_повторное_замещение_отклоняется(self, document: Document, hr_user):
        first = store_document(_upload("а.pdf"), title="Первая замена", uploader=hr_user)
        second = store_document(_upload("б.pdf"), title="Вторая замена", uploader=hr_user)

        supersede(document, first)
        with pytest.raises(DocumentError, match="уже замещён"):
            supersede(document, second)

    def test_документ_не_замещает_сам_себя(self, document: Document):
        with pytest.raises(DocumentError, match="сам себя"):
            supersede(document, document)


# ---------------------------------------------------------------------------
# Доступ
# ---------------------------------------------------------------------------
class TestAccess:
    """Право на документ выводится из права на сотрудника."""

    def test_кадровая_служба_читает_любой_документ(self, document, hr_user, admin_user):
        assert can_access(hr_user, document) is True
        assert can_access(admin_user, document) is True

    def test_руководитель_читает_документы_подчинённых(self, document, manager_user):
        assert can_access(manager_user, document) is True

    def test_посторонний_не_читает_ограниченный_документ(
        self, hr_user, other_department, employee_user
    ):
        foreign = store_document(
            _upload(),
            title="Протокол оценки",
            uploader=hr_user,
            employee=EmployeeFactory(department=other_department),
            access_level=AccessLevel.RESTRICTED,
        )
        assert can_access(employee_user, foreign) is False

    def test_сотрудник_читает_документ_о_себе(self, hr_user, employee_user):
        own = store_document(
            _upload(),
            title="Сертификат",
            uploader=hr_user,
            employee=EmployeeFactory(user=employee_user),
            access_level=AccessLevel.RESTRICTED,
        )
        assert can_access(employee_user, own) is True

    def test_конфиденциальный_документ_закрыт_от_самого_сотрудника(self, hr_user, employee_user):
        """
        Заключение кадровой службы о готовности к назначению не предназначено для того, о ком
        составлено.
        """
        confidential = store_document(
            _upload(),
            title="Заключение о готовности",
            uploader=hr_user,
            employee=EmployeeFactory(user=employee_user),
            access_level=AccessLevel.CONFIDENTIAL,
        )
        assert can_access(employee_user, confidential) is False

    def test_открытый_документ_доступен_всем_сотрудникам(self, hr_user, employee_user):
        public = store_document(
            _upload(),
            title="Положение о кадровом резерве",
            uploader=hr_user,
            access_level=AccessLevel.PUBLIC,
        )
        assert can_access(employee_user, public) is True

    def test_выборка_и_поштучная_проверка_согласованы(
        self, hr_user, manager_user, employee, other_department
    ):
        """Список строится запросом, а одиночная проверка — функцией."""
        store_document(_upload(), title="Свой", uploader=hr_user, employee=employee)
        store_document(
            _upload(),
            title="Чужой",
            uploader=hr_user,
            employee=EmployeeFactory(department=other_department),
        )
        store_document(
            _upload(), title="Открытый", uploader=hr_user, access_level=AccessLevel.PUBLIC
        )

        visible = list(visible_documents(manager_user))
        assert {item.title for item in visible} == {"Свой", "Открытый"}
        assert all(can_access(manager_user, item) for item in visible)

        for hidden in Document.objects.exclude(pk__in=[item.pk for item in visible]):
            assert can_access(manager_user, hidden) is False


# ---------------------------------------------------------------------------
# Страницы и выдача
# ---------------------------------------------------------------------------
class TestPages:
    """Страницы раздела и защищённая выдача файла."""

    def test_список_открывается_кадровой_службе(self, hr_client, document):
        response = hr_client.get(reverse("documents:list"))
        assert response.status_code == 200
        assert document.title in response.content.decode()

    def test_карточка_показывает_состояние_целостности(self, hr_client, document):
        response = hr_client.get(reverse("documents:detail", args=[document.public_id]))
        assert response.status_code == 200
        assert response.context["integrity_ok"] is True

    def test_выдача_отдаёт_файл_с_кириллическим_именем(self, hr_client, document):
        """
        Без кодирования по RFC 5987 браузер сохранил бы файл под искажённым именем либо отбросил
        заголовок целиком.
        """
        response = hr_client.get(reverse("documents:download", args=[document.public_id]))
        assert response.status_code == 200

        disposition = response["Content-Disposition"]
        assert disposition.startswith("attachment; filename*=UTF-8''")
        assert "%D0%BF%D1%80%D0%B8%D0%BA%D0%B0%D0%B7" in disposition
        assert response["Cache-Control"] == "private, no-store"

    def test_выдача_учитывается_счётчиком(self, hr_client, document):
        hr_client.get(reverse("documents:download", args=[document.public_id]))
        document.refresh_from_db()
        assert document.download_count == 1

    def test_посторонний_не_получает_файл(self, client, employee_user, hr_user, other_department):
        foreign = store_document(
            _upload(),
            title="Протокол",
            uploader=hr_user,
            employee=EmployeeFactory(department=other_department),
        )
        client.force_login(employee_user)
        response = client.get(reverse("documents:download", args=[foreign.public_id]))
        assert response.status_code == 403

    def test_загрузка_через_форму(self, hr_client, employee):
        response = hr_client.post(
            reverse("documents:upload"),
            {
                "file": _upload("протокол.pdf"),
                "title": "Протокол заседания комиссии",
                "kind": DocumentKind.PROTOCOL,
                "access_level": AccessLevel.RESTRICTED,
                "employee": employee.pk,
            },
        )
        assert response.status_code == 302

        stored = Document.objects.get(title="Протокол заседания комиссии")
        assert stored.checksum
        assert stored.employee == employee

    def test_удаление_мягкое(self, hr_client, document):
        """
        Документ мог быть основанием кадрового решения, и его бесследное исчезновение сделало бы
        решение необоснованным задним числом.
        """
        response = hr_client.post(reverse("documents:delete", args=[document.public_id]))
        assert response.status_code == 302

        assert Document.objects.filter(pk=document.pk).count() == 0
        assert Document.all_objects.filter(pk=document.pk).count() == 1


class TestStatistics:
    """Сводка по хранилищу."""

    def test_сводка_различает_замещённое_и_истёкшее(self, hr_user, employee):
        current = store_document(_upload(), title="Действующий", uploader=hr_user)
        expired = store_document(
            _upload(),
            title="Истёкший",
            uploader=hr_user,
            employee=employee,
            valid_until=timezone.localdate() - dt.timedelta(days=1),
        )
        replacement = store_document(_upload(), title="Замена", uploader=hr_user)
        supersede(current, replacement)

        stats = storage_statistics()
        assert stats.total == 3
        assert stats.superseded == 1
        assert stats.expired == 1
        assert stats.size_bytes == sum(item.size_bytes for item in (current, expired, replacement))

    def test_вложения_объекта_отбираются_по_доступу(self, hr_user, employee, employee_user):
        entry_like = employee  # объект, к которому прикрепляется документ
        store_document(
            _upload(),
            title="Открытое вложение",
            uploader=hr_user,
            obj=entry_like,
            access_level=AccessLevel.PUBLIC,
        )
        store_document(
            _upload(),
            title="Конфиденциальное вложение",
            uploader=hr_user,
            obj=entry_like,
            access_level=AccessLevel.CONFIDENTIAL,
        )

        assert len(documents_for(entry_like)) == 2
        titles = {item.title for item in documents_for(entry_like, user=employee_user)}
        assert titles == {"Открытое вложение"}
