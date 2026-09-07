"""Source memberships, durable invitations and worker diagnostics."""
from alembic import op
import sqlalchemy as sa

revision = '20260907_0004'
down_revision = '20260906_0003'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('offer_sources',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('offer_id', sa.Integer(), sa.ForeignKey('offers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('region', sa.String(50), nullable=False),
        sa.Column('programme_type', sa.String(30), nullable=False),
        sa.Column('season', sa.String(20), nullable=False),
        sa.Column('start_term', sa.String(100)),
        sa.Column('opening_date', sa.Date()), sa.Column('closing_date', sa.Date()),
        sa.Column('is_open', sa.Boolean(), nullable=False),
        sa.Column('missing_collections', sa.Integer(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('offer_id', 'region', 'programme_type', 'season'))
    op.create_index('ix_offer_sources_offer_id', 'offer_sources', ['offer_id'])
    # Existing application and workflows exclusively collected the 2027 season.
    op.execute("INSERT INTO offer_sources (offer_id, region, programme_type, season, start_term, opening_date, closing_date, is_open, missing_collections, last_seen_at) SELECT id, region, programme_type, '2027', start_term, opening_date, closing_date, is_open, missing_collections, last_seen_at FROM offers")
    op.add_column('invitations', sa.Column('delivery_status', sa.String(20), nullable=False, server_default='pending'))
    op.add_column('invitations', sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('invitations', sa.Column('last_error', sa.Text()))
    op.add_column('invitations', sa.Column('next_attempt_at', sa.DateTime(timezone=True)))
    # Never resend historical invitations as a side effect of migration.
    op.execute("UPDATE invitations SET delivery_status = 'sent'")
    for table in ('deliveries', 'notion_syncs'):
        op.add_column(table, sa.Column('next_attempt_at', sa.DateTime(timezone=True)))
    op.add_column('notion_connections', sa.Column('setup_status', sa.String(20), nullable=False, server_default='idle'))
    op.add_column('notion_connections', sa.Column('parent_page_id', sa.String(100)))
    op.create_table('worker_states', sa.Column('key', sa.String(160), primary_key=True), sa.Column('last_success_at', sa.DateTime(timezone=True)), sa.Column('last_error', sa.Text()))
    op.create_table('legacy_tasks', sa.Column('key', sa.String(64), primary_key=True),
        sa.Column('source', sa.String(160), nullable=False), sa.Column('channel', sa.String(20), nullable=False),
        sa.Column('recipient', sa.String(320), nullable=False), sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False), sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text()), sa.Column('next_attempt_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_legacy_tasks_source', 'legacy_tasks', ['source'])
    op.create_index('ix_legacy_tasks_status', 'legacy_tasks', ['status'])


def downgrade():
    op.drop_table('legacy_tasks')
    op.drop_table('worker_states')
    op.drop_column('notion_connections', 'parent_page_id')
    op.drop_column('notion_connections', 'setup_status')
    for table in ('deliveries', 'notion_syncs'):
        op.drop_column(table, 'next_attempt_at')
    for column in ('next_attempt_at', 'last_error', 'attempts', 'delivery_status'):
        op.drop_column('invitations', column)
    op.drop_table('offer_sources')
