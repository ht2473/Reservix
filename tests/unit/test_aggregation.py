"""Проверка сведения оценок в уровень владения."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from apps.assessment.aggregation import (
    DEFAULT_METHOD_WEIGHT,
    LOW_CONFIDENCE_THRESHOLD,
    METHOD_WEIGHTS,
    Observation,
    _window_start,
    consolidate,
)
from apps.assessment.models import AssessmentMethod

pytestmark = pytest.mark.unit

TODAY = dt.date(2026, 6, 15)


def observation(
    method: str,
    level: float,
    *,
    competency_id: int = 1,
    date: dt.date = TODAY,
    session_id: int = 1,
) -> Observation:
    """Создаёт наблюдение с разумными значениями по умолчанию."""
    return Observation(
        competency_id=competency_id,
        method=method,
        level=level,
        assessment_date=date,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Веса методов
# ---------------------------------------------------------------------------
class TestMethodWeights:
    """Соотношение весов методов — предметное решение, а не подбор чисел."""

    def test_проверяемые_методы_весят_больше_экспертных(self):
        """Тест и экзамен дают воспроизводимый результат, оценка — суждение."""
        assert METHOD_WEIGHTS[AssessmentMethod.TEST] > METHOD_WEIGHTS[AssessmentMethod.MANAGER]
        assert METHOD_WEIGHTS[AssessmentMethod.EXAM] > METHOD_WEIGHTS[AssessmentMethod.EXPERT]

    def test_самооценка_весит_меньше_любого_внешнего_метода(self):
        """Иначе сотрудник поднимает итог, поставив себе пятёрки."""
        own = METHOD_WEIGHTS[AssessmentMethod.SELF]
        external = [
            weight for method, weight in METHOD_WEIGHTS.items() if method != AssessmentMethod.SELF
        ]
        assert all(own < weight for weight in external)

    def test_неизвестный_метод_получает_наименьший_вес(self):
        """Способ получения балла, не описанный в системе, не может весить много."""
        unknown = observation("неизвестный-метод", 5.0)
        assert unknown.weight == DEFAULT_METHOD_WEIGHT
        assert min(METHOD_WEIGHTS.values()) >= DEFAULT_METHOD_WEIGHT


# ---------------------------------------------------------------------------
# Правила сведения
# ---------------------------------------------------------------------------
class TestConsolidate:
    """Сведение наблюдений в уровень владения."""

    def test_одно_наблюдение_даёт_свой_уровень(self):
        result = consolidate([observation(AssessmentMethod.MANAGER, 4.0)])
        assert result[1].level == 4.0

    def test_методы_сводятся_взвешенным_средним(self):
        """Проверяется само правило: итог смещён к более достоверному методу."""
        result = consolidate(
            [
                observation(AssessmentMethod.TEST, 5.0),
                observation(AssessmentMethod.SELF, 1.0, session_id=2),
            ]
        )
        level = result[1].level
        assert level > 3.0, "итог должен быть смещён к проверяемому методу"

        expected = float(
            (
                Decimal("5.0") * METHOD_WEIGHTS[AssessmentMethod.TEST]
                + Decimal("1.0") * METHOD_WEIGHTS[AssessmentMethod.SELF]
            )
            / (METHOD_WEIGHTS[AssessmentMethod.TEST] + METHOD_WEIGHTS[AssessmentMethod.SELF])
        )
        assert level == pytest.approx(expected, abs=0.01)

    def test_повторная_оценка_тем_же_методом_вытесняет_прежнюю(self):
        """Рост с тройки до четвёрки должен дать четвёрку, а не 3,5."""
        result = consolidate(
            [
                observation(AssessmentMethod.MANAGER, 3.0, date=dt.date(2025, 6, 1)),
                observation(AssessmentMethod.MANAGER, 4.0, date=dt.date(2026, 6, 1), session_id=2),
            ]
        )
        assert result[1].level == 4.0
        assert len(result[1].observations) == 1

    def test_нормировка_идёт_по_присутствующим_методам(self):
        """Отсутствие остальных методов не занижает уровень."""
        result = consolidate([observation(AssessmentMethod.MANAGER, 4.0)])
        assert result[1].level == 4.0

    def test_разные_компетенции_сводятся_независимо(self):
        result = consolidate(
            [
                observation(AssessmentMethod.MANAGER, 2.0, competency_id=1),
                observation(AssessmentMethod.MANAGER, 5.0, competency_id=2, session_id=2),
            ]
        )
        assert result[1].level == 2.0
        assert result[2].level == 5.0

    def test_пустой_ввод_даёт_пустой_результат(self):
        assert consolidate([]) == {}


# ---------------------------------------------------------------------------
# Достоверность
# ---------------------------------------------------------------------------
class TestConfidence:
    """Достоверность — доля веса фактически применённых методов."""

    def test_одна_самооценка_даёт_низкую_достоверность(self):
        """
        Уровень 5,0 из одной самооценки не равен такому же уровню из трёх методов, и интерфейс
        обязан их различать.
        """
        result = consolidate([observation(AssessmentMethod.SELF, 5.0)])
        assert result[1].is_low_confidence
        assert result[1].confidence < LOW_CONFIDENCE_THRESHOLD

    def test_несколько_методов_повышают_достоверность(self):
        one = consolidate([observation(AssessmentMethod.MANAGER, 4.0)])[1]
        many = consolidate(
            [
                observation(AssessmentMethod.MANAGER, 4.0),
                observation(AssessmentMethod.TEST, 4.0, session_id=2),
                observation(AssessmentMethod.EXPERT, 4.0, session_id=3),
            ]
        )[1]
        assert many.confidence > one.confidence
        assert many.method_count == 3

    def test_достоверность_не_превышает_единицы(self):
        result = consolidate(
            [
                observation(method, 4.0, session_id=index)
                for index, method in enumerate(METHOD_WEIGHTS)
            ]
        )
        assert result[1].confidence <= 1.0


# ---------------------------------------------------------------------------
# Расхождение самооценки
# ---------------------------------------------------------------------------
class TestSelfBias:
    """Расхождение самооценки с внешней оценкой."""

    def test_завышенная_самооценка_даёт_положительное_расхождение(self):
        result = consolidate(
            [
                observation(AssessmentMethod.SELF, 5.0),
                observation(AssessmentMethod.MANAGER, 3.0, session_id=2),
            ]
        )
        assert result[1].self_bias == 2.0

    def test_заниженная_самооценка_даёт_отрицательное_расхождение(self):
        result = consolidate(
            [
                observation(AssessmentMethod.SELF, 2.0),
                observation(AssessmentMethod.MANAGER, 4.0, session_id=2),
            ]
        )
        assert result[1].self_bias == -2.0

    def test_без_самооценки_расхождение_не_определено(self):
        result = consolidate([observation(AssessmentMethod.MANAGER, 4.0)])
        assert result[1].self_bias is None

    def test_внешний_уровень_не_включает_самооценку(self):
        """
        Сравнивать самооценку со сведённым уровнем, в который она входит, бессмысленно: часть
        расхождения при этом сравнивается сама с собой.
        """
        result = consolidate(
            [
                observation(AssessmentMethod.SELF, 5.0),
                observation(AssessmentMethod.MANAGER, 3.0, session_id=2),
            ]
        )
        assert result[1].external_level == 3.0
        assert result[1].external_level != result[1].level


# ---------------------------------------------------------------------------
# Окно актуальности
# ---------------------------------------------------------------------------
class TestWindow:
    """Граница окна актуальности считается по календарю, а не по суткам."""

    def test_окно_отсчитывается_календарными_месяцами(self):
        assert _window_start(dt.date(2026, 6, 15), 24) == dt.date(2024, 6, 15)
        assert _window_start(dt.date(2026, 1, 10), 1) == dt.date(2025, 12, 10)

    def test_переход_через_год_выполняется_верно(self):
        assert _window_start(dt.date(2026, 3, 31), 12) == dt.date(2025, 3, 31)

    def test_короткий_месяц_не_даёт_несуществующей_даты(self):
        """31 марта минус один месяц — 28 или 29 февраля, а не ошибка."""
        result = _window_start(dt.date(2026, 3, 31), 1)
        assert result.month == 2
        assert result.day <= 29
