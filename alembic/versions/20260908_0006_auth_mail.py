"""Durable authentication email queue."""
from alembic import op
import sqlalchemy as sa

revision = '20260908_0006'
down_revision = '20260907_0005'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('auth_mail',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('email_encrypted', sa.Text(), nullable=False),
        sa.Column('email_hash', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('invitation_id', sa.Integer(), sa.ForeignKey('invitations.id'), unique=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text()),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True)),
        sa.Column('processing_started_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_auth_mail_email_hash', 'auth_mail', ['email_hash'])
    op.create_index('ix_auth_mail_status', 'auth_mail', ['status'])


def downgrade():
    op.drop_table('auth_mail')
