"""Bind traffic notification state to a verified numeric panel account.

Revision ID: evo_0109
Revises: evo_0108
"""

import sqlalchemy as sa
from alembic import op

revision = 'evo_0109'
down_revision = 'evo_0108'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing state remains unbound until a verified runtime observation.
    op.add_column('traffic_notification_states', sa.Column('panel_user_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('traffic_notification_states', 'panel_user_id')
