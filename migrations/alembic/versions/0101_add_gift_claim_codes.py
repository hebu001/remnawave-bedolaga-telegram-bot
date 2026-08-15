"""add short exact-match claim codes for gifts

Revision ID: 0101
Revises: 0100
"""

import secrets

import sqlalchemy as sa
from alembic import op


revision = '0101'
down_revision = '0100'
branch_labels = None
depends_on = None

_CLAIM_CODE_BYTES = 16


def _new_claim_code() -> str:
    return secrets.token_urlsafe(_CLAIM_CODE_BYTES)


def upgrade() -> None:
    op.add_column('guest_purchases', sa.Column('claim_code', sa.String(length=22), nullable=True))
    op.create_index('ix_guest_purchases_claim_code', 'guest_purchases', ['claim_code'], unique=True)

    connection = op.get_bind()
    gift_ids = connection.execute(
        sa.text('SELECT id FROM guest_purchases WHERE is_gift = true AND claim_code IS NULL')
    ).scalars()
    used: set[str] = set()
    for purchase_id in gift_ids:
        claim_code = _new_claim_code()
        while claim_code in used:
            claim_code = _new_claim_code()
        used.add(claim_code)
        connection.execute(
            sa.text('UPDATE guest_purchases SET claim_code = :claim_code WHERE id = :purchase_id'),
            {'claim_code': claim_code, 'purchase_id': purchase_id},
        )


def downgrade() -> None:
    op.drop_index('ix_guest_purchases_claim_code', table_name='guest_purchases')
    op.drop_column('guest_purchases', 'claim_code')
