"""Join the preserved custom branch and upstream 5.0.0 DDL.

Revision ID: evo_merge_5_0_0
Revises: evo_0109, 0131

Neither lineage is replayed or replaced. Alembic applies all pending revisions
before recording this join.
"""

revision = 'evo_merge_5_0_0'
down_revision = ('evo_0109', '0131')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
