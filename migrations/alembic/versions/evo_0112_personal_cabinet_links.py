"""Create one revocable personal link selector for every cabinet user."""

import sqlalchemy as sa
from alembic import op


revision = 'evo_0112'
down_revision = 'evo_0111'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        op.add_column('users', sa.Column('cabinet_personal_link_id', sa.Uuid(), nullable=True))
        op.add_column(
            'users', sa.Column('cabinet_personal_link_key_version', sa.Integer(), nullable=False, server_default='1')
        )
        op.execute('UPDATE users SET cabinet_personal_link_id = lower(hex(randomblob(16)))')
        with op.batch_alter_table('users') as batch:
            batch.alter_column(
                'cabinet_personal_link_id',
                existing_type=sa.Uuid(),
                nullable=False,
                server_default=sa.text('(lower(hex(randomblob(16))))'),
            )
        op.create_index('ix_users_cabinet_personal_link_id', 'users', ['cabinet_personal_link_id'], unique=True)
        op.add_column('cabinet_ws_tickets', sa.Column('personal_link_id', sa.Uuid(), nullable=True))
        return

    # The migration runner holds a session-level advisory lock. Commit before the
    # backfill to release the DDL lock and let new users pick up the database default.
    # On a restart, leave all previously assigned selectors unchanged.
    op.execute(
        'ALTER TABLE users ADD COLUMN IF NOT EXISTS cabinet_personal_link_id uuid, '
        'ADD COLUMN IF NOT EXISTS cabinet_personal_link_key_version integer NOT NULL DEFAULT 1'
    )
    op.execute('ALTER TABLE users ALTER COLUMN cabinet_personal_link_id SET DEFAULT gen_random_uuid()')
    op.execute('ALTER TABLE cabinet_ws_tickets ADD COLUMN IF NOT EXISTS personal_link_id uuid')
    with op.get_context().autocommit_block():
        last_id = None
        while True:
            cursor_filter = '' if last_id is None else ' AND id > :last_id'
            batch = sa.text(
                'WITH batch AS (SELECT id FROM users WHERE cabinet_personal_link_id IS NULL'
                + cursor_filter
                + ' ORDER BY id LIMIT 2000), '
                'assigned AS (UPDATE users u SET cabinet_personal_link_id = gen_random_uuid() FROM batch '
                'WHERE u.id = batch.id AND u.cabinet_personal_link_id IS NULL RETURNING u.id) '
                'SELECT max(id) FROM assigned'
            )
            last_id = bind.execute(batch, {'last_id': last_id} if last_id is not None else {}).scalar_one()
            if last_id is None:
                break
    # Index + final constraints + Alembic stamp are committed together. If these
    # fail, a retry only checks the already backfilled rows.
    op.create_index('ix_users_cabinet_personal_link_id', 'users', ['cabinet_personal_link_id'], unique=True)
    op.alter_column('users', 'cabinet_personal_link_id', existing_type=sa.Uuid(), nullable=False)


def downgrade() -> None:
    raise RuntimeError('Dropping permanent personal links would invalidate saved bookmarks; restore a verified backup.')
