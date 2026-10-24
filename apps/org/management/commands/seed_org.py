"""
Наполнение справочников организационной структуры.

Запуск::

    uv run python manage.py seed_org
    uv run python manage.py seed_org --flush   # с предварительной очисткой
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.org.models import Branch, Department, Position, PositionGrade, PositionProfile
from core.utils import slugify_ru

#: Филиалы организации.
BRANCHES: list[dict[str, Any]] = [
    {
        "code": "hq-msk",
        "name": "Головной офис",
        "city": "Москва",
        "address": "ул. Тверская, 12",
        "timezone": "Europe/Moscow",
        "is_head_office": True,
        "phone": "+74951234567",
        "email": "office@meridian-soft.ru",
    },
    {
        "code": "br-spb",
        "name": "Филиал Санкт-Петербург",
        "city": "Санкт-Петербург",
        "address": "Невский пр., 100",
        "timezone": "Europe/Moscow",
        "phone": "+78121234567",
        "email": "spb@meridian-soft.ru",
    },
    {
        "code": "br-nsk",
        "name": "Филиал Новосибирск",
        "city": "Новосибирск",
        "address": "Красный пр., 55",
        "timezone": "Asia/Novosibirsk",
        "phone": "+73831234567",
        "email": "nsk@meridian-soft.ru",
    },
    {
        "code": "br-ekb",
        "name": "Филиал Екатеринбург",
        "city": "Екатеринбург",
        "address": "ул. Малышева, 30",
        "timezone": "Asia/Yekaterinburg",
        "phone": "+73431234567",
        "email": "ekb@meridian-soft.ru",
    },
]

#: Подразделения.
DEPARTMENTS: list[dict[str, Any]] = [
    # --- Головной офис ------------------------------------------------------
    {"code": "dev", "name": "Департамент разработки", "branch": "hq-msk"},
    {"code": "hr", "name": "Отдел кадров (HR)", "branch": "hq-msk"},
    {"code": "sales", "name": "Департамент продаж", "branch": "hq-msk"},
    {"code": "finance", "name": "Финансовый отдел", "branch": "hq-msk"},
    {"code": "marketing", "name": "Отдел маркетинга", "branch": "hq-msk"},
    {"code": "legal", "name": "Юридический отдел", "branch": "hq-msk"},
    {"code": "accounting-hq", "name": "Бухгалтерия (Центр)", "branch": "hq-msk"},
    {"code": "facility", "name": "АХО", "branch": "hq-msk"},
    {"code": "labor-safety", "name": "Охрана труда", "branch": "hq-msk"},
    {"code": "security", "name": "Служба безопасности", "branch": "hq-msk"},
    # Вложенные подразделения департамента разработки: без них невозможно
    # проверить область видимости руководителя, охватывающую поддерево.
    {
        "code": "dev-backend",
        "name": "Отдел серверной разработки",
        "branch": "hq-msk",
        "parent": "dev",
    },
    {
        "code": "dev-frontend",
        "name": "Отдел клиентской разработки",
        "branch": "hq-msk",
        "parent": "dev",
    },
    {"code": "dev-qa", "name": "Отдел тестирования", "branch": "hq-msk", "parent": "dev"},
    {
        "code": "dev-analytics",
        "name": "Отдел системного анализа",
        "branch": "hq-msk",
        "parent": "dev",
    },
    {
        "code": "sales-corp",
        "name": "Отдел корпоративных продаж",
        "branch": "hq-msk",
        "parent": "sales",
    },
    {
        "code": "sales-smb",
        "name": "Отдел работы с малым бизнесом",
        "branch": "hq-msk",
        "parent": "sales",
    },
    # --- Санкт-Петербург ----------------------------------------------------
    {"code": "sales-spb", "name": "Отдел продаж (СПб)", "branch": "br-spb"},
    {"code": "logistics-spb", "name": "Складская логистика", "branch": "br-spb"},
    {"code": "it-support-spb", "name": "Отдел IT-поддержки", "branch": "br-spb"},
    # --- Новосибирск --------------------------------------------------------
    {"code": "dev-nsk", "name": "Департамент разработки (НСК)", "branch": "br-nsk"},
    {"code": "accounting-nsk", "name": "Бухгалтерия (НСК)", "branch": "br-nsk"},
    {
        "code": "dev-nsk-platform",
        "name": "Отдел платформенной разработки",
        "branch": "br-nsk",
        "parent": "dev-nsk",
    },
    # --- Екатеринбург -------------------------------------------------------
    {"code": "sales-ekb", "name": "Отдел продаж (ЕКБ)", "branch": "br-ekb"},
    {"code": "warehouse-ekb", "name": "Склад (ЕКБ)", "branch": "br-ekb"},
    {"code": "supply-ekb", "name": "Отдел снабжения", "branch": "br-ekb"},
]

#: Грейды. Задают вертикаль организации: без них система не отличает
#: повышение от горизонтального перехода и не может оценить карьерный рост.
GRADES: list[dict[str, Any]] = [
    {"code": "g1", "name": "Стажёр", "level": 1, "sort_order": 10},
    {"code": "g2", "name": "Младший специалист", "level": 2, "sort_order": 20},
    {"code": "g3", "name": "Специалист", "level": 3, "sort_order": 30},
    {"code": "g4", "name": "Ведущий специалист", "level": 4, "sort_order": 40},
    {"code": "g5", "name": "Главный специалист", "level": 5, "sort_order": 50},
    {"code": "g6", "name": "Руководитель группы", "level": 6, "sort_order": 60, "managerial": True},
    {"code": "g7", "name": "Начальник отдела", "level": 7, "sort_order": 70, "managerial": True},
    {
        "code": "g8",
        "name": "Руководитель департамента",
        "level": 8,
        "sort_order": 80,
        "managerial": True,
    },
    {"code": "g9", "name": "Директор филиала", "level": 9, "sort_order": 90, "managerial": True},
    {"code": "g10", "name": "Член правления", "level": 10, "sort_order": 100, "managerial": True},
]

#: Должности штатного расписания.
POSITIONS: list[dict[str, Any]] = [
    # --- Разработка ---------------------------------------------------------
    {
        "code": "junior-dev",
        "name": "Junior Developer",
        "grade": "g2",
        "dept": "dev-backend",
        "plan": 18,
    },
    {
        "code": "middle-dev",
        "name": "Middle Developer",
        "grade": "g3",
        "dept": "dev-backend",
        "plan": 36,
    },
    {
        "code": "senior-dev",
        "name": "Senior Developer",
        "grade": "g4",
        "dept": "dev-backend",
        "plan": 22,
        "key": True,
    },
    {
        "code": "tech-lead",
        "name": "Tech Lead",
        "grade": "g6",
        "dept": "dev-backend",
        "plan": 4,
        "key": True,
    },
    {
        "code": "team-lead-dev",
        "name": "Team Lead разработки",
        "grade": "g6",
        "dept": "dev-frontend",
        "plan": 3,
        "key": True,
    },
    {
        "code": "qa-engineer",
        "name": "Инженер по тестированию",
        "grade": "g3",
        "dept": "dev-qa",
        "plan": 20,
    },
    {
        "code": "qa-lead",
        "name": "Руководитель группы тестирования",
        "grade": "g6",
        "dept": "dev-qa",
        "plan": 3,
        "key": True,
    },
    {
        "code": "system-analyst",
        "name": "Системный аналитик",
        "grade": "g4",
        "dept": "dev-analytics",
        "plan": 12,
    },
    {
        "code": "head-of-dev",
        "name": "Руководитель департамента разработки",
        "grade": "g8",
        "dept": "dev",
        "plan": 1,
        "key": True,
    },
    {
        "code": "architect",
        "name": "Архитектор решений",
        "grade": "g5",
        "dept": "dev",
        "plan": 4,
        "key": True,
    },
    # --- Продажи ------------------------------------------------------------
    {
        "code": "sales-manager",
        "name": "Менеджер по продажам",
        "grade": "g3",
        "plan": 70,
    },
    {
        "code": "senior-sales",
        "name": "Старший менеджер по продажам",
        "grade": "g4",
        "plan": 24,
    },
    {
        "code": "team-lead-sales",
        "name": "Team Lead Sales",
        "grade": "g6",
        "dept": "sales-corp",
        "plan": 3,
        "key": True,
    },
    {
        "code": "head-of-sales",
        "name": "Head of Sales",
        "grade": "g8",
        "dept": "sales",
        "plan": 1,
        "key": True,
    },
    {
        "code": "account-manager",
        "name": "Менеджер по работе с ключевыми клиентами",
        "grade": "g4",
        "dept": "sales-corp",
        "plan": 12,
        "key": True,
    },
    # --- Обеспечивающие подразделения ---------------------------------------
    {
        "code": "hr-specialist",
        "name": "Специалист по кадрам",
        "grade": "g3",
        "dept": "hr",
        "plan": 8,
    },
    {
        "code": "hr-bp",
        "name": "HR бизнес-партнёр",
        "grade": "g5",
        "dept": "hr",
        "plan": 4,
        "key": True,
    },
    {
        "code": "head-of-hr",
        "name": "Руководитель отдела кадров",
        "grade": "g7",
        "dept": "hr",
        "plan": 1,
        "key": True,
    },
    {"code": "accountant", "name": "Бухгалтер", "grade": "g3", "dept": "accounting-hq", "plan": 18},
    {
        "code": "chief-accountant",
        "name": "Главный бухгалтер",
        "grade": "g7",
        "dept": "accounting-hq",
        "plan": 1,
        "key": True,
    },
    {
        "code": "financial-analyst",
        "name": "Финансовый аналитик",
        "grade": "g4",
        "dept": "finance",
        "plan": 8,
    },
    {
        "code": "cfo",
        "name": "Финансовый директор",
        "grade": "g10",
        "dept": "finance",
        "plan": 1,
        "key": True,
    },
    {"code": "lawyer", "name": "Юрисконсульт", "grade": "g4", "dept": "legal", "plan": 5},
    {
        "code": "marketing-manager",
        "name": "Менеджер по маркетингу",
        "grade": "g3",
        "dept": "marketing",
        "plan": 8,
    },
    # --- Филиалы ------------------------------------------------------------
    {
        "code": "branch-director",
        "name": "Директор филиала",
        "grade": "g9",
        "dept": "sales-spb",
        "plan": 3,
        "key": True,
    },
    {
        "code": "it-support",
        "name": "Специалист технической поддержки",
        "grade": "g2",
        "dept": "it-support-spb",
        "plan": 12,
    },
    {
        "code": "logistics-manager",
        "name": "Менеджер по логистике",
        "grade": "g3",
        "dept": "logistics-spb",
        "plan": 5,
    },
    {
        "code": "warehouse-manager",
        "name": "Заведующий складом",
        "grade": "g6",
        "dept": "warehouse-ekb",
        "plan": 3,
        "key": True,
    },
    {
        "code": "supply-specialist",
        "name": "Специалист по снабжению",
        "grade": "g3",
        "plan": 10,
    },
    {
        "code": "platform-engineer",
        "name": "Инженер платформы",
        "grade": "g4",
        "dept": "dev-nsk-platform",
        "plan": 20,
    },
    # --- Должности, восстановленные из целевых должностей резерва ---------- Наименования ниже
    # встречались в поле `reserve.target_position` и в `candidate.current_position` исходной базы
    # как свободный текст.
    {
        "code": "devops-engineer",
        "name": "DevOps-инженер",
        "grade": "g4",
        "dept": "dev-backend",
        "plan": 6,
        "key": True,
    },
    {
        "code": "ux-lead",
        "name": "Руководитель группы дизайна",
        "grade": "g6",
        "dept": "dev-frontend",
        "plan": 1,
        "key": True,
    },
    {
        "code": "ux-designer",
        "name": "Дизайнер интерфейсов",
        "grade": "g3",
        "dept": "dev-frontend",
        "plan": 8,
    },
    {
        "code": "data-analyst",
        "name": "Аналитик данных",
        "grade": "g4",
        "dept": "dev-analytics",
        "plan": 6,
    },
    {
        "code": "senior-analyst",
        "name": "Ведущий системный аналитик",
        "grade": "g5",
        "dept": "dev-analytics",
        "plan": 3,
        "key": True,
    },
    {
        "code": "product-manager",
        "name": "Менеджер продукта",
        "grade": "g5",
        "dept": "dev",
        "plan": 4,
        "key": True,
    },
    {
        "code": "tech-lead-nsk",
        "name": "Tech Lead (Новосибирск)",
        "grade": "g6",
        "dept": "dev-nsk-platform",
        "plan": 2,
        "key": True,
    },
    {
        "code": "head-of-sales-spb",
        "name": "Руководитель отдела продаж (СПб)",
        "grade": "g7",
        "dept": "sales-spb",
        "plan": 1,
        "key": True,
    },
    {
        "code": "sales-director-ekb",
        "name": "Директор по продажам (Екатеринбург)",
        "grade": "g8",
        "dept": "sales-ekb",
        "plan": 1,
        "key": True,
    },
    {
        "code": "marketing-director",
        "name": "Директор по маркетингу",
        "grade": "g8",
        "dept": "marketing",
        "plan": 1,
        "key": True,
    },
    {
        "code": "smm-lead",
        "name": "Руководитель направления социальных сетей",
        "grade": "g6",
        "dept": "marketing",
        "plan": 1,
    },
    {
        "code": "smm-manager",
        "name": "SMM-менеджер",
        "grade": "g3",
        "dept": "marketing",
        "plan": 3,
    },
    {
        "code": "sysadmin",
        "name": "Системный администратор",
        "grade": "g3",
        "dept": "it-support-spb",
        "plan": 6,
    },
    {
        "code": "senior-sysadmin",
        "name": "Ведущий системный администратор",
        "grade": "g5",
        "dept": "it-support-spb",
        "plan": 2,
        "key": True,
    },
    {
        "code": "it-manager-spb",
        "name": "Руководитель службы поддержки (СПб)",
        "grade": "g6",
        "dept": "it-support-spb",
        "plan": 1,
        "key": True,
    },
    {
        "code": "logistician",
        "name": "Логист",
        "grade": "g2",
        "plan": 18,
    },
    {
        "code": "logistics-director",
        "name": "Директор по логистике",
        "grade": "g8",
        "dept": "logistics-spb",
        "plan": 1,
        "key": True,
    },
    {
        "code": "storekeeper",
        "name": "Кладовщик",
        "grade": "g1",
        "plan": 28,
    },
    {
        "code": "warehouse-chief-ekb",
        "name": "Руководитель складского комплекса (Екатеринбург)",
        "grade": "g7",
        "dept": "warehouse-ekb",
        "plan": 1,
        "key": True,
    },
    {
        "code": "procurement-manager",
        "name": "Руководитель отдела снабжения",
        "grade": "g6",
        "dept": "supply-ekb",
        "plan": 1,
        "key": True,
    },
    {
        "code": "head-of-legal",
        "name": "Руководитель юридического отдела",
        "grade": "g7",
        "dept": "legal",
        "plan": 1,
        "key": True,
    },
    {
        "code": "aho-manager",
        "name": "Руководитель административно-хозяйственного отдела",
        "grade": "g6",
        "dept": "facility",
        "plan": 1,
    },
    {
        "code": "security-chief",
        "name": "Начальник службы безопасности",
        "grade": "g7",
        "dept": "security",
        "plan": 1,
        "key": True,
    },
    {
        "code": "labor-safety-specialist",
        "name": "Специалист по охране труда",
        "grade": "g3",
        "dept": "labor-safety",
        "plan": 3,
    },
    {
        "code": "engineer",
        "name": "Инженер",
        "grade": "g3",
        "plan": 10,
    },
]

#: Профили требований для критичных должностей.
PROFILES: list[dict[str, Any]] = [
    {
        "position": "senior-dev",
        "purpose": "Самостоятельная реализация сложных технических решений и "
        "поддержка младших разработчиков.",
        "responsibilities": "Проектирование и разработка компонентов системы; "
        "код-ревью; наставничество; участие в архитектурных обсуждениях.",
        "education": PositionProfile.Education.BACHELOR,
        "total_years": 5,
        "relevant_years": 4,
        "tenure_months": 12,
    },
    {
        "position": "tech-lead",
        "purpose": "Техническое руководство командой и ответственность за "
        "архитектурную целостность продукта.",
        "responsibilities": "Постановка технических задач; контроль качества "
        "решений; развитие инженеров команды; взаимодействие с бизнес-подразделениями.",
        "education": PositionProfile.Education.BACHELOR,
        "total_years": 7,
        "relevant_years": 5,
        "tenure_months": 18,
        "management": True,
        "subordinates": 3,
    },
    {
        "position": "head-of-dev",
        "purpose": "Управление департаментом разработки: ресурсы, сроки, "
        "качество и развитие инженерной культуры.",
        "responsibilities": "Планирование и распределение ресурсов; управление "
        "руководителями отделов; бюджет департамента; отраслевая экспертиза.",
        "education": PositionProfile.Education.MASTER,
        "total_years": 10,
        "relevant_years": 7,
        "tenure_months": 24,
        "management": True,
        "subordinates": 15,
    },
    {
        "position": "team-lead-sales",
        "purpose": "Выполнение плана продаж командой и развитие менеджеров.",
        "responsibilities": "Планирование продаж; сопровождение крупных сделок; "
        "обучение менеджеров; отчётность перед руководством департамента.",
        "education": PositionProfile.Education.BACHELOR,
        "total_years": 5,
        "relevant_years": 3,
        "tenure_months": 12,
        "management": True,
        "subordinates": 4,
    },
    {
        "position": "head-of-sales",
        "purpose": "Достижение целевых показателей выручки организации.",
        "responsibilities": "Стратегия продаж; управление руководителями "
        "направлений; ключевые переговоры; бюджет департамента.",
        "education": PositionProfile.Education.MASTER,
        "total_years": 10,
        "relevant_years": 8,
        "tenure_months": 24,
        "management": True,
        "subordinates": 20,
    },
    {
        "position": "head-of-hr",
        "purpose": "Обеспечение организации подготовленным персоналом и ведение кадрового резерва.",
        "responsibilities": "Кадровая политика; подбор и оценка; формирование и "
        "развитие кадрового резерва; кадровое делопроизводство.",
        "education": PositionProfile.Education.MASTER,
        "total_years": 8,
        "relevant_years": 6,
        "tenure_months": 18,
        "management": True,
        "subordinates": 4,
    },
    {
        "position": "chief-accountant",
        "purpose": "Достоверность учёта и своевременность отчётности организации.",
        "responsibilities": "Ведение бухгалтерского и налогового учёта; "
        "взаимодействие с контролирующими органами; руководство бухгалтерией.",
        "education": PositionProfile.Education.SPECIALIST,
        "total_years": 8,
        "relevant_years": 6,
        "tenure_months": 24,
        "management": True,
        "subordinates": 5,
    },
    {
        "position": "branch-director",
        "purpose": "Управление филиалом и выполнение его показателей.",
        "responsibilities": "Оперативное управление; бюджет филиала; "
        "взаимодействие с головным офисом; развитие команды.",
        "education": PositionProfile.Education.MASTER,
        "total_years": 12,
        "relevant_years": 8,
        "tenure_months": 36,
        "management": True,
        "subordinates": 25,
    },
    {
        "position": "senior-analyst",
        "purpose": "Формализация требований к продукту и согласование их между "
        "бизнесом и разработкой.",
        "responsibilities": "Сбор и анализ требований; проектирование процессов; "
        "сопровождение разработки; наставничество над аналитиками.",
        "education": PositionProfile.Education.MASTER,
        "total_years": 6,
        "relevant_years": 4,
        "tenure_months": 12,
    },
    {
        "position": "product-manager",
        "purpose": "Развитие продукта: определение состава функций и очерёдности их выпуска.",
        "responsibilities": "Исследование потребностей; формирование дорожной карты; "
        "приоритизация задач; оценка результата выпусков.",
        "education": PositionProfile.Education.MASTER,
        "total_years": 6,
        "relevant_years": 4,
        "tenure_months": 12,
    },
    {
        "position": "senior-sysadmin",
        "purpose": "Работоспособность и защищённость инфраструктуры филиала.",
        "responsibilities": "Администрирование серверов и сетей; резервное копирование; "
        "контроль доступа; расследование инцидентов.",
        "education": PositionProfile.Education.BACHELOR,
        "total_years": 6,
        "relevant_years": 5,
        "tenure_months": 18,
    },
    {
        "position": "logistics-director",
        "purpose": "Обеспечение поставок и складской обработки в установленные сроки.",
        "responsibilities": "Планирование логистики; управление складами филиалов; "
        "договорная работа с перевозчиками; бюджет направления.",
        "education": PositionProfile.Education.MASTER,
        "total_years": 10,
        "relevant_years": 7,
        "tenure_months": 24,
        "management": True,
        "subordinates": 12,
    },
    {
        "position": "head-of-legal",
        "purpose": "Правовая защищённость деятельности организации.",
        "responsibilities": "Договорная работа; представительство в судах; "
        "правовая экспертиза решений; руководство юристами.",
        "education": PositionProfile.Education.SPECIALIST,
        "total_years": 9,
        "relevant_years": 7,
        "tenure_months": 24,
        "management": True,
        "subordinates": 3,
    },
    {
        "position": "sales-director-ekb",
        "purpose": "Выполнение плана продаж филиала и развитие клиентской базы региона.",
        "responsibilities": "Управление отделом продаж; ключевые переговоры; "
        "планирование выручки; развитие менеджеров.",
        "education": PositionProfile.Education.MASTER,
        "total_years": 9,
        "relevant_years": 7,
        "tenure_months": 18,
        "management": True,
        "subordinates": 8,
    },
    {
        "position": "warehouse-chief-ekb",
        "purpose": "Бесперебойная работа складского комплекса филиала.",
        "responsibilities": "Организация приёмки и отгрузки; учёт остатков; "
        "охрана труда на складе; руководство сменами.",
        "education": PositionProfile.Education.VOCATIONAL,
        "total_years": 6,
        "relevant_years": 4,
        "tenure_months": 18,
        "management": True,
        "subordinates": 10,
    },
    {
        "position": "security-chief",
        "purpose": "Защита имущества, персонала и сведений ограниченного доступа.",
        "responsibilities": "Организация пропускного режима; расследование происшествий; "
        "взаимодействие с правоохранительными органами; руководство службой.",
        "education": PositionProfile.Education.SPECIALIST,
        "total_years": 10,
        "relevant_years": 8,
        "tenure_months": 24,
        "management": True,
        "subordinates": 6,
    },
]


class Command(BaseCommand):
    """Создаёт филиалы, подразделения, грейды, должности и их профили."""

    help = "Наполняет справочники организационной структуры демонстрационными данными"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--flush",
            action="store_true",
            help="удалить существующие записи справочников перед наполнением",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        """Наполняет справочники."""
        if options["flush"]:
            self._flush()

        branches = self._create_branches()
        departments = self._create_departments(branches)
        grades = self._create_grades()
        positions = self._create_positions(grades, departments)
        self._create_profiles(positions)

        self.stdout.write(self.style.SUCCESS("\nСправочники организационной структуры заполнены."))
        self.stdout.write(
            f"  Филиалов: {len(branches)}\n"
            f"  Подразделений: {len(departments)}\n"
            f"  Грейдов: {len(grades)}\n"
            f"  Должностей: {len(positions)} "
            f"(из них критичных: {sum(1 for p in positions.values() if p.is_key_position)})\n"
            f"  Профилей должностей: {len(PROFILES)}"
        )

    # -- Этапы наполнения ---------------------------------------------------
    def _flush(self) -> None:
        """Удаляет справочники в порядке, обратном зависимостям."""
        counts = {
            "профилей должностей": PositionProfile.objects.count(),
            "должностей": Position.objects.count(),
            "подразделений": Department.objects.count(),
            "грейдов": PositionGrade.objects.count(),
            "филиалов": Branch.objects.count(),
        }

        PositionProfile.objects.all().delete()
        Position.objects.all().delete()
        Department.objects.all().delete()
        PositionGrade.objects.all().delete()
        Branch.objects.all().delete()

        summary = ", ".join(f"{name}: {value}" for name, value in counts.items() if value)
        if summary:
            self.stdout.write(self.style.WARNING(f"Удалено — {summary}"))

    def _create_branches(self) -> dict[str, Branch]:
        created: dict[str, Branch] = {}

        for item in BRANCHES:
            branch, _ = Branch.objects.update_or_create(
                code=item["code"],
                defaults={
                    "name": item["name"],
                    "city": item["city"],
                    "address": item.get("address", ""),
                    "timezone": item["timezone"],
                    "is_head_office": item.get("is_head_office", False),
                    "phone": item.get("phone", ""),
                    "email": item.get("email", ""),
                    "description": f"Подразделения организации в городе {item['city']}",
                },
            )
            created[item["code"]] = branch

        self.stdout.write(f"Филиалы: {len(created)}")
        return created

    def _create_departments(self, branches: dict[str, Branch]) -> dict[str, Department]:
        """Создаёт подразделения в два прохода."""
        created: dict[str, Department] = {}

        for pass_number in (1, 2):
            for item in DEPARTMENTS:
                has_parent = "parent" in item
                if (pass_number == 1) == has_parent:
                    continue

                department, _ = Department.objects.update_or_create(
                    code=item["code"],
                    defaults={
                        "name": item["name"],
                        "branch": branches[item["branch"]],
                        "parent": created.get(item.get("parent", "")),
                    },
                )
                created[item["code"]] = department

        self.stdout.write(f"Подразделения: {len(created)}")
        return created

    def _create_grades(self) -> dict[str, PositionGrade]:
        created: dict[str, PositionGrade] = {}

        for item in GRADES:
            grade, _ = PositionGrade.objects.update_or_create(
                code=item["code"],
                defaults={
                    "name": item["name"],
                    "level": item["level"],
                    "sort_order": item["sort_order"],
                    "is_managerial": item.get("managerial", False),
                },
            )
            created[item["code"]] = grade

        self.stdout.write(f"Грейды: {len(created)}")
        return created

    def _create_positions(
        self, grades: dict[str, PositionGrade], departments: dict[str, Department]
    ) -> dict[str, Position]:
        created: dict[str, Position] = {}

        for item in POSITIONS:
            # Должность без указания подразделения — типовая: она встречается в нескольких
            # подразделениях, и закреплять её за одним было бы ошибкой штатного расписания.
            department = departments[item["dept"]] if "dept" in item else None
            description = (
                f"Должность подразделения «{department.name}»"
                if department is not None
                else "Типовая должность штатного расписания"
            )

            position, _ = Position.objects.update_or_create(
                code=item["code"],
                defaults={
                    "name": item["name"],
                    "grade": grades[item["grade"]],
                    "department": department,
                    "headcount_plan": item.get("plan", 1),
                    "is_key_position": item.get("key", False),
                    "description": description,
                },
            )
            created[item["code"]] = position

        self.stdout.write(f"Должности: {len(created)}")
        return created

    def _create_profiles(self, positions: dict[str, Position]) -> None:
        for item in PROFILES:
            PositionProfile.objects.update_or_create(
                position=positions[item["position"]],
                defaults={
                    "purpose": item["purpose"],
                    "responsibilities": item["responsibilities"],
                    "min_education": item["education"],
                    "min_total_experience_years": item["total_years"],
                    "min_relevant_experience_years": item["relevant_years"],
                    "min_company_tenure_months": item["tenure_months"],
                    "requires_management_experience": item.get("management", False),
                    "min_subordinates": item.get("subordinates", 0),
                    "is_published": True,
                },
            )

        self.stdout.write(f"Профили должностей: {len(PROFILES)}")

    @staticmethod
    def _slug(value: str) -> str:
        """Формирует код из наименования, если он не задан явно."""
        return slugify_ru(value, max_length=32)
