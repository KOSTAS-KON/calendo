from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import settings


def _normalize_db_url(url: str) -> str:
    """Render/Postgres compatibility.

    Render (and some managed Postgres providers) expose DATABASE_URL as
    ``postgres://...`` which SQLAlchemy does not reliably accept.

    Normalize it to ``postgresql://...``.
    """
    u = (url or "").strip()
    if u.startswith("postgres://"):
        return "postgresql://" + u[len("postgres://") :]
    return u

db_url = _normalize_db_url(settings.DATABASE_URL)

connect_args = {}
if db_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(db_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """
    DB session dependency.
    Roll back on exceptions to avoid InFailedSqlTransaction cascades.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()
