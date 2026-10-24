# =============================================================================
# Reservix — команды разработки
#
# Требуется just (https://github.com/casey/just):
#   winget install --id Casey.Just
#
# Список команд: just --list
# =============================================================================

# Оболочка Windows: PowerShell 7. Каждая команда выполняется в своём процессе.
set windows-shell := ["pwsh.exe", "-NoLogo", "-NoProfile", "-Command"]

# Команда по умолчанию — список доступных рецептов.
default:
    @just --list

# --- Окружение ---------------------------------------------------------------

# Первичная настройка проекта: зависимости, инфраструктура, база, сборка статики
setup: install up migrate assets compilemessages
    @echo "Готово. Создайте администратора: just superuser"

# Установить зависимости, включая инструменты разработки
install:
    uv sync --all-groups
    uv run pre-commit install

# Обновить зависимости в пределах ограничений pyproject.toml
upgrade:
    uv lock --upgrade
    uv sync --all-groups

# --- Инфраструктура ----------------------------------------------------------

# Поднять PostgreSQL, Redis и почтовый сервер-заглушку
up:
    docker compose -f deploy/docker-compose.dev.yml up -d

# Остановить инфраструктуру, сохранив данные
down:
    docker compose -f deploy/docker-compose.dev.yml down

# Остановить инфраструктуру и удалить данные
reset-infra:
    docker compose -f deploy/docker-compose.dev.yml down -v

# --- Приложение --------------------------------------------------------------

# Запустить сервер разработки
run:
    uv run python manage.py runserver 8000

# Запустить воркер фоновых задач
worker:
    uv run celery -A config worker --loglevel=info --pool=solo

# Запустить планировщик периодических задач
beat:
    uv run celery -A config beat --loglevel=info

# Интерактивная оболочка с автоимпортом моделей
shell:
    uv run python manage.py shell_plus

# Оболочка PostgreSQL
dbshell:
    docker compose -f deploy/docker-compose.dev.yml exec postgres psql -U reservix -d reservix

# --- База данных -------------------------------------------------------------

# Создать миграции по изменениям моделей
makemigrations *ARGS:
    uv run python manage.py makemigrations {{ARGS}}

# Применить миграции
migrate:
    uv run python manage.py migrate

# Проверить, что модели и миграции согласованы (используется в CI)
check-migrations:
    uv run python manage.py makemigrations --check --dry-run

# Заполнить базу демонстрационными данными
seed:
    uv run python manage.py seed_demo

# Заполнить английские версии справочников и содержимого
translations:
    uv run python manage.py load_translations --report

# Пересоздать демонстрационные данные поверх существующих
reseed:
    uv run python manage.py seed_demo --reset

# Пересоздать базу с нуля и заполнить демонстрационными данными
reset-db: reset-infra up
    @Start-Sleep -Seconds 8
    uv run python manage.py migrate
    uv run python manage.py seed_demo

# Создать администратора системы
superuser:
    uv run python manage.py createsuperuser

# Выгрузить схему базы в db/schema.sql
dump-schema:
    docker compose -f deploy/docker-compose.dev.yml exec -T postgres pg_dump -U reservix -d reservix --schema-only --no-owner --no-privileges > db/schema.sql

# Выгрузить полную копию демонстрационного стенда в db/demo_data.sql
dump-demo:
    docker compose -f deploy/docker-compose.dev.yml exec -T postgres pg_dump -U reservix -d reservix --no-owner --no-privileges --exclude-table-data=django_session --exclude-table-data=axes_* > db/demo_data.sql

# --- Ресурсы -----------------------------------------------------------------

# Собрать стили и сценарии
assets:
    uv run python scripts/build_assets.py

# Собрать ресурсы со сжатием (используется при развёртывании)
assets-min:
    uv run python scripts/build_assets.py --minify

# Собрать статику Django для продакшена
collectstatic:
    uv run python manage.py collectstatic --noinput

# --- Локализация -------------------------------------------------------------
# Каталоги обслуживает scripts/i18n.py, а не makemessages и compilemessages:
# штатные команды вызывают xgettext и msgfmt из набора GNU gettext, которого нет
# в Windows и который не ставится вместе с зависимостями проекта.

# Извлечь строки для перевода в locale/*/LC_MESSAGES/django.po
messages:
    uv run python scripts/i18n.py extract

# Скомпилировать переводы в двоичные каталоги .mo
compilemessages:
    uv run python scripts/i18n.py compile

# Проверить полноту перевода (используется в CI)
messages-check:
    uv run python scripts/i18n.py check

# --- Качество кода -----------------------------------------------------------

# Полная проверка: линтер, типы, безопасность, тесты
check: lint typecheck security test

# Линтер и форматтер
lint:
    uv run ruff check .
    uv run ruff format --check .

# Исправить всё, что ruff умеет исправлять автоматически
fix:
    uv run ruff check --fix .
    uv run ruff format .

# Проверка типов
typecheck:
    uv run mypy apps core config

# Анализ безопасности кода и зависимостей
security:
    uv run bandit -c pyproject.toml -r apps core config
    uv run pip-audit

# --- Тестирование ------------------------------------------------------------

# Быстрый прогон: модульные и интеграционные тесты
test:
    uv run pytest -x -q --no-cov

# Полный прогон с отчётом о покрытии
test-cov:
    uv run pytest --cov --cov-report=term-missing --cov-report=html

# Прогон в несколько процессов
test-fast:
    uv run pytest -n auto -q

# Проверки безопасности по классификации OWASP Top-10
test-security:
    uv run pytest tests/security -q --no-cov

# Сквозные сценарии в браузере
test-e2e:
    uv run pytest tests/e2e --browser chromium --browser firefox

# Установить браузеры для сквозных сценариев
browsers:
    uv run playwright install --with-deps chromium firefox

# Нагрузочное тестирование: профиль задаётся в веб-интерфейсе Locust
load-test:
    uv run locust -f tests/load/locustfile.py --host http://127.0.0.1:8000

# Нагрузочное тестирование без интерфейса: ступени 50 → 200 → 500
load-test-headless:
    uv run locust -f tests/load/locustfile.py --host http://127.0.0.1:8000 --headless --html reports/load.html

# Сверить контрольные суммы всех документов хранилища
verify-documents:
    uv run python manage.py verify_documents
