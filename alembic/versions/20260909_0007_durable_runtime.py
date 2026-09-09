"""Add durable runtime tables without changing the 0006 application tables."""
from alembic import op
import sqlalchemy as sa

revision = '20260909_0007'
down_revision = '20260908_0006'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('durable_jobs',
        sa.Column('key', sa.String(160), primary_key=True),
        sa.Column('kind', sa.String(30), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('cursor', sa.Integer(), nullable=False),
        sa.Column('lease_token', sa.String(64)),
        sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True)),
        sa.Column('last_error', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True)))
    op.create_index('ix_durable_jobs_kind', 'durable_jobs', ['kind'])
    op.create_index('ix_durable_jobs_status', 'durable_jobs', ['status'])
    op.create_table('source_snapshots',
        sa.Column('key', sa.String(160), primary_key=True),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('lease_token', sa.String(64)),
        sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('payload', sa.Text()),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
        sa.Column('last_error', sa.Text()))
    op.create_table('oauth_nonces',
        sa.Column('token_hash', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('session_id', sa.Integer(), sa.ForeignKey('user_sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table('oauth_nonces')
    op.drop_table('source_snapshots')
    op.drop_table('durable_jobs')
