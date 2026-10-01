# Проверяемый мост ревизий старого форка (локальный прототип)

Мост разводит идентификаторы старой истории. Его исходное доказательство
закреплено на v4.15.0; текущая integration-ветка также включает upstream 5.0.0.
CLI моста по-прежнему ограничен лабораторией и не применяется повторно к уже
обновлённой базе `evo_0109`.

Шесть кастомных миграций `0101`–`0106` теперь называются `evo_0101`–`evo_0106`.
DDL сохранён; меняются только идентификаторы, связи и документация ревизий.
`evo_0101` продолжает общую `0100`. Runtime включает неизменённую цепочку
upstream `0101`–`0127`; `evo_merge_4_15_0` объединяет `0127` и `evo_0106`.
Последующие `evo_0107` и `evo_0108` сохраняют исторические gift aliases и
выравнивают server defaults/индексы. Следующая `evo_0109` добавляет nullable проверенную привязку трафика к numeric ID.
Голова релиза 4.15.0 — `evo_0109`. Для 5.0.0 новая `evo_merge_5_0_0`
соединяет её с неизменённой upstream-головой `0131`; следующая `evo_0110`
выравнивает метаданные и DDL новых Cashera/DPI таблиц. Текущая голова —
`evo_0110`; подробности — [upstream-5-billing-migrations.md](upstream-5-billing-migrations.md). Отдельный
временный тестовый граф фазы 2 сохранён как доказательство исходной DDL-цепочки.

## Допустимые состояния

Мост принимает только одну строку `0106` и точное совпадение всей схемы с одним
из закреплённых профилей форка `4b06edcdce26850c03ca474e8d195ef94ddb347e`:

- fresh: схема старых моделей плюс прежний runtime grace guard;
- upgraded: синтетический predecessor `0102`, реально прошедший закреплённые
  `0103`–`0106` через Alembic. Он отличается 15 server defaults, а не данными.

Профили содержатся в `app/database/fork_profiles`. Это исходные каталоги
PostgreSQL 15.13, независимые от будущей `Base.metadata`. Тестовые DDL/seed и
provenance лежат в `tests/fixtures/migrations`. Сравниваются колонки, defaults,
типы, nullable, constraints/FK (включая target schema), индексы/порядок/opclasses/
predicates/validity, sequences, функции, triggers, rules и неизвестные объекты.
Extension-owned объекты исключаются по каталогу зависимостей PostgreSQL.
Имена автоматически созданных FK не считаются семантикой; их определение
проверяется. Владельцы, OID, статистика и удалённые ordinal positions не входят.

Неизвестное отличие означает отказ. Профили не описывают любую историческую
установку с надписью `0106`; отличия реальной базы сначала требуют отдельного
разбора. Старые `0101`–`0105`, upstream-only, смешанная/повреждённая схема,
несколько/нет строк версии и unknown revision не переписываются.

## Локальная команда

CLI специально ограничен выделенным Unix-only кластером baseline harness.
Он не принимает произвольный URL, не читает `.env` и не подключается к панели,
Telegram или платёжным API. Это защита границ прототипа; команды для живой
копии БД будут подготовлены отдельно после review и репетиции восстановления.

```sh
.venv/bin/python scripts/bridge_fork_revision.py \
  --local-postgres /absolute/path/to/baseline/postgres.json \
  --schema bridge_fixture
```

Без `--apply` транзакция read-only; JSON-отчёт содержит schema hash, профиль,
исходную версию и предложенную `evo_0106`. Для применения на подготовленной
локальной схеме нужны **оба** значения из проверенного dry-run:

```sh
.venv/bin/python scripts/bridge_fork_revision.py \
  --local-postgres /absolute/path/to/baseline/postgres.json \
  --schema bridge_fixture --apply \
  --expected-revision 0106 --expected-digest HASH_FROM_DRY_RUN
```

При apply используются общий session advisory lock, `ACCESS EXCLUSIVE` на
таблице версии и `SHARE` на application tables, повторный fingerprint,
условный UPDATE ровно одной строки и проверка перед COMMIT. `lock_timeout=3s`,
`statement_timeout=30s`; занятый advisory lock сразу даёт отказ. Writers должны
быть остановлены перед будущей репетицией: advisory protocol не может запретить
произвольный SQL чужой сессии. Наличие внешней caller-транзакции вызывает отказ,
не её скрытый rollback. Session lock сохраняется через Alembic autocommit.

При ошибке до COMMIT версия остаётся `0106`; повтор после успеха возвращает
`already_bridged` без записи. Счета, ledger, лимиты, сроки и короткие gift-коды
мост не изменяет. Мост не делает `stamp head` и не выполняет кастомный backfill.
Последующий Alembic upgrade — отдельный, не глобально атомарный шаг.

## Startup и прямой Alembic

Оба пути проверяют неоднозначную старую историю до планирования миграций.
`main.py` запускает fatal preflight **до** `SKIP_MIGRATION`; `ForkRevisionError`
не подавляется `ALLOW_MIGRATION_FAILURE`. `env.py` использует переданный
connection и тот же session lock. Offline `--sql` не может проверить источник
и получает диагностический отказ.

Непустая схема без версии больше не получает автоматический `stamp 0001`.
Для действительно пустой PostgreSQL создание объединённых таблиц, guards,
выключенного встроенного reminder и `stamp head` (`evo_0110`) выполняются в одной
транзакции под тем же lock. Повторный startup после bridge и fresh bootstrap
проверяется настоящей PostgreSQL. Полный каталог fresh и upgrade сравнивается
по тем же строгим полям fingerprint; исходные значения строк, включая SQL NULL,
остаются неизменными. На уже обновлённой схеме старый bridge не применяется.

Унаследованные модели используют PostgreSQL `JSONB` в `info_pages`: создание
всех таблиц с нуля на SQLite уже не поддерживается. Этот прототип не исправляет
типы моделей; тест явно проверяет ошибку без ложного stamp. Подготовленная
SQLite-head проходит повторный no-op startup; partial/ambiguous SQLite
диагностируется, PostgreSQL-мост к ней не применяется.

## Проверка

```sh
.venv/bin/python tests/baseline/run.py --suite migration \
  --pg-bin /absolute/path/to/postgresql/bin --output /absolute/new/evidence-dir
.venv/bin/python tests/baseline/run.py --suite custom \
  --pg-bin /absolute/path/to/postgresql/bin --output /absolute/another/evidence-dir
```

Каждый запуск создаёт и останавливает свой локальный кластер. Произвольный
DB URL harness не принимает. Network guard перехватывает `socket.connect`/`connect_ex` в pytest-процессе
и разрешает только Unix sockets. Это не полная изоляция ОС, запрет UDP/sendto
или сетевых вызовов дочерних процессов.
В identity фиксируются исходники, CLI, SQL/seed и JSON-профили; pytest skipped
не считается успешным baseline.

Combined-graph тесты используют 27 vendored upstream scripts байт-в-байт из
`877690a7039d1326b2c00eda3e297879b80c0678`, проверяют SHA256 и AST общей сотни,
затем выполняют настоящую `alembic.command.upgrade`. Подтверждаются новые
таблицы/числовые колонки, NULL для ещё не backfilled numeric IDs, grace markers,
disabled builtin reminder, сохранение исходных строк, повторные no-op и resume
после `0106`/`0121`. Git/fetch во время теста не требуется.

Успех этих тестов подтверждает bridge и указанную DDL-цепочку на синтетических
профилях. Runtime-набор дополнительно проверяет parity объединённых моделей, исходные
строки и ORM backup/restore с заменой и без неё. Поведение API 3.x проверяется
отдельными mocked-контрактами общего integration-набора. Ни один из этих
наборов не подтверждает реальные платежи, живую базу сервера или готовность
к переключению окружения.

## Дополнительные EVO revisions

`evo_0107` заполняет nullable `legacy_claim_prefix` только у уже существующих
подарков, из первых 12 символов старого token. Индекс неуникальный: историческая
коллизия остаётся видимой, resolver отказывает при неоднозначности. Новые
подарки сохраняют отдельный случайный claim_code; legacy prefix остаётся NULL.

`evo_0108` задаёт 54 server defaults из фактически обновлённой схемы и создаёт
три индекса по id, отсутствующие в старом DDL. Уже существующие индексы
принимаются только после проверки структуры. Named unique constraints и
выключенный reminder сходятся и для pinned upstream metadata bootstrap.
Existing rows не переписываются. Nullable двух новых upstream payment полей
приводится к их реальным NOT NULL DDL только при отсутствии NULL; наличие
неопределённого is_paid останавливает переход без подстановки значения.
Обратный переход к двум исходным профилям нельзя однозначно восстановить из
версии: downgrade этой convergence-ревизии требует согласованного snapshot
вместо угадывания прежних defaults.

`evo_0109` добавляет `traffic_notification_states.panel_user_id` без defaults
и backfill: numeric identity должен подтвердить runtime, а не угадывать DDL.
