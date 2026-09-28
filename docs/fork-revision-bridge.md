# Проверяемый мост ревизий старого форка (локальный прототип)

Этот этап разводит идентификаторы истории. Он **не объединяет** бизнес-код,
модели или API v4.15.0 и не является релизом для работающего сервера.

Шесть кастомных миграций `0101`–`0106` теперь называются `evo_0101`–`evo_0106`.
DDL сохранён; меняются только идентификаторы, связи и документация ревизий.
`evo_0101` продолжает общую `0100`. В настоящем runtime пока одна голова
`evo_0106`; upstream `0101`–`0127` и merge-head существуют только в тестовом
временном графе. Не копировать тестовую merge-head в действующий релиз.

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
Для действительно пустой PostgreSQL текущего форка создание таблиц, guards и
`stamp evo_0106` выполняются в одной транзакции под тем же lock. Повторный
startup после bridge и после fresh bootstrap проверен реальными тестами.
Полный fresh bootstrap будущих объединённых моделей остаётся следующей фазой.

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
профилях. Он не подтверждает merged model parity, Remnawave API 3.x, реальные
платежи, живую базу тестового сервера или готовность прод-релиза.
