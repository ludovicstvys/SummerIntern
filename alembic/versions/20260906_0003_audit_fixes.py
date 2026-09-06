"""Shared login limits and confirmed offer closures."""
from alembic import op
import sqlalchemy as sa

revision = "20260906_0003"
down_revision = "20260905_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("offers", sa.Column("missing_collections", sa.Integer(), nullable=False, server_default="0"))
    op.create_table("auth_limits", sa.Column("key", sa.String(64), primary_key=True), sa.Column("count", sa.Integer(), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_auth_limits_expires_at", "auth_limits", ["expires_at"])


def downgrade():
    op.drop_table("auth_limits")
    op.drop_column("offers", "missing_collections")
