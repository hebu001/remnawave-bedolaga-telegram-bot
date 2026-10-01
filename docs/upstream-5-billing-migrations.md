# Preserved billing and schema integration for 5.0.0

The deployed PostgreSQL 15 fork head `evo_0109` and immutable upstream `0131`
join at `evo_merge_5_0_0`. New `evo_0110` aligns server defaults and named unique
constraints of new Cashera/DPI tables, plus model ID indexes. Historical
`0128`–`0131`, the fork revisions and frozen schema profiles remain untouched.
Startup still applies pending DDL through Alembic, keeps the fork bridge and
bootstrap/preflight/lock guards, and has one head. Fresh models and upgrades
share the same defaults, indexes, foreign keys and disabled reminder seed.

`CasheraPayment.is_paid` is non-null to match upstream DDL. Schema convergence
refuses a historical ambiguous NULL instead of inventing whether money arrived.
Equivalent metadata unique indexes are replaced only after installing the
upstream named constraint; uniqueness protection remains present.

Cashera recurring stages `RenewalSyncTask` in the same transaction as the
subscription extension, charge marker and ledger entry. Reset flags follow
`RESET_TRAFFIC_ON_PAYMENT` (including active paid subscriptions) and
`RESET_DEVICES_ON_RENEWAL`; squad synchronization is requested. Inline delivery
uses a bounded fresh-session worker after commit. Failed delivery and process
restarts leave the task pending. Duplicate callbacks preserve the existing task
and do not extend or charge again. A newer charge during an old worker is
protected by the existing task version check. Cashera remains disabled by default.

The income receipt contract retains WATA/YooKassa and adds Cashera one-time
gifts/top-ups once; recurring charge ledger rows without a one-time receipt
remain income. Existing deposit and subscription debit entries remain intact.
Cashera has not been added to the Subpage provider allowlist.

Synthetic regression sources:

- `test_merged_runtime_migrations.py`: full catalog parity, historical fixture
  preservation, actual incremental `evo_0109` upgrade, repeat upgrade and JSON
  roundtrips of the new FK-linked models.
- `test_cashera_renewal_sync.py`: real PostgreSQL commit rollback/replay, duplicate
  charge race, panel failure/restart and newer-charge worker race.
- Income and endpoint tests: retained WATA/YooKassa boundaries plus Cashera
  receipts/gifts/deposit-debit/recurring/refund gross-income behavior.

This source integration alone does not prove a live payment, provider callback,
remote panel reset or native backup restoration. External calls are mocked and
network connections are guarded during these tests.
