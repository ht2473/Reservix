"""Проверка банка заданий встроенного тестирования."""

from __future__ import annotations

import pytest

from apps.assessment.models import QuestionKind
from apps.assessment.question_bank import TESTS
from apps.competencies.dictionary import COMPETENCIES

pytestmark = pytest.mark.unit

COMPETENCY_CODES = {spec.code for spec in COMPETENCIES}
ALL_QUESTIONS = [(test.code, question) for test in TESTS for question in test.questions]


class TestBankStructure:
    """Состав банка заданий."""

    def test_коды_тестов_уникальны(self):
        codes = [test.code for test in TESTS]
        assert len(set(codes)) == len(codes)

    def test_каждый_тест_привязан_к_существующей_компетенции(self):
        unknown = [test.code for test in TESTS if test.competency not in COMPETENCY_CODES]
        assert unknown == []

    def test_тест_проверяет_компетенцию_помеченную_как_проверяемую(self):
        """
        Иначе результат теста некуда переносить: страница компетенции не покажет тест, а сотрудник
        не поймёт, что именно измерялось.
        """
        testable = {spec.code for spec in COMPETENCIES if spec.is_testable}
        assert {test.competency for test in TESTS} <= testable

    @pytest.mark.parametrize("test", TESTS, ids=lambda test: test.code)
    def test_заданий_в_банке_хватает_на_попытку(self, test):
        """
        Тест, в котором заданий меньше, чем требует настройка попытки, не запускается вовсе — и
        обнаруживается это только при попытке пройти.
        """
        assert len(test.questions) >= test.questions_per_attempt

    @pytest.mark.parametrize("test", TESTS, ids=lambda test: test.code)
    def test_порог_прохождения_лежит_в_разумных_границах(self, test):
        assert 0 < test.passing_score <= 100

    @pytest.mark.parametrize("test", TESTS, ids=lambda test: test.code)
    def test_ограничение_времени_задано(self, test):
        assert test.time_limit_minutes > 0


class TestQuestions:
    """Корректность отдельных заданий."""

    @pytest.mark.parametrize(
        ("test_code", "question"), ALL_QUESTIONS, ids=lambda item: str(item)[:40]
    )
    def test_у_задания_с_выбором_есть_правильный_ответ(self, test_code, question):
        if question.kind == QuestionKind.OPEN:
            pytest.skip("развёрнутый ответ проверяется экспертом")
        correct = [option for option in question.options if option.is_correct]
        assert correct, f"{test_code}: {question.text[:60]}"

    @pytest.mark.parametrize(
        ("test_code", "question"), ALL_QUESTIONS, ids=lambda item: str(item)[:40]
    )
    def test_у_задания_с_выбором_есть_неправильные_варианты(self, test_code, question):
        """
        Задание, где верны все варианты, ничего не проверяет: испытуемый получает балл, отметив всё
        подряд.
        """
        if question.kind == QuestionKind.OPEN:
            pytest.skip("развёрнутый ответ вариантов не имеет")
        wrong = [option for option in question.options if not option.is_correct]
        assert wrong, f"{test_code}: {question.text[:60]}"

    @pytest.mark.parametrize(
        ("test_code", "question"), ALL_QUESTIONS, ids=lambda item: str(item)[:40]
    )
    def test_у_задания_с_одним_ответом_ровно_один_верный(self, test_code, question):
        """
        Два верных варианта в задании этого вида делают его нерешаемым: засчитывается только точное
        совпадение множеств.
        """
        if question.kind != QuestionKind.SINGLE:
            pytest.skip("вид задания допускает несколько верных ответов")
        correct = [option for option in question.options if option.is_correct]
        assert len(correct) == 1, f"{test_code}: {question.text[:60]}"

    @pytest.mark.parametrize(
        ("test_code", "question"), ALL_QUESTIONS, ids=lambda item: str(item)[:40]
    )
    def test_у_задания_с_несколькими_ответами_их_действительно_несколько(self, test_code, question):
        if question.kind != QuestionKind.MULTIPLE:
            pytest.skip("вид задания предполагает один верный ответ")
        correct = [option for option in question.options if option.is_correct]
        assert len(correct) >= 2, f"{test_code}: {question.text[:60]}"

    @pytest.mark.parametrize(
        ("test_code", "question"), ALL_QUESTIONS, ids=lambda item: str(item)[:40]
    )
    def test_задание_объясняет_ошибку(self, test_code, question):
        """Тест, не объясняющий ошибку, ничего не развивает."""
        assert len(question.explanation) >= 40, f"{test_code}: {question.text[:60]}"

    @pytest.mark.parametrize(
        ("test_code", "question"), ALL_QUESTIONS, ids=lambda item: str(item)[:40]
    )
    def test_варианты_ответа_не_повторяются(self, test_code, question):
        texts = [option.text for option in question.options]
        assert len(set(texts)) == len(texts), f"{test_code}: {question.text[:60]}"

    @pytest.mark.parametrize(
        ("test_code", "question"), ALL_QUESTIONS, ids=lambda item: str(item)[:40]
    )
    def test_сложность_и_вес_лежат_в_допустимых_границах(self, test_code, question):
        assert 1 <= question.difficulty <= 5
        assert question.weight >= 1

    @pytest.mark.parametrize("test", TESTS, ids=lambda test: test.code)
    def test_формулировки_заданий_не_повторяются(self, test):
        texts = [question.text for question in test.questions]
        assert len(set(texts)) == len(texts)

    @pytest.mark.parametrize("test", TESTS, ids=lambda test: test.code)
    def test_задания_различаются_по_сложности(self, test):
        """
        Тест из заданий одной сложности не различает уровни владения: он либо сдаётся всеми, либо
        никем.
        """
        difficulties = {question.difficulty for question in test.questions}
        assert len(difficulties) > 1, test.code
