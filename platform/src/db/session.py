"""Database session factory — supports both SQLite (dev) and PostgreSQL (prod)."""
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager

from src.core.config import EFFECTIVE_DB_URL, USE_SQLITE

_connect_args = {"check_same_thread": False} if USE_SQLITE else {}

engine = create_engine(
    EFFECTIVE_DB_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    """Create all tables (dev/test). Use Alembic migrations in production."""
    from src.db.models import Base
    Path("data").mkdir(exist_ok=True)
    Base.metadata.create_all(bind=engine)


@contextmanager
def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
