"""Converge old metadata bootstraps with the preserved migration defaults.

Revision ID: evo_0108
Revises: evo_0107

Defaults below are frozen from the upgraded 4.15.0 schema. They do not rewrite
existing rows. Model defaults and constraints match these same DDL contracts.
"""

import sqlalchemy as sa
from alembic import op
from alembic.script import ScriptDirectory

revision = 'evo_0108'
down_revision = 'evo_0107'
branch_labels = None
depends_on = None

_DEFAULTS = (
    ('coupon_batches', 'max_per_user', '0'),
    ('email_queue', 'attempts', '0'),
    ('email_queue', 'status', "'pending'::character varying"),
    ('lava_subscriptions', 'charges_failed', '0'),
    ('lava_subscriptions', 'charges_success', '0'),
    ('lava_subscriptions', 'created_at', 'now()'),
    ('lava_subscriptions', 'currency', "'RUB'::character varying"),
    ('lava_subscriptions', 'status', "'PENDING'::character varying"),
    ('lava_subscriptions', 'updated_at', 'now()'),
    ('legal_consents', 'accepted_at', 'now()'),
    ('paritypay_payments', 'created_at', 'now()'),
    ('paritypay_payments', 'currency', "'RUB'::character varying"),
    ('paritypay_payments', 'is_paid', 'false'),
    ('paritypay_payments', 'status', "'pending'::character varying"),
    ('paritypay_payments', 'updated_at', 'now()'),
    ('reachability_batches', 'created_at', 'now()'),
    ('reachability_batches', 'status', "'pending'::character varying"),
    ('reachability_batches', 'total_targets', '0'),
    ('reachability_batches', 'updated_at', 'now()'),
    ('reachability_jobs', 'attempts', '0'),
    ('reachability_jobs', 'created_at', 'now()'),
    ('reachability_jobs', 'dpi', "'on'::character varying"),
    ('reachability_jobs', 'estimate_is_exact', 'true'),
    ('reachability_jobs', 'status', "'pending'::character varying"),
    ('reachability_jobs', 'trigger', "'manual'::character varying"),
    ('reachability_jobs', 'updated_at', 'now()'),
    ('reachability_target_prefs', 'excluded', 'false'),
    ('reachability_target_prefs', 'purpose', "'unknown'::character varying"),
    ('reachability_target_prefs', 'updated_at', 'now()'),
    ('renewal_sync_tasks', 'attempts', '0'),
    ('renewal_sync_tasks', 'next_attempt_at', 'now()'),
    ('renewal_sync_tasks', 'reset_devices', 'false'),
    ('renewal_sync_tasks', 'reset_traffic', 'false'),
    ('renewal_sync_tasks', 'status', "'pending'::character varying"),
    ('renewal_sync_tasks', 'sync_squads', 'false'),
    ('renewal_sync_tasks', 'updated_at', 'now()'),
    ('renewal_sync_tasks', 'version', '1'),
    ('subpage_invoices', 'attempts', '0'),
    ('subpage_invoices', 'created_at', 'now()'),
    ('subpage_invoices', 'currency', "'RUB'::character varying"),
    ('subpage_invoices', 'next_attempt_at', 'now()'),
    ('subpage_invoices', 'status', "'pending'::character varying"),
    ('subpage_invoices', 'updated_at', 'now()'),
    ('system_error_events', 'delivery_attempts', '0'),
    ('system_error_events', 'delivery_status', "'pending'::character varying"),
    ('system_error_events', 'level', "'error'::character varying"),
    ('tabpay_payments', 'created_at', 'now()'),
    ('tabpay_payments', 'currency', "'RUB'::character varying"),
    ('tabpay_payments', 'is_paid', 'false'),
    ('tabpay_payments', 'is_test', 'false'),
    ('tabpay_payments', 'status', "'pending'::character varying"),
    ('tabpay_payments', 'updated_at', 'now()'),
    ('user_reminders', 'created_at', 'now()'),
    ('user_reminders', 'updated_at', 'now()'),
)


def _check_index(bind, table, name, column, *, unique):
    row = bind.execute(sa.text("""
        SELECT i.indisunique, i.indisprimary, i.indisvalid, i.indisready,
               am.amname, i.indnkeyatts, i.indnatts,
               pg_get_expr(i.indpred, i.indrelid) AS predicate,
               pg_get_expr(i.indexprs, i.indrelid) AS expressions,
               a.attname AS column_name
        FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid
        JOIN pg_class t ON t.oid=i.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace
        JOIN pg_am am ON am.oid=idx.relam
        LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=i.indkey[0]
        WHERE n.nspname=current_schema() AND t.relname=:table AND idx.relname=:name
    """), {'table': table, 'name': name}).first()
    if row is None:
        return False
    if tuple(row) != (unique, False, True, True, 'btree', 1, 1, None, None, column):
        raise RuntimeError(f'Unexpected index shape: {table}.{name}; inspect before migration')
    return True


def _ensure_unique(bind, table, name, column):
    constraints = {item['name']: item for item in sa.inspect(bind).get_unique_constraints(table)}
    existing = constraints.get(name)
    if existing:
        if existing['column_names'] != [column]:
            raise RuntimeError(f'Unexpected unique constraint shape: {table}.{name}')
    else:
        op.create_unique_constraint(name, table, [column])


def _seed_builtin_reminder(bind):
    # The pinned upstream metadata bootstrap stamps 0127 without its seed.
    # Reuse its immutable data, not application models or a replay of its DDL.
    seed = ScriptDirectory.from_config(op.get_context().config).get_revision('0127').module.BUILTIN_LINK_AUTH
    if bind.execute(sa.text('SELECT 1 FROM user_reminders WHERE builtin_key=:key'),
                    {'key': seed['builtin_key']}).first() is not None:
        return
    table = sa.table(
        'user_reminders',
        sa.column('name', sa.String()), sa.column('is_active', sa.Boolean()),
        sa.column('builtin_key', sa.String()), sa.column('channels', sa.String()),
        sa.column('category', sa.String()), sa.column('conditions', sa.JSON()),
        sa.column('repeat_every_days', sa.Integer()), sa.column('max_sends', sa.Integer()),
        sa.column('button_kind', sa.String()), sa.column('button_target', sa.String()),
        sa.column('texts', sa.JSON()),
    )
    op.bulk_insert(table, [seed])


def upgrade() -> None:
    bind = op.get_bind()
    # Upstream metadata allowed NULL where upstream migrations used NOT NULL.
    # Never infer whether an ambiguous historical payment was paid.
    nullable_payments = []
    for table in ('tabpay_payments', 'paritypay_payments'):
        column = next(c for c in sa.inspect(bind).get_columns(table) if c['name'] == 'is_paid')
        if column['nullable']:
            if bind.execute(sa.text(f'SELECT 1 FROM {table} WHERE is_paid IS NULL LIMIT 1')).first():
                raise RuntimeError(f'Cannot infer {table}.is_paid for existing NULL; inspect the payment record')
            nullable_payments.append(table)
    for table, column, default in _DEFAULTS:
        op.alter_column(table, column, server_default=sa.text(default))
    for table in nullable_payments:
        op.alter_column(table, 'is_paid', nullable=False)
    for table in ('email_queue', 'lava_subscriptions', 'system_error_events'):
        name = f'ix_{table}_id'
        if not _check_index(bind, table, name, 'id', unique=False):
            op.create_index(name, table, ['id'])
    for table, name, column in (
        ('lava_subscriptions', 'uq_lava_subscriptions_lava_id', 'lava_subscription_id'),
        ('lava_subscriptions', 'uq_lava_subscriptions_order_id', 'order_id'),
        ('system_error_events', 'system_error_events_event_uid_key', 'event_uid'),
    ):
        _ensure_unique(bind, table, name, column)
    # Replace U metadata's equivalent standalone indexes only after the named
    # unique constraints are installed. No uniqueness protection is removed.
    for column in ('lava_subscription_id', 'order_id'):
        name = f'ix_lava_subscriptions_{column}'
        if _check_index(bind, 'lava_subscriptions', name, column, unique=True):
            op.drop_index(name, table_name='lava_subscriptions')
    _seed_builtin_reminder(bind)


def downgrade() -> None:
    # Both old fork profiles are supported and had different defaults. A blind
    # downgrade cannot recover which profile was the source of an installation.
    raise RuntimeError('Restore the pre-upgrade database snapshot to reverse schema convergence')
