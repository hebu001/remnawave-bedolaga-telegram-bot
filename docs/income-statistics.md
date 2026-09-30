# Income statistics: one gateway payment, one income entry

The production fix from 2026-09-16 (`8669ff41`) was applied to Docker image
files, but was not an ancestor of the branch used for the 4.15.0 upgrade.
Keeping its Compose overlay did not preserve those files when a new image
replaced the old image. This fix and its regression tests must live in Git.

`income_payments_query()` is the source for total income, daily income,
the daily chart, payment-method totals and the recent-payments totals:

- WATA: paid RUB gateway records, using `paid_at`.
- YooKassa: paid, succeeded, non-test RUB records, using `captured_at`, with
  the linked transaction's `completed_at` as the historical fallback.
- Other gateways: the existing completed external-transaction source,
  using completion time, or creation time when completion time is missing.

The gateway's deposit and the subsequent subscription debit are both valid
ledger entries. They must not both contribute to cash income. Do not delete
either ledger entry to repair a report. Gateway records also include gifts
without a linked ledger transaction.

The sales summary uses the same income source. Its existing unlinked-gift
fallback remains for other gateways only: WATA/YooKassa gift receipts have
already contributed to income. Sales consumption metrics (subscription
spending, add-ons and manual credits) keep their distinct definitions.

Calendar boundaries and chart grouping use `settings.TIMEZONE` through the
existing local-day and local-date helpers. Revenue is dated by payment,
not by the time an invoice was created.

## Regression coverage

- `tests/database/test_income_statistics.py`: the restored receipt contract,
  including a deposit/debit pair, unlinked gifts, unpaid/test/manual/balance
  exclusions, historical YooKassa dates and Moscow boundaries.
- `tests/cabinet/test_income_receipts_endpoints.py`: real PostgreSQL queries
  for the dashboard, chart, method totals, recent-payment totals and sales
  summary, including gift deduplication and the other-gateway gift fallback.
- Existing local-day tests still cover SQLite and PostgreSQL timezone
  grouping. The new tests are part of `tests/baseline/custom-contracts.json`.

Before production rollout, compare candidate queries with independent SQL
in a forced read-only repeatable-read snapshot. No database migration or
payment-data rewrite is required for this correction.
