"""Upgrade SQLite databases, including the pre-Alembic local schema."""
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect


def upgrade_local(engine):
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    config = Config('alembic.ini')
    with engine.begin() as connection:
        config.attributes['connection'] = connection
        if tables and 'alembic_version' not in tables:
            required = {'users', 'preferences', 'offers', 'invitations', 'magic_links', 'user_sessions', 'user_offers', 'deliveries', 'notion_connections', 'notion_syncs'}
            if not required.issubset(tables):
                raise RuntimeError('Unrecognized local database schema; preserve it and migrate manually')
            if 'offer_sources' in tables:
                raise RuntimeError('Unversioned new schema; verify with Alembic before stamping')
            revision = '20260906_0003' if 'auth_limits' in tables else '20260904_0001'
            command.stamp(config, revision)
        command.upgrade(config, 'head')
