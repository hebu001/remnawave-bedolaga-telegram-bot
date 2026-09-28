"""Join the preserved custom schema and pinned upstream v4.15.0 history.

Both parent branches must execute. This revision never stamps over their DDL.
"""

revision = 'evo_merge_4_15_0'
down_revision = ('0127', 'evo_0106')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
