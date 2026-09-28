"""Persist traffic notification thresholds across restarts and calendar days.

Revision ID: evo_0106
Revises: evo_0105
"""

import sqlalchemy as sa
from alembic import op

revision = 'evo_0106'
down_revision = 'evo_0105'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'traffic_notification_states',
        sa.Column(
            'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='CASCADE'), primary_key=True
        ),
        sa.Column('cycle_key', sa.String(64), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('highest_threshold', sa.Integer(), nullable=False),
        sa.Column('used_bytes', sa.BigInteger(), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('delivery_status', sa.String(20), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_table('traffic_notification_states')
