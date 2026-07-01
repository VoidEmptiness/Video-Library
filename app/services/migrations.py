from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from ..database import engine
from ..models import Base

logger = logging.getLogger(__name__)


def _migrate_db() -> None:
    try:
        inspector = inspect(engine)
        columns = [col["name"] for col in inspector.get_columns("folders")]
        if "parent_id" not in columns:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE folders ADD COLUMN parent_id INTEGER REFERENCES folders(id) ON DELETE SET NULL"
                    )
                )
                conn.commit()
        indexes = [ix["name"] for ix in inspector.get_indexes("folders")]
        for ix_name in indexes:
            if ix_name and "name" in ix_name.lower():
                with engine.connect() as conn:
                    conn.execute(
                        text('DROP INDEX IF EXISTS "%s"' % ix_name.replace('"', '""'))
                    )
                    conn.commit()
                break
    except Exception:
        logger.exception("Migration failed (folders)")

    try:
        inspector = inspect(engine)
        columns = [col["name"] for col in inspector.get_columns("tags")]
        if "description" not in columns:
            with engine.connect() as conn:
                conn.execute(
                    text("ALTER TABLE tags ADD COLUMN description VARCHAR(256)")
                )
                conn.commit()
    except Exception:
        logger.exception("Migration failed (tags)")


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_db()
    from .auth import ensure_secret_key, migrate_env_users

    ensure_secret_key()
    migrate_env_users()
