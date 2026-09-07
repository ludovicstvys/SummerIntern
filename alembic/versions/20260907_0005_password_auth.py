"""Password credentials, one-time recovery tokens and sliding sessions."""
from alembic import op
import sqlalchemy as sa

revision = '20260907_0005'
down_revision = '20260907_0004'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('password_hash', sa.String(255)))
    op.add_column('user_sessions', sa.Column('renewed_at', sa.DateTime(timezone=True)))
    op.create_table('password_tokens',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('token_hash', sa.String(64), unique=True, nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_password_tokens_user_id', 'password_tokens', ['user_id'])


def downgrade():
    op.drop_table('password_tokens')
    op.drop_column('user_sessions', 'renewed_at')
    op.drop_column('users', 'password_hash')
