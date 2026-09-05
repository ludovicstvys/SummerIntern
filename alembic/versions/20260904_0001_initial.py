"""Initial Trackr Alerts schema."""
from alembic import op
from trackr_app.database import Base
from trackr_app import models  # noqa: F401

revision = "20260904_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(bind=op.get_bind())


def downgrade():
    Base.metadata.drop_all(bind=op.get_bind())

