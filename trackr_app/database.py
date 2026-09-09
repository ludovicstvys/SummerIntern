from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
pool_options = {}
if not settings.database_url.startswith('sqlite'):
    connect_args.update(connect_timeout=settings.db_connect_timeout)
    pool_options.update(pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow,
                        pool_timeout=settings.db_pool_timeout)
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args, **pool_options)
if not settings.database_url.startswith('sqlite'):
    from sqlalchemy import event
    @event.listens_for(engine, 'begin')
    def transaction_limits(connection):
        connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{settings.db_lock_timeout_ms}ms'")
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

