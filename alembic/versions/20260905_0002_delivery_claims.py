"""Add reliable digest and delivery claim state.

Revision ID: 20260905_0002
Revises: 20260904_0001
"""
from alembic import op
import sqlalchemy as sa

revision = "20260905_0002"
down_revision = "20260904_0001"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    preference_columns = {column["name"] for column in inspector.get_columns("preferences")}
    delivery_columns = {column["name"] for column in inspector.get_columns("deliveries")}
    if "last_digest_date" not in preference_columns:
        op.add_column("preferences", sa.Column("last_digest_date", sa.Date(), nullable=True))
    if "processing_started_at" not in delivery_columns:
        op.add_column("deliveries", sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    preference_columns = {column["name"] for column in inspector.get_columns("preferences")}
    delivery_columns = {column["name"] for column in inspector.get_columns("deliveries")}
    if "processing_started_at" in delivery_columns:
        op.drop_column("deliveries", "processing_started_at")
    if "last_digest_date" in preference_columns:
        op.drop_column("preferences", "last_digest_date")
