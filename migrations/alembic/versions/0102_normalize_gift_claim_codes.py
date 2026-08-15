"""normalize gift claim codes to 12 characters

Revision ID: 0102
Revises: 0101
"""

import secrets

import sqlalchemy as sa
from alembic import op


revision = '0102'
down_revision = '0101'
branch_labels = None
depends_on = None

_CLAIM_CODE_BYTES = 9


def _new_claim_code() -> str:
    return secrets.token_urlsafe(_CLAIM_CODE_BYTES)


def upgrade() -> None:
    connection = op.get_bind()
    gift_ids = list(connection.execute(sa.text('SELECT id FROM guest_purchases WHERE is_gift = true')).scalars())

    # Old 22-character claim-code links are intentionally invalidated. Clearing
    # first avoids unique-index conflicts while every gift receives a new code.
    connection.execute(sa.text('UPDATE guest_purchases SET claim_code = NULL WHERE is_gift = true'))
    used = set(
        connection.execute(sa.text('SELECT claim_code FROM guest_purchases WHERE claim_code IS NOT NULL')).scalars()
    )

    for purchase_id in gift_ids:
        claim_code = _new_claim_code()
        while claim_code in used:
            claim_code = _new_claim_code()
        used.add(claim_code)
        connection.execute(
            sa.text('UPDATE guest_purchases SET claim_code = :claim_code WHERE id = :purchase_id'),
            {'claim_code': claim_code, 'purchase_id': purchase_id},
        )

    op.alter_column(
        'guest_purchases',
        'claim_code',
        existing_type=sa.String(length=22),
        type_=sa.String(length=12),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        'guest_purchases',
        'claim_code',
        existing_type=sa.String(length=12),
        type_=sa.String(length=22),
        existing_nullable=True,
    )
