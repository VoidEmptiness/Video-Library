import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _db_url() -> str:
    # Default to SQLite stored on the persistent /data volume.
    # Can be overridden with DATABASE_URL for PostgreSQL, etc.
    return os.getenv("DATABASE_URL", "sqlite:////data/app.db")


DATABASE_URL = _db_url()

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite:") else {}
engine = create_engine(DATABASE_URL, future=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
