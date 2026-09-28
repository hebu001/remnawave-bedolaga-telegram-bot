# Immutable migration evidence fixtures

Source fork: `4b06edcdce26850c03ca474e8d195ef94ddb347e`.
Source upstream: `877690a7039d1326b2c00eda3e297879b80c0678` (v4.15.0).
All row data is synthetic. These are not exports of a live user database.

`fork_0106_fresh.sql` was rendered from the pinned fork's standalone
`app/database/models.py` with SQLAlchemy PostgreSQL `CreateTable`/`CreateIndex`,
then supplemented with the exact pinned runtime grace function/trigger and
standard Alembic version table. `provenance.json` records the source model,
DDL and original history SHA256. Future test models never construct this fixture.

For the upgraded profile, the helper installs this frozen DDL, removes only
post-0102 custom tables, auth_version and notification lookup index, and marks
the synthetic predecessor 0102. `pinned_fork_graph` contains a non-executable
0102 marker and the **unaltered** real 0103–0106. Actual Alembic planning executes
them (including concurrent-index autocommit), producing the 15 historical server
defaults that differ from a fresh metadata install. This is a synthetic
predecessor, not proof that every historic installation has this exact schema.

`app/database/fork_profiles` holds the resulting PostgreSQL catalogs and hashes.
Tests independently rebuild both fixtures and require exact matches. Changes to
those profiles require an explained provenance change and real PostgreSQL checks;
do not regenerate them from merged/current models to make tests pass.

`fork_0106_seed.sql` covers two users/subscriptions, one tariff, two ledger
entries, gift/non-gift purchases, pending/paid/review/fulfilled invoices,
pending/done jobs, auth generations and tokens, traffic dedup/history, and open
and completed grace sessions. It is explicitly synthetic, with fixed timestamps,
invalid-example domains and known dummy identifiers. Actual table rows and
columns are compared before and after the bridge/upstream chain.

`upstream_4_15_0` vendors exactly 27 unmodified migration files. Its provenance
includes byte SHA256 plus AST hashes for all 100 common migrations. The test
copies the checkout's common and evo histories, validates those hashes, adds
upstream files and a temporary no-op merge head. No Git objects, network fetch
or new application models are needed. The runtime history does not contain that
merge head yet. Existing Ruff exclusion of directories named `migrations` keeps
these immutable migration bytes out of automatic formatting.
