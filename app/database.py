import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def _db_url() -> str:
    return os.getenv("DATABASE_URL", "sqlite:////data/app.db")


DATABASE_URL = _db_url()

_IS_SQLITE = DATABASE_URL.startswith("sqlite:")
connect_args = {"check_same_thread": False} if _IS_SQLITE else {}
engine = create_engine(DATABASE_URL, future=True, connect_args=connect_args)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    if _IS_SQLITE:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
