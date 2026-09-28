"""Preserve already-issued short gift token aliases without exposing new tokens.

Revision ID: evo_0107
Revises: evo_merge_4_15_0
"""

import sqlalchemy as sa
from alembic import op


revision = 'evo_0107'
down_revision = 'evo_merge_4_15_0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('guest_purchases', sa.Column('legacy_claim_prefix', sa.String(12), nullable=True))
    op.create_index('ix_guest_purchases_legacy_claim_prefix', 'guest_purchases', ['legacy_claim_prefix'])
    # Only rows present at the transition receive an alias. Collisions deliberately
    # remain representable, so the resolver can reject ambiguity rather than choose.
    op.execute(sa.text(
        "UPDATE guest_purchases SET legacy_claim_prefix=substr(token,1,12) "
        "WHERE is_gift=true AND length(token)>=12"
    ))


def downgrade() -> None:
    op.drop_index('ix_guest_purchases_legacy_claim_prefix', table_name='guest_purchases')
    op.drop_column('guest_purchases', 'legacy_claim_prefix')
