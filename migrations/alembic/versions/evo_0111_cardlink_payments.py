"""Add Cardlink receipts without changing existing payments.

Revision ID: evo_0111
Revises: evo_0110
"""

import sqlalchemy as sa
from alembic import op

revision = 'evo_0111'
down_revision = 'evo_0110'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cardlink_payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('bill_id', sa.String(64), nullable=False),
        sa.Column('order_id', sa.String(64), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(10), nullable=False, server_default='RUB'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(32), nullable=False, server_default='NEW'),
        sa.Column('is_paid', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('url', sa.Text(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('callback_payload', sa.JSON(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    for column in ('id', 'user_id', 'bill_id', 'order_id'):
        op.create_index(
            f'ix_cardlink_payments_{column}', 'cardlink_payments', [column], unique=column in ('bill_id', 'order_id')
        )


def downgrade() -> None:
    # Never discard financial receipts as part of an application rollback.
    raise RuntimeError('Restore a verified database backup to remove Cardlink receipts')
