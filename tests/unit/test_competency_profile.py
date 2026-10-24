"""Проверка расчёта компетентностного соответствия."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from apps.assessment.aggregation import CompetencyLevel, Observation
from apps.assessment.models import AssessmentMethod
from apps.competencies.models import Competency, CompetencyCategory, CompetencyRequirement
from apps.competencies.services import (
    CRITICAL_GAP,
    NEGLIGIBLE_GAP,
    CompetencyProfile,
    RequirementGap,
    build_radar,
)
from core.constants import Signal

pytestmark = pytest.mark.unit


def measured(level: float, *, competency_id: int = 1, method: str = AssessmentMethod.MANAGER):
    """Сведённый уровень для подстановки в разрыв."""
    return CompetencyLevel(
        competency_id=competency_id,
        level=level,
        observations=(
            Observation(
                competency_id=competency_id,
                method=method,
                level=level,
                assessment_date=dt.date(2026, 1, 1),
                session_id=1,
            ),
        ),
        confidence=0.4,
    )


def gap(
    *,
    target: int = 4,
    actual: float | None = 3.0,
    weight: str = "0.500",
    critical: bool = False,
    minimum: int | None = None,
    competency_id: int = 1,
) -> RequirementGap:
    """Разрыв по одному требованию без обращения к базе."""
    # Категория нужна лепестковой диаграмме: оси упорядочиваются по ней, иначе управленческие
    # компетенции перемешиваются с профессиональными и диаграмма не читается.
    category = CompetencyCategory(pk=1, code="corporate", name="Категория", sort_order=10)
    competency = Competency(
        pk=competency_id,
        name=f"Компетенция {competency_id}",
        code=f"competency-{competency_id}",
        category=category,
        sort_order=competency_id * 10,
    )
    requirement = CompetencyRequirement(
        competency=competency,
        target_level=target,
        minimum_level=minimum,
        weight=Decimal(weight),
        is_critical=critical,
    )
    requirement.competency_id = competency_id
    return RequirementGap(
        requirement=requirement,
        measured=None if actual is None else measured(actual, competency_id=competency_id),
        weight=Decimal(weight),
    )


def profile(*gaps: RequirementGap) -> CompetencyProfile:
    """Профиль из готовых разрывов."""
    return CompetencyProfile(
        employee_id=1,
        competency_model=None,
        gaps=gaps,
        evaluated_on=dt.date(2026, 6, 15),
    )


# ---------------------------------------------------------------------------
# Степень выполнения требования
# ---------------------------------------------------------------------------
class TestAttainment:
    """Нормировка степени выполнения требования."""

    def test_соответствие_целевому_уровню_даёт_единицу(self):
        assert gap(target=4, actual=4.0).attainment == 1.0

    def test_превышение_целевого_уровня_не_даёт_больше_единицы(self):
        """
        Знание сверх требуемого по одной компетенции не компенсирует провал по другой: компетенции
        не взаимозаменяемы.
        """
        assert gap(target=4, actual=5.0).attainment == 1.0

    def test_минимум_шкалы_даёт_ноль_а_не_двадцать_процентов(self):
        """
        Отсчёт от нуля дал бы сотруднику с единицами соответствие двадцать процентов — число,
        выглядящее как частичное соответствие, хотя означающее полное его отсутствие.
        """
        assert gap(target=5, actual=1.0).attainment == 0.0

    def test_середина_полосы_даёт_половину(self):
        """Уровень 3 при цели 5 — половина пути от минимума шкалы к цели."""
        assert gap(target=5, actual=3.0).attainment == pytest.approx(0.5)

    def test_неоценённая_компетенция_даёт_ноль(self):
        """Отсутствие проверки — не доказательство соответствия."""
        assert gap(actual=None).attainment == 0.0

    def test_целевой_уровень_равный_минимуму_шкалы_выполняется_любым_баллом(self):
        """Вырожденный случай не должен приводить к делению на ноль."""
        assert gap(target=1, actual=1.0).attainment == 1.0
        assert gap(target=1, actual=4.0).attainment == 1.0


# ---------------------------------------------------------------------------
# Разрыв и его признаки
# ---------------------------------------------------------------------------
class TestGap:
    """Величина разрыва и сигнальная семантика."""

    def test_разрыв_равен_недобору_до_цели(self):
        assert gap(target=4, actual=2.5).gap == 1.5

    def test_превышение_разрывом_не_считается(self):
        assert gap(target=3, actual=5.0).gap == 0.0

    def test_неоценённая_компетенция_даёт_полный_разрыв(self):
        assert gap(target=4, actual=None).gap == 4.0

    def test_малый_разрыв_не_помечается_тревожно(self):
        """
        Полбалла — цена округления при сведении методов, а не реальный недобор. Красная метка на
        нём приучает к ложным тревогам.
        """
        assert gap(target=4, actual=4 - NEGLIGIBLE_GAP).signal == Signal.READY

    def test_существенный_разрыв_помечается_риском(self):
        assert gap(target=5, actual=5 - CRITICAL_GAP).signal == Signal.RISK

    def test_неоценённая_компетенция_помечается_нейтрально(self):
        """Отсутствие данных — не риск и не норма, а пробел процедуры."""
        assert gap(actual=None, critical=False).signal == Signal.NEUTRAL

    def test_минимальный_уровень_вытесняет_целевой_при_проверке(self):
        """
        Требование с минимально допустимым уровнем выполняется по нему: целевой уровень остаётся
        ориентиром, а не пропуском.
        """
        item = gap(target=5, actual=3.0, minimum=3)
        assert item.is_satisfied
        assert item.gap == 2.0, "разрыв до цели сохраняется и при выполненном минимуме"


class TestBlocking:
    """Критичные требования блокируют назначение."""

    def test_невыполненное_критичное_требование_блокирует(self):
        assert gap(target=4, actual=2.0, critical=True).is_blocking

    def test_выполненное_критичное_требование_не_блокирует(self):
        assert not gap(target=4, actual=4.0, critical=True).is_blocking

    def test_неоценённое_критичное_требование_блокирует(self):
        """Отсутствие проверки блокирует наравне с провалом."""
        assert gap(actual=None, critical=True).is_blocking

    def test_обычное_требование_не_блокирует_даже_при_провале(self):
        assert not gap(target=5, actual=1.0, critical=False).is_blocking


# ---------------------------------------------------------------------------
# Итоговое соответствие
# ---------------------------------------------------------------------------
class TestMatchScore:
    """Соответствие должности — взвешенная сумма степеней выполнения."""

    def test_полное_соответствие_даёт_сто_процентов(self):
        result = profile(
            gap(target=4, actual=4.0, weight="0.500"),
            gap(target=3, actual=3.0, weight="0.500", competency_id=2),
        )
        assert result.match_score == 100.0

    def test_вес_определяет_вклад_требования(self):
        """
        Провал по весомой компетенции обходится дороже провала по второстепенной — это и есть смысл
        весов.
        """
        heavy = profile(
            gap(target=5, actual=1.0, weight="0.900"),
            gap(target=5, actual=5.0, weight="0.100", competency_id=2),
        )
        light = profile(
            gap(target=5, actual=5.0, weight="0.900"),
            gap(target=5, actual=1.0, weight="0.100", competency_id=2),
        )
        assert heavy.match_score < light.match_score

    def test_пустой_профиль_даёт_ноль(self):
        assert profile().match_score == 0.0

    def test_соответствие_не_превышает_ста_процентов(self):
        result = profile(gap(target=3, actual=5.0, weight="1.000"))
        assert result.match_score == 100.0


class TestCoverage:
    """Покрытие оценкой отличает слабого сотрудника от незавершённой оценки."""

    def test_покрытие_считает_только_оценённые_требования(self):
        result = profile(
            gap(actual=3.0),
            gap(actual=None, competency_id=2),
        )
        assert result.coverage == 50.0

    def test_низкое_соответствие_при_низком_покрытии_различимо(self):
        """
        Соответствие 0 при покрытии 0 означает не провал, а отсутствие оценки. Эти два случая
        обязаны различаться, иначе интерфейс введёт в заблуждение.
        """
        unassessed = profile(gap(actual=None), gap(actual=None, competency_id=2))
        failed = profile(gap(target=5, actual=1.0), gap(target=5, actual=1.0, competency_id=2))
        assert unassessed.match_score == failed.match_score == 0.0
        assert unassessed.coverage == 0.0
        assert failed.coverage == 100.0


class TestPriorities:
    """Приоритеты развития упорядочены ценой разрыва, а не его величиной."""

    def test_дорогой_малый_разрыв_важнее_дешёвого_большого(self):
        small_but_costly = gap(target=4, actual=3.5, weight="0.400", competency_id=1)
        large_but_cheap = gap(target=5, actual=2.0, weight="0.020", competency_id=2)

        result = profile(small_but_costly, large_but_cheap)
        priorities = result.development_priorities()

        assert priorities[0].competency.pk == 1
        assert large_but_cheap.gap > small_but_costly.gap, "разрыв больше у дешёвого требования"
        assert large_but_cheap.loss < small_but_costly.loss, "но цена его ниже"

    def test_выполненные_требования_в_приоритеты_не_попадают(self):
        result = profile(gap(target=4, actual=4.0), gap(target=4, actual=2.0, competency_id=2))
        assert [item.competency.pk for item in result.development_priorities()] == [2]

    def test_число_приоритетов_ограничивается(self):
        gaps = [gap(target=4, actual=1.0, competency_id=index) for index in range(1, 11)]
        assert len(profile(*gaps).development_priorities(limit=3)) == 3


class TestStrengths:
    """Превышение целевого уровня — основание искать наставников."""

    def test_в_сильные_стороны_попадает_только_превышение(self):
        result = profile(
            gap(target=3, actual=5.0),
            gap(target=4, actual=4.0, competency_id=2),
            gap(target=4, actual=2.0, competency_id=3),
        )
        assert [item.competency.pk for item in result.strengths] == [1]


# ---------------------------------------------------------------------------
# Лепестковая диаграмма
# ---------------------------------------------------------------------------
class TestRadar:
    """Диаграмма строится на сервере и проверяется числами, а не глазом."""

    def test_пустой_профиль_диаграммы_не_даёт(self):
        assert build_radar(profile()) is None

    def test_число_осей_равно_числу_требований(self):
        chart = build_radar(profile(gap(competency_id=1), gap(competency_id=2)))
        assert chart is not None
        assert len(chart.axes) == 2

    def test_число_осей_ограничено_читаемостью(self):
        """Радар из двадцати лучей нечитаем: подписи наползают друг на друга."""
        gaps = [gap(competency_id=index) for index in range(1, 21)]
        chart = build_radar(profile(*gaps), max_axes=8)
        assert chart is not None
        assert len(chart.axes) == 8

    def test_контур_факта_не_строится_при_пробелах_в_оценке(self):
        """
        Ломаная по части осей создаёт впечатление провала там, где оценки просто нет, — поэтому
        незамкнутый контур не рисуется вовсе.
        """
        chart = build_radar(
            profile(gap(actual=4.0, competency_id=1), gap(actual=None, competency_id=2))
        )
        assert chart is not None
        assert chart.target_points, "контур требований строится всегда"
        assert not chart.has_actual

    def test_полностью_оценённый_профиль_даёт_контур_факта(self):
        chart = build_radar(
            profile(gap(actual=4.0, competency_id=1), gap(actual=3.0, competency_id=2))
        )
        assert chart is not None
        assert chart.has_actual
        assert len(chart.actual_points.split()) == 2

    def test_координаты_лежат_внутри_области_диаграммы(self):
        chart = build_radar(profile(gap(actual=5.0), gap(actual=5.0, competency_id=2)), size=300)
        assert chart is not None
        for point in chart.target_points.split():
            x, y = (float(value) for value in point.split(","))
            assert 0 <= x <= chart.size
            assert 0 <= y <= chart.size

    def test_первая_ось_направлена_вверх(self):
        """Диаграмму читают от вершины, а не от трёх часов."""
        chart = build_radar(profile(gap(actual=5.0), gap(actual=5.0, competency_id=2)), size=200)
        assert chart is not None
        first_x, first_y = (float(value) for value in chart.target_points.split()[0].split(","))
        assert first_x == pytest.approx(chart.center, abs=0.5)
        assert first_y < chart.center
