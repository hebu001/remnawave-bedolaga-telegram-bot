"""Index notification lookups without rewriting or deduplicating history.

Revision ID: 0105
Revises: 0104
"""

import sqlalchemy as sa
from alembic import op


revision = '0105'
down_revision = '0104'
branch_labels = None
depends_on = None

INDEX_NAME = 'ix_sent_notifications_lookup'


def upgrade() -> None:
    context = op.get_context()
    if context.dialect.name == 'postgresql':
        # A failed concurrent build can leave an invalid index behind. Repair
        # that artifact on retry; IF NOT EXISTS alone would silently keep it.
        with context.autocommit_block():
            if not context.as_sql:
                valid = op.get_bind().scalar(
                    sa.text('SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)'),
                    {'name': INDEX_NAME},
                )
                if valid is False:
                    op.drop_index(INDEX_NAME, postgresql_concurrently=True, if_exists=True)
            op.create_index(
                INDEX_NAME,
                'sent_notifications',
                ['subscription_id', 'user_id', 'notification_type', 'days_before'],
                unique=False,
                postgresql_concurrently=True,
                if_not_exists=True,
            )
    else:
        op.create_index(
            INDEX_NAME,
            'sent_notifications',
            ['subscription_id', 'user_id', 'notification_type', 'days_before'],
            unique=False,
            if_not_exists=True,
        )


def downgrade() -> None:
    context = op.get_context()
    if context.dialect.name == 'postgresql':
        with context.autocommit_block():
            op.drop_index(INDEX_NAME, postgresql_concurrently=True, if_exists=True)
    else:
        op.drop_index(INDEX_NAME, if_exists=True)
