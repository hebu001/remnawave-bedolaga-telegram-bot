"""Persist subscription-page orders and paid-renewal panel intents.

Revision ID: 0103
Revises: 0102
"""

import sqlalchemy as sa
from alembic import op


revision = '0103'
down_revision = '0102'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'subpage_invoices',
        sa.Column('token', sa.String(128), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='SET NULL')),
        sa.Column('short_uuid', sa.String(64), nullable=False),
        sa.Column('period_days', sa.Integer(), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False, server_default='RUB'),
        sa.Column('method', sa.String(32), nullable=False),
        sa.Column('provider_payment_id', sa.String(255)),
        sa.Column('local_payment_id', sa.Integer()),
        sa.Column('configuration', sa.JSON()),
        sa.Column('pricing', sa.JSON()),
        sa.Column('status', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('reason', sa.String(64)),
        sa.Column('paid_amount_kopeks', sa.Integer()),
        sa.Column('deposit_transaction_id', sa.Integer(), sa.ForeignKey('transactions.id', ondelete='SET NULL')),
        sa.Column('renewal_transaction_id', sa.Integer(), sa.ForeignKey('transactions.id', ondelete='SET NULL')),
        sa.Column('new_expires_at', sa.DateTime(timezone=True)),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('method', 'provider_payment_id', name='uq_subpage_invoice_provider_payment'),
        sa.CheckConstraint('amount_kopeks > 0 AND period_days > 0', name='ck_subpage_invoice_positive'),
    )
    op.create_index('ix_subpage_invoices_recovery', 'subpage_invoices', ['status', 'next_attempt_at'])
    op.create_table(
        'renewal_sync_tasks',
        sa.Column(
            'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='CASCADE'), primary_key=True
        ),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('reset_traffic', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('reset_devices', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('sync_squads', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.String(255)),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_renewal_sync_tasks_due', 'renewal_sync_tasks', ['status', 'next_attempt_at'])


def downgrade() -> None:
    # Do not silently discard captured money or pending panel work during rollback.
    connection = op.get_bind()
    pending = connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM subpage_invoices WHERE status IN ('pending', 'paid', 'review')) "
            "OR EXISTS (SELECT 1 FROM renewal_sync_tasks WHERE status <> 'done')"
        )
    ).scalar()
    if pending:
        raise RuntimeError('Cannot drop durable orders/renewal intents while work remains pending')
    op.drop_table('renewal_sync_tasks')
    op.drop_table('subpage_invoices')
