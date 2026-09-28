"""Synthetic predecessor; frozen fresh DDL minus post-0102 objects. Never a live DB export."""
revision = "0102"
down_revision = None
branch_labels = None
depends_on = None
def upgrade():
    raise RuntimeError("Install the pinned synthetic predecessor fixture first")
def downgrade():
    raise RuntimeError("Fixture only")
