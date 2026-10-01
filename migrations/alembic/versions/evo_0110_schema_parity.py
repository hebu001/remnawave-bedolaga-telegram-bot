"""Converge metadata bootstraps and immutable upstream 5.0.0 DDL.

Revision ID: evo_0110
Revises: evo_merge_5_0_0

The defaults and named uniqueness constraints below are frozen from 0128–0131.
No existing data is inferred or rewritten. Fresh metadata uses the same schema;
this revision also supports older upstream metadata bootstraps with these tables.
"""

import sqlalchemy as sa
from alembic import op


revision = 'evo_0110'
down_revision = 'evo_merge_5_0_0'
branch_labels = None
depends_on = None

_DEFAULTS = (
    ('cashera_payments', 'currency', "'RUB'::character varying"),
    ('cashera_payments', 'status', "'pending'::character varying"),
    ('cashera_payments', 'is_paid', 'false'),
    ('cashera_payments', 'created_at', 'now()'),
    ('cashera_payments', 'updated_at', 'now()'),
    ('cashera_subscriptions', 'currency', "'RUB'::character varying"),
    ('cashera_subscriptions', 'status', "'PENDING'::character varying"),
    ('cashera_subscriptions', 'charges_success', '0'),
    ('cashera_subscriptions', 'charges_failed', '0'),
    ('cashera_subscriptions', 'created_at', 'now()'),
    ('cashera_subscriptions', 'updated_at', 'now()'),
    ('dpichecker_actions', 'status', "'submitting'::character varying"),
    ('dpichecker_actions', 'pop_count', '0'),
    ('dpichecker_actions', 'resource_count', '0'),
    ('dpichecker_actions', 'source', "'paste'::character varying"),
    ('dpichecker_actions', 'label', "''::character varying"),
    ('dpichecker_actions', 'created_at', 'now()'),
    ('dpichecker_actions', 'updated_at', 'now()'),
)


def _check_index(bind, table, name, column, *, unique):
    row = bind.execute(
        sa.text("""
        SELECT i.indisunique, i.indisprimary, i.indisvalid, i.indisready,
               am.amname, i.indnkeyatts, i.indnatts,
               pg_get_expr(i.indpred, i.indrelid), pg_get_expr(i.indexprs, i.indrelid),
               a.attname
        FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid
        JOIN pg_class t ON t.oid=i.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace
        JOIN pg_am am ON am.oid=idx.relam
        LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=i.indkey[0]
        WHERE n.nspname=current_schema() AND t.relname=:table AND idx.relname=:name
    """),
        {'table': table, 'name': name},
    ).first()
    if row is None:
        return False
    if tuple(row) != (unique, False, True, True, 'btree', 1, 1, None, None, column):
        raise RuntimeError(f'Unexpected index shape: {table}.{name}; inspect before migration')
    return True


def upgrade() -> None:
    bind = op.get_bind()
    columns = sa.inspect(bind).get_columns('cashera_payments')
    paid_column = next(column for column in columns if column['name'] == 'is_paid')
    if (
        paid_column['nullable']
        and bind.execute(sa.text('SELECT 1 FROM cashera_payments WHERE is_paid IS NULL LIMIT 1')).first()
    ):
        raise RuntimeError('Cannot infer cashera_payments.is_paid for existing NULL; inspect the payment record')
    for table, column, default in _DEFAULTS:
        op.alter_column(table, column, server_default=sa.text(default))
    if paid_column['nullable']:
        op.alter_column('cashera_payments', 'is_paid', nullable=False)
    for table in ('cashera_payments', 'cashera_subscriptions'):
        name = f'ix_{table}_id'
        if not _check_index(bind, table, name, 'id', unique=False):
            op.create_index(name, table, ['id'])
    table = 'cashera_subscriptions'
    constraints = {item['name']: item for item in sa.inspect(bind).get_unique_constraints(table)}
    for name, column in (
        ('uq_cashera_subscriptions_uuid', 'cashera_subscription_uuid'),
        ('uq_cashera_subscriptions_external_id', 'external_id'),
    ):
        existing = constraints.get(name)
        if existing is not None and existing['column_names'] != [column]:
            raise RuntimeError(f'Unexpected unique constraint shape: {table}.{name}')
        if existing is None:
            op.create_unique_constraint(name, table, [column])
        # Remove equivalent metadata-only indexes after installing the named
        # constraint; uniqueness is never temporarily absent.
        index = f'ix_{table}_{column}'
        if _check_index(bind, table, index, column, unique=True):
            op.drop_index(index, table_name=table)


def downgrade() -> None:
    raise RuntimeError('Restore the pre-upgrade database snapshot to reverse schema convergence')
