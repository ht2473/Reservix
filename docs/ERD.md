# Модель данных

Схема системы содержит **57 моделей** и 83 таблицы в базе данных. Модель строится вокруг реального кадрового
процесса, а не вокруг экранных форм:

```
профиль должности → модель компетенций → оценка → расчёт готовности →
9-box и калибровка → зачисление в резерв → план развития → назначение
```

Каждая стрелка этой цепочки выражена связью в базе данных. Именно связность
отличает систему от набора справочников: разрыв компетенций вычисляется
сопоставлением оценки с требованием модели, индекс готовности — свёрткой
частных критериев, а доля назначений из резерва — соотношением записей
назначений и записей резерва. Ни одна из этих величин не хранится как
введённое человеком число.

## Отличия от исходной схемы

Исходная схема состояла из девяти таблиц. Четыре решения изменили её
существенно, и каждое вызвано конкретной невозможностью.

**Учётная запись отделена от карточки сотрудника.** В таблицах `User` и
`candidate` персональные данные дублировались: сотрудник, бывший одновременно
пользователем и кандидатом, существовал в базе дважды. Теперь `User` отвечает
за вход и права, `Employee` — за кадровые сведения, связь между ними — «один к
одному» и необязательная. Это позволяет вести сотрудников без доступа в
систему и внешних кандидатов без учётной записи.

**Свободный текст заменён ссылками на справочники.** Поля `target_position`,
`current_position` и `competency_name` были строками, из-за чего «Team Lead» и
«Тимлид» считались разными должностями, а сопоставление кандидата с
требованиями было невыполнимо. Теперь это ссылки на `Position` и `Competency`.

**Снято ограничение `UNIQUE` на кандидата в резерве.** Резервист может идти на
несколько целевых должностей — именно конкуренция кандидатов за должность даёт
материал для ранжирования.

**Добавлены сущности процесса, которых не было вовсе:** профиль должности,
модель компетенций с целевыми уровнями и весами, заявка на выдвижение с
маршрутом согласования, критичная должность, план преемственности, назначение,
схема оценивания, снимок индекса готовности, размещение в матрице 9-box,
сессия калибровки, наставничество, журнал аудита.

## Диаграммы по блокам

Диаграммы разделены по блокам предметной области. Единая схема из 57 сущностей
нечитаема: связи пересекаются, и найти на ней нужную цепочку сложнее, чем в
исходном коде моделей. Служебные связи — права Django, обобщённые ссылки на
объекты и отметки авторства — на диаграммах не показаны: они присутствуют почти
в каждой сущности и не добавляют сведений о предметной области.

### Организационная структура (5)

```mermaid
erDiagram
    Branch ||--o{ Department : "филиал"
    Department |o--o{ Department : "вышестоящее подразделение"
    User |o--o{ Department : "руководитель"
    PositionGrade ||--o{ Position : "грейд"
    Department |o--o{ Position : "подразделение"
    Position |o--o{ Position : "подчиняется должности"
    Position ||--|| PositionProfile : "должность"
```

### Люди и доступ (6)

```mermaid
erDiagram
    Branch |o--o{ User : "филиал"
    Department |o--o{ User : "подразделение"
    User ||--|| UserProfile : "пользователь"
    User ||--o{ AccessScope : "пользователь"
    Branch |o--o{ AccessScope : "филиал"
    Department |o--o{ AccessScope : "подразделение"
    User |o--o{ AccessScope : "кем предоставлено"
    User |o--o{ LoginRecord : "пользователь"
    User ||--o| Employee : "учётная запись"
    Branch |o--o{ Employee : "филиал"
    Department |o--o{ Employee : "подразделение"
    Position |o--o{ Employee : "должность"
    Employee |o--o{ Employee : "непосредственный руководитель"
    Employee ||--o{ EmploymentHistory : "сотрудник"
    Position |o--o{ EmploymentHistory : "должность"
    Department |o--o{ EmploymentHistory : "подразделение"
    Branch |o--o{ EmploymentHistory : "филиал"
```

### Компетентностная модель (5)

```mermaid
erDiagram
    CompetencyCategory ||--o{ Competency : "категория"
    Competency ||--o{ CompetencyScaleLevel : "компетенция"
    PositionGrade |o--o{ CompetencyModel : "типовой грейд"
    Position }o--o{ CompetencyModel : "должности"
    Competency }o--o{ CompetencyModel : "компетенции"
    CompetencyModel ||--o{ CompetencyRequirement : "модель компетенций"
    Competency ||--o{ CompetencyRequirement : "компетенция"
```

### Оценка (9)

```mermaid
erDiagram
    CompetencyModel ||--o{ AssessmentCampaign : "модель компетенций"
    Branch |o--o{ AssessmentCampaign : "филиал"
    Department |o--o{ AssessmentCampaign : "подразделение"
    AssessmentCampaign |o--o{ AssessmentSession : "кампания"
    Employee ||--o{ AssessmentSession : "оцениваемый"
    User |o--o{ AssessmentSession : "оценивающий"
    CompetencyModel ||--o{ AssessmentSession : "модель компетенций"
    Position |o--o{ AssessmentSession : "оцениваемая должность"
    User |o--o{ AssessmentSession : "утвердил"
    AssessmentSession ||--o{ AssessmentScore : "сессия"
    Competency ||--o{ AssessmentScore : "компетенция"
    AssessmentCampaign ||--o{ Assessment360Respondent : "кампания"
    Employee ||--o{ Assessment360Respondent : "оцениваемый"
    User ||--o{ Assessment360Respondent : "респондент"
    AssessmentSession ||--o| Assessment360Respondent : "сессия оценки"
    Competency ||--o{ Test : "компетенция"
    Test ||--o{ TestQuestion : "тест"
    TestQuestion ||--o{ TestOption : "вопрос"
    Test ||--o{ TestAttempt : "тест"
    Employee ||--o{ TestAttempt : "испытуемый"
    AssessmentSession |o--o{ TestAttempt : "сессия оценки"
    TestAttempt ||--o{ TestAnswer : "попытка"
    TestQuestion ||--o{ TestAnswer : "вопрос"
    User |o--o{ TestAnswer : "проверил"
    TestOption }o--o{ TestAnswer : "выбранные варианты"
```

### Кадровый резерв (5)

```mermaid
erDiagram
    User |o--o{ ReserveGroup : "куратор группы"
    Employee ||--o{ ReserveEntry : "резервист"
    Position ||--o{ ReserveEntry : "целевая должность"
    ReserveGroup |o--o{ ReserveEntry : "группа резерва"
    ReserveEntry ||--o{ ReserveStatusHistory : "запись резерва"
    User |o--o{ ReserveStatusHistory : "автор изменения"
    Employee ||--o{ ReserveNomination : "кандидат"
    Position ||--o{ ReserveNomination : "целевая должность"
    ReserveGroup |o--o{ ReserveNomination : "группа резерва"
    User |o--o{ ReserveNomination : "инициатор"
    User |o--o{ ReserveNomination : "согласующий руководитель"
    User |o--o{ ReserveNomination : "согласующий от кадровой службы"
    ReserveEntry ||--o| ReserveNomination : "созданная запись резерва"
    ReserveGroup ||--o{ ReserveMembership : "группа резерва"
    Employee ||--o{ ReserveMembership : "резервист"
    ReserveEntry |o--o{ ReserveMembership : "запись резерва"
```

### Преемственность (4)

```mermaid
erDiagram
    Position ||--|| KeyPosition : "должность"
    Employee |o--o{ KeyPosition : "действующий сотрудник"
    KeyPosition ||--o{ SuccessionPlan : "критичная должность"
    User |o--o{ SuccessionPlan : "ответственный"
    SuccessionPlan ||--o{ SuccessionSlate : "план преемственности"
    Employee ||--o{ SuccessionSlate : "преемник"
    ReserveEntry |o--o{ SuccessionSlate : "запись резерва"
    Employee ||--o{ Appointment : "назначенный"
    Position ||--o{ Appointment : "должность"
    SuccessionPlan |o--o{ Appointment : "план преемственности"
    ReserveEntry |o--o{ Appointment : "запись резерва"
    Employee |o--o{ Appointment : "предшественник"
```

### Развитие (5)

```mermaid
erDiagram
    Employee ||--o{ DevelopmentPlan : "сотрудник"
    ReserveEntry |o--o{ DevelopmentPlan : "запись резерва"
    User |o--o{ DevelopmentPlan : "куратор плана"
    User |o--o{ DevelopmentPlan : "утвердил"
    DevelopmentPlan ||--o{ DevelopmentActivity : "план развития"
    Competency |o--o{ DevelopmentActivity : "развиваемая компетенция"
    LearningResource |o--o{ DevelopmentActivity : "учебный ресурс"
    User |o--o{ DevelopmentActivity : "ответственный"
    User |o--o{ DevelopmentActivity : "наставник"
    DevelopmentActivity ||--o{ ActivityEvidence : "мероприятие"
    User |o--o{ ActivityEvidence : "приложил"
    User |o--o{ ActivityEvidence : "проверил"
    User ||--o{ Mentorship : "наставник"
    Employee ||--o{ Mentorship : "подопечный"
    DevelopmentPlan |o--o{ Mentorship : "план развития"
    Competency }o--o{ LearningResource : "развиваемые компетенции"
```

### Аналитика (6)

```mermaid
erDiagram
    ScoringCriterion }o--o{ ScoringScheme : "критерии"
    ScoringScheme ||--o{ ScoringWeight : "схема оценивания"
    ScoringCriterion ||--o{ ScoringWeight : "критерий"
    ReserveEntry ||--o{ ReadinessSnapshot : "запись резерва"
    ScoringScheme ||--o{ ReadinessSnapshot : "схема оценивания"
    Branch |o--o{ CalibrationSession : "филиал"
    Department |o--o{ CalibrationSession : "подразделение"
    User |o--o{ CalibrationSession : "ведущий"
    AssessmentCampaign |o--o{ CalibrationSession : "кампания оценки"
    User }o--o{ CalibrationSession : "участники"
    Employee ||--o{ NineBoxPlacement : "сотрудник"
    CalibrationSession |o--o{ NineBoxPlacement : "сессия калибровки"
    User |o--o{ NineBoxPlacement : "оценку выставил"
```

### Документы и отчёты (3)

```mermaid
erDiagram
    Employee |o--o{ Document : "сотрудник"
    User |o--o{ Document : "загрузил"
    Document ||--o| Document : "заменён документом"
    ReportTemplate ||--o{ GeneratedReport : "шаблон"
    User |o--o{ GeneratedReport : "запросил"
```

### Публичная часть и сервис (9)

```mermaid
erDiagram
    ArticleCategory ||--o{ Article : "раздел"
    User |o--o{ Article : "автор"
    GlossaryTerm }o--o{ GlossaryTerm : "смежные термины"
    FaqCategory ||--o{ FaqItem : "раздел"
    User |o--o{ Feedback : "ответственный"
    User |o--o{ Feedback : "ответил"
    User |o--o{ Feedback : "авторизованный пользователь"
    User ||--o{ Notification : "получатель"
    User |o--o{ Notification : "инициатор"
    User |o--o{ AuditLog : "пользователь"
```

## Ключевые решения о хранении

**Иерархия подразделений — материализованный путь.** Строка вида
`0001.0003.0012` задаёт положение узла в дереве. Подход выбран вместо
рекурсивных запросов потому, что структура организации меняется редко, а
читается постоянно: в каждом списке сотрудников и в каждой проверке прав
руководителя. Стоимость перестроения путей при переносе подразделения
окупается отсутствием рекурсии в горячем пути.

**Индекс готовности хранится, а не вычисляется при чтении.** Расчёт требует
агрегации оценок, кадровых данных и результатов развития по шести критериям.
Выполнять его при отрисовке списка из двухсот записей недопустимо, поэтому
значение хранится в `ReserveEntry`, обновляется фоновой задачей, а история
значений накапливается в `ReadinessSnapshot`.

**Мягкое удаление применяется избирательно.** Оно включено там, где на запись
ссылается история и отчётность: карточка сотрудника, документ. Справочники
используют признак активности — неактивный элемент остаётся видимым в
администрировании, но не предлагается при заполнении форм. Записи журнала
аудита и истории статусов не удаляются вовсе.

**Публичный идентификатор отделён от первичного ключа.** Первичный ключ
остаётся целочисленным ради компактности индексов, а во внешних ссылках —
адресах страниц, программном интерфейсе, ссылках в письмах — используется
`UUID`: он не позволяет перебором узнать численность организации.

**Проверки предметной области закреплены ограничениями базы.** Дата увольнения
обязательна для уволенного и недопустима для работающего; целевой уровень
компетенции не ниже минимально допустимого; выполненное мероприятие имеет дату
выполнения; действующий план преемственности по должности только один. Такие
правила нельзя обойти ни через форму, ни через программный интерфейс, ни
командой сопровождения.

## Соответствие исходным таблицам

| Исходная таблица | Модель системы | Что изменилось |
|---|---|---|
| `branch` | `Branch` | Добавлены код и часовой пояс |
| `department` | `Department` | Добавлена иерархия и руководитель |
| `User` | `User` + `UserProfile` + `AccessScope` | Добавлены область видимости, настройки, история входов |
| `candidate` | `Employee` + `EmploymentHistory` | Отделена от учётной записи, добавлены карьерная история и мобильность |
| `reserve` | `ReserveEntry` + `ReserveNomination` | Снят `UNIQUE`, должность стала ссылкой, добавлено выдвижение |
| `reservestatushistory` | `ReserveStatusHistory` | Добавлен индекс готовности на момент перехода |
| `assessment` | `AssessmentCampaign` + `AssessmentSession` + `AssessmentScore` | Оценка получила процедуру, компетенция — справочник |
| `developmentplan` | `DevelopmentPlan` | Добавлены утверждение и расчёт выполнения |
| `developmentactivity` | `DevelopmentActivity` + `ActivityEvidence` | Добавлены компетенция, вес и подтверждение результата |

## Файлы

* `db/schema.sql` — действующая схема, выгруженная из PostgreSQL 18;
* `db/cadr_reserve.sql` — исходная база данных;
* `db/demo_data.sql` — демонстрационные данные;
* `db/README.md` — описание содержимого каталога.
