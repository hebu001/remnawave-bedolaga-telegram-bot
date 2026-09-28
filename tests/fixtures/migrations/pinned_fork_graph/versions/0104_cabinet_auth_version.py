"""Invalidate all cabinet credentials after a password reset.

Revision ID: 0104
Revises: 0103
"""

import sqlalchemy as sa
from alembic import op


revision = '0104'
down_revision = '0103'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('cabinet_auth_version', sa.Integer(), nullable=False, server_default='0'))
    op.create_table(
        'cabinet_ws_tickets',
        sa.Column('token_hash', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('auth_version', sa.Integer(), nullable=False),
        sa.Column('origin', sa.String(512), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('access_expires_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_cabinet_ws_tickets_user', 'cabinet_ws_tickets', ['user_id'])
    op.create_index('ix_cabinet_ws_tickets_expires', 'cabinet_ws_tickets', ['expires_at'])


def downgrade() -> None:
    if op.get_bind().execute(sa.text('SELECT EXISTS (SELECT 1 FROM users WHERE cabinet_auth_version <> 0)')).scalar():
        raise RuntimeError('Cannot discard credential revocations; keep auth version when rolling back')
    op.drop_table('cabinet_ws_tickets')
    op.drop_column('users', 'cabinet_auth_version')
