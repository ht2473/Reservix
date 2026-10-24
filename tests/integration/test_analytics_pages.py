"""Проверка страниц аналитического ядра."""

from __future__ import annotations

from decimal import Decimal

import pytest

from django.urls import reverse

from apps.analytics.models import (
    CalibrationStatus,
    NineBoxPlacement,
    ReadinessSnapshot,
    ScoringScheme,
    WeightingMethod,
)
from apps.analytics.services import ensure_criteria, ensure_default_scheme
from tests.factories.domain import (
    CalibrationSessionFactory,
    EmployeeFactory,
    NineBoxPlacementFactory,
    ReserveEntryFactory,
    ScoringCriterionFactory,
    ScoringSchemeFactory,
    ScoringWeightFactory,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def methodology(db, hr_user):
    """Справочник критериев и схема по умолчанию."""
    ensure_criteria()
    return ensure_default_scheme(author=hr_user)


# ---------------------------------------------------------------------------
# Доступ
# ---------------------------------------------------------------------------
class TestAccess:
    """Разграничение доступа к разделу."""

    @pytest.mark.parametrize(
        "route",
        [
            "analytics:index",
            "analytics:ranking",
            "analytics:criteria",
            "analytics:schemes",
            "analytics:comparison",
            "analytics:stability",
            "analytics:ninebox",
            "analytics:placements",
            "analytics:calibrations",
        ],
    )
    def test_hr_sees_section(self, hr_client, methodology, route):
        """Кадровая служба ведёт методику и видит все страницы раздела."""
        assert hr_client.get(reverse(route)).status_code == 200

    def test_manager_sees_matrix(self, manager_client, methodology):
        """Руководителю доступна матрица: оценки в неё ставит он."""
        assert manager_client.get(reverse("analytics:ninebox")).status_code == 200

    def test_manager_cannot_change_methodology(self, manager_client, methodology):
        """Веса критериев меняет методолог, а не руководитель подразделения."""
        response = manager_client.get(reverse("analytics:scheme_weights", args=[methodology.pk]))

        assert response.status_code == 403

    @pytest.mark.parametrize("route", ["analytics:index", "analytics:schemes", "analytics:ninebox"])
    def test_employee_is_denied(self, auth_client, methodology, route):
        """Сотруднику раздел аналитики недоступен."""
        assert auth_client.get(reverse(route)).status_code == 403

    def test_anonymous_is_redirected(self, client, methodology):
        """Неавторизованный посетитель отправляется на вход, а не получает отказ."""
        response = client.get(reverse("analytics:index"))

        assert response.status_code == 302
        assert reverse("accounts:login") in response.url


# ---------------------------------------------------------------------------
# Отрисовка
# ---------------------------------------------------------------------------
class TestRendering:
    """Страницы открываются и на пустых, и на заполненных данных."""

    def test_overview_without_calculations(self, hr_client, methodology):
        """Обзор без расчётов объясняет причину пустоты."""
        response = hr_client.get(reverse("analytics:index"))

        assert response.status_code == 200
        assert "расчёт не запускался" in response.content.decode()

    def test_overview_with_data(self, hr_client, methodology, position, branch, department):
        """Обзор показывает показатели резерва и распределение индекса."""
        entry = ReserveEntryFactory(target_position=position, readiness_index=Decimal("72.00"))
        ReadinessSnapshot.objects.create(
            entry=entry,
            scheme=methodology,
            index_value=Decimal("72.00"),
            readiness_level="medium",
            criterion_values={"_meta": {"coverage": 0.8, "reliable": True}},
        )

        response = hr_client.get(reverse("analytics:index"))

        assert response.status_code == 200
        assert response.context["health"].entries == 1

    def test_criteria_page_flags_unimplemented_source(self, hr_client, methodology):
        """Критерий без функции расчёта помечается на странице."""
        ScoringCriterionFactory(code="ghost", name="Мнимый критерий", source="nowhere")

        response = hr_client.get(reverse("analytics:criteria"))

        assert response.status_code == 200
        assert "Нет функции расчёта" in response.content.decode()

    def test_scheme_detail_shows_pairwise_matrix(self, hr_client, methodology):
        """Для схемы метода анализа иерархий выводится исходная матрица суждений."""
        scheme = ScoringSchemeFactory(
            code="ahp-page",
            method=WeightingMethod.AHP,
            pairwise_matrix={
                "criteria": ["k1-competency"],
                "matrix": [[1.0]],
                "lambda_max": 1.0,
                "consistency_index": 0.0,
            },
        )
        ScoringWeightFactory(
            scheme=scheme,
            criterion=ScoringCriterionFactory(code="k1-competency"),
            weight=Decimal("1.00000"),
        )

        response = hr_client.get(reverse("analytics:scheme_detail", args=[scheme.pk]))

        assert response.status_code == 200
        assert response.context["matrix"] is not None

    def test_ninebox_page_reports_concentration(self, hr_client, methodology, branch, department):
        """Матрица показывает перекос распределения наравне с самим распределением."""
        for _ in range(3):
            NineBoxPlacementFactory(
                employee=EmployeeFactory(branch=branch, department=department),
                performance=Decimal("55"),
                potential=Decimal("55"),
            )

        response = hr_client.get(reverse("analytics:ninebox"))

        assert response.status_code == 200
        assert response.context["grid"].concentration == pytest.approx(100.0)

    def test_ranking_filters_by_scheme(self, hr_client, methodology, position):
        """Рейтинг показывается по выбранной схеме, а не по всем сразу."""
        other = ScoringSchemeFactory(code="other-scheme")
        entry = ReserveEntryFactory(target_position=position)
        ReadinessSnapshot.objects.create(
            entry=entry,
            scheme=other,
            index_value=Decimal("50.00"),
            readiness_level="low",
            criterion_values={"_meta": {"coverage": 1.0, "reliable": True}},
        )

        default_page = hr_client.get(reverse("analytics:ranking"))
        other_page = hr_client.get(reverse("analytics:ranking"), {"scheme": "other-scheme"})

        assert list(default_page.context["snapshots"]) == []
        assert len(other_page.context["snapshots"]) == 1

    def test_breakdown_page_explains_index(self, hr_client, methodology, position):
        """Разбор показывает вклад каждого критерия, а не только итог."""
        entry = ReserveEntryFactory(target_position=position)

        response = hr_client.get(reverse("analytics:breakdown", args=[entry.public_id]))

        assert response.status_code == 200
        assert len(response.context["breakdown"].contributions) == 6

    def test_calibration_detail_shows_shift(self, hr_client, methodology, branch, department):
        """Карточка сессии показывает, насколько калибровка изменила оценки."""
        session = CalibrationSessionFactory()
        NineBoxPlacementFactory(
            employee=EmployeeFactory(branch=branch, department=department),
            session=session,
            performance=Decimal("70"),
            potential=Decimal("70"),
            initial_performance=Decimal("80"),
            initial_potential=Decimal("80"),
            is_adjusted=True,
        )

        response = hr_client.get(reverse("analytics:calibration_detail", args=[session.public_id]))

        assert response.status_code == 200
        assert response.context["effect"]["performance_shift"] == pytest.approx(-10.0)


# ---------------------------------------------------------------------------
# Действия
# ---------------------------------------------------------------------------
class TestActions:
    """Операции, изменяющие методику и данные расчёта."""

    def test_recalculation_creates_snapshots(self, hr_client, methodology, position):
        """Пересчёт выполняется по явному действию и сохраняет расчёт."""
        entry = ReserveEntryFactory(target_position=position)

        response = hr_client.post(reverse("analytics:recalculate"), {"notify": ""})

        assert response.status_code == 302
        assert ReadinessSnapshot.objects.filter(entry=entry, is_current=True).count() == 1

    def test_page_view_does_not_recalculate(self, hr_client, methodology, position):
        """Открытие страницы расчёт не запускает."""
        ReserveEntryFactory(target_position=position)

        hr_client.get(reverse("analytics:index"))
        hr_client.get(reverse("analytics:ranking"))

        assert ReadinessSnapshot.objects.count() == 0

    def test_expert_weights_are_saved_normalised(self, hr_client, methodology):
        """Введённые баллы важности приводятся к сумме единицы."""
        criteria = [weight.criterion for weight in methodology.weights.all()]
        data = {f"weight_{criterion.pk}": 10 for criterion in criteria}

        response = hr_client.post(reverse("analytics:scheme_weights", args=[methodology.pk]), data)

        methodology.refresh_from_db()
        total = sum(float(weight.weight) for weight in methodology.weights.all())
        assert response.status_code == 302
        assert total == pytest.approx(1.0, abs=0.001)

    def test_inconsistent_judgements_are_kept_and_flagged(self, hr_client, hr_user):
        """Противоречивые суждения сохраняются, но схема помечается непригодной."""
        first = ScoringCriterionFactory(code="p1", source="competency_match", sort_order=1)
        second = ScoringCriterionFactory(code="p2", source="performance", sort_order=2)
        third = ScoringCriterionFactory(code="p3", source="experience", sort_order=3)
        scheme = ScoringSchemeFactory(code="ahp-form", method=WeightingMethod.AHP)

        response = hr_client.post(
            reverse("analytics:scheme_ahp", args=[scheme.pk]),
            {"pair_0_1": "9", "pair_0_2": "0.1111", "pair_1_2": "9", "note": "проверка"},
        )

        scheme.refresh_from_db()
        assert response.status_code == 200
        assert scheme.weights.count() == 3
        assert not scheme.is_consistent
        assert not scheme.is_ready
        assert {first.pk, second.pk, third.pk} == {
            weight.criterion_id for weight in scheme.weights.all()
        }

    def test_auto_placement_keeps_manual_decisions(
        self, hr_client, methodology, branch, department, manager_user
    ):
        """Предварительное размещение не отменяет решение руководителя."""
        employee = EmployeeFactory(branch=branch, department=department)
        NineBoxPlacementFactory(
            employee=employee,
            performance=Decimal("30"),
            potential=Decimal("30"),
            placed_by=manager_user,
        )

        hr_client.post(reverse("analytics:placement_auto"))

        current = NineBoxPlacement.objects.get(employee=employee, is_current=True)
        assert current.performance == pytest.approx(Decimal("30.00"))
        assert current.placed_by == manager_user

    def test_calibration_requires_substantial_reason(
        self, hr_client, methodology, branch, department
    ):
        """Короткое обоснование не принимается: оно ничего не объясняет."""
        placement = NineBoxPlacementFactory(
            employee=EmployeeFactory(branch=branch, department=department),
            performance=Decimal("80"),
            potential=Decimal("80"),
        )

        response = hr_client.post(
            reverse("analytics:calibrate", args=[placement.pk]),
            {"performance": "50", "potential": "50", "comment": "уточнено"},
        )

        placement.refresh_from_db()
        assert response.status_code == 200
        assert placement.performance == pytest.approx(Decimal("80.00"))

    def test_session_close_requires_protocol(self, hr_client, methodology):
        """Сессия без протокола неотличима от несостоявшейся."""
        session = CalibrationSessionFactory(status=CalibrationStatus.IN_PROGRESS)

        hr_client.post(
            reverse("analytics:calibration_close", args=[session.public_id]),
            {"decisions": "коротко"},
        )

        session.refresh_from_db()
        assert session.status == CalibrationStatus.IN_PROGRESS

    def test_criteria_sync_is_idempotent(self, hr_client, methodology):
        """Повторная сверка справочника не создаёт дубликатов."""
        hr_client.post(reverse("analytics:criteria_sync"))
        hr_client.post(reverse("analytics:criteria_sync"))

        from apps.analytics.models import ScoringCriterion

        assert ScoringCriterion.objects.count() == 6

    def test_scheme_creation(self, hr_client, methodology):
        """Схема создаётся без весов: способ их получения задаёт форму ввода."""
        response = hr_client.post(
            reverse("analytics:scheme_create"),
            {
                "name": "Новая схема",
                "code": "new-scheme",
                "description": "Проверочная",
                "method": WeightingMethod.EXPERT,
                "aggregation": "weighted_sum",
                "source_note": "",
            },
        )

        assert response.status_code == 302
        assert ScoringScheme.objects.filter(code="new-scheme").exists()
