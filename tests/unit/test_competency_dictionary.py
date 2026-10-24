"""Проверка корпоративной модели компетенций как данных."""

from __future__ import annotations

import pytest

from apps.competencies.dictionary import (
    CATEGORIES,
    COMPETENCIES,
    LEGACY_ALIASES,
    LEVEL_NAMES,
    LEVEL_SUMMARIES,
    MODELS,
)
from apps.competencies.models import WEIGHT_SUM_TOLERANCE

pytestmark = pytest.mark.unit

COMPETENCY_CODES = {spec.code for spec in COMPETENCIES}
CATEGORY_CODES = {spec.code for spec in CATEGORIES}
MODEL_CODES = {spec.code for spec in MODELS}


# ---------------------------------------------------------------------------
# Справочник компетенций
# ---------------------------------------------------------------------------
class TestDictionary:
    """Целостность справочника."""

    def test_коды_компетенций_уникальны(self):
        assert len(COMPETENCY_CODES) == len(COMPETENCIES)

    def test_каждая_компетенция_относится_к_существующей_категории(self):
        unknown = {
            spec.code: spec.category for spec in COMPETENCIES if spec.category not in CATEGORY_CODES
        }
        assert unknown == {}

    def test_у_каждой_компетенции_описаны_все_уровни_шкалы(self):
        """
        Компетенция с пробелами в шкале непригодна для оценки: эксперт не различает соседние уровни
        и ставит балл наугад.
        """
        incomplete = {
            spec.code: len(spec.indicators)
            for spec in COMPETENCIES
            if len(spec.indicators) != len(LEVEL_NAMES)
        }
        assert incomplete == {}

    def test_индикаторы_уровней_различаются(self):
        """Повторённый текст означает, что уровень описан формально."""
        duplicated = [
            spec.code for spec in COMPETENCIES if len(set(spec.indicators)) != len(spec.indicators)
        ]
        assert duplicated == []

    def test_у_каждой_компетенции_есть_определение_и_рекомендация(self):
        """
        Определение делает оценку воспроизводимой, рекомендация подставляется в индивидуальный план
        развития при выявлении разрыва.
        """
        for spec in COMPETENCIES:
            assert len(spec.definition) >= 40, spec.code
            assert len(spec.development_hint) >= 40, spec.code

    def test_число_описаний_уровней_совпадает_с_числом_наименований(self):
        assert len(LEVEL_SUMMARIES) == len(LEVEL_NAMES)

    def test_сквозные_компетенции_существуют(self):
        """
        Без сквозных компетенций сотрудники разных функций несравнимы: у них нет ни одного общего
        основания для сопоставления.
        """
        assert any(spec.is_core for spec in COMPETENCIES)

    def test_проверяемые_тестом_компетенции_существуют(self):
        assert any(spec.is_testable for spec in COMPETENCIES)


# ---------------------------------------------------------------------------
# Сведение наименований исходной базы
# ---------------------------------------------------------------------------
class TestLegacyAliases:
    """Соответствия наименований дампа компетенциям корпоративной модели."""

    def test_каждое_наименование_ведёт_к_существующей_компетенции(self):
        unknown = {
            legacy: target
            for legacy, target in LEGACY_ALIASES.items()
            if target not in COMPETENCY_CODES
        }
        assert unknown == {}

    def test_сведение_действительно_сокращает_справочник(self):
        """
        Смысл соответствий — устранить синонимы. Если бы каждое наименование вело в собственную
        компетенцию, сведение ничего не давало бы.
        """
        assert len(set(LEGACY_ALIASES.values())) < len(LEGACY_ALIASES)

    def test_разновидности_лидерства_сводятся_в_одну_компетенцию(self):
        """
        Лидерство руководителя склада и лидерство руководителя разработки — одно качество,
        наблюдаемое в разных условиях.
        """
        variants = (
            "leadership",
            "sales-leadership",
            "tech-leadership",
            "technical-leadership",
            "team-leadership",
            "financial-leadership",
            "legal-leadership",
            "warehouse-leadership",
        )

        assert {LEGACY_ALIASES[code] for code in variants} == {"leadership"}
        assert LEGACY_ALIASES["strategic-leadership"] == "strategic-thinking"

    def test_отраслевое_управление_сводится_в_управление_людьми(self):
        for code in ("team-management", "sales-management", "warehouse-management"):
            assert LEGACY_ALIASES[code] == "people-management"

    def test_цепочек_соответствий_не_образуется(self):
        """
        Компетенция-приёмник не должна сама вести в третью: перенос выполняется в один шаг, и
        цепочка означала бы потерю оценок на промежуточном звене.
        """
        for target in set(LEGACY_ALIASES.values()):
            assert LEGACY_ALIASES.get(target, target) == target


# ---------------------------------------------------------------------------
# Модели требований
# ---------------------------------------------------------------------------
class TestModels:
    """Состав и веса моделей требований."""

    def test_коды_моделей_уникальны(self):
        assert len(MODEL_CODES) == len(MODELS)

    @pytest.mark.parametrize("spec", MODELS, ids=lambda spec: spec.code)
    def test_сумма_весов_модели_равна_единице(self, spec):
        """
        Иначе итоговое соответствие нельзя истолковать как долю: оно окажется больше ста процентов
        либо систематически заниженным.
        """
        total = sum(requirement.weight for requirement in spec.requirements)
        assert abs(total - 1) <= float(WEIGHT_SUM_TOLERANCE)

    @pytest.mark.parametrize("spec", MODELS, ids=lambda spec: spec.code)
    def test_требования_ссылаются_на_существующие_компетенции(self, spec):
        unknown = [
            requirement.competency
            for requirement in spec.requirements
            if requirement.competency not in COMPETENCY_CODES
        ]
        assert unknown == []

    @pytest.mark.parametrize("spec", MODELS, ids=lambda spec: spec.code)
    def test_компетенция_входит_в_модель_не_более_одного_раза(self, spec):
        codes = [requirement.competency for requirement in spec.requirements]
        assert len(set(codes)) == len(codes)

    @pytest.mark.parametrize("spec", MODELS, ids=lambda spec: spec.code)
    def test_минимальный_уровень_не_превышает_целевой(self, spec):
        for requirement in spec.requirements:
            if requirement.minimum is not None:
                assert requirement.minimum <= requirement.target, requirement.competency

    @pytest.mark.parametrize("spec", MODELS, ids=lambda spec: spec.code)
    def test_целевые_уровни_лежат_на_шкале(self, spec):
        for requirement in spec.requirements:
            assert 1 <= requirement.target <= len(LEVEL_NAMES), requirement.competency

    @pytest.mark.parametrize("spec", MODELS, ids=lambda spec: spec.code)
    def test_у_каждой_модели_есть_критичное_требование(self, spec):
        """
        Модель без критичных требований допускает кандидата, проваленного по главной компетенции
        должности, если он добрал процентов на второстепенных.
        """
        assert any(requirement.is_critical for requirement in spec.requirements)

    def test_уровневые_модели_покрывают_шкалу_грейдов_без_пропусков(self):
        """
        Должность, чей грейд не попал ни в один диапазон, осталась бы без требований, и
        соответствие по ней не считалось бы вовсе.
        """
        covered: set[int] = set()
        for spec in MODELS:
            if spec.is_functional:
                continue
            low, high = spec.grade_range
            covered |= set(range(low, high + 1))

        assert covered == set(range(1, 11))

    def test_уровневые_диапазоны_не_пересекаются(self):
        """Пересечение сделало бы выбор модели зависящим от порядка перебора."""
        seen: set[int] = set()
        for spec in MODELS:
            if spec.is_functional:
                continue
            low, high = spec.grade_range
            levels = set(range(low, high + 1))
            assert not (levels & seen), spec.code
            seen |= levels

    def test_функциональные_модели_не_делят_подразделения(self):
        """Иначе должность подразделения получила бы две модели сразу."""
        seen: set[str] = set()
        for spec in MODELS:
            if not spec.is_functional:
                continue
            assert not (set(spec.departments) & seen), spec.code
            seen |= set(spec.departments)

    def test_функциональные_модели_не_делят_должности(self):
        seen: set[str] = set()
        for spec in MODELS:
            if not spec.is_functional:
                continue
            assert not (set(spec.positions) & seen), spec.code
            seen |= set(spec.positions)

    def test_управленческие_модели_требуют_управления_людьми(self):
        """
        Требование составляет содержание руководящей должности: модель без него оценивала бы
        руководителя как специалиста.
        """
        for code in ("tier-lead", "tier-head", "tier-executive"):
            spec = next(item for item in MODELS if item.code == code)
            codes = {requirement.competency for requirement in spec.requirements}
            assert "people-management" in codes

    def test_каждая_компетенция_справочника_где_то_требуется(self):
        """
        Компетенция, не входящая ни в одну модель, не оценивается ни у кого: она описана, но
        система её не измеряет.
        """
        required = {requirement.competency for spec in MODELS for requirement in spec.requirements}
        assert COMPETENCY_CODES - required == set()
