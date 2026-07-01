from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from itsdangerous import BadSignature, TimestampSigner

from ..database import SessionLocal
from ..models import User


SESSION_COOKIE = "vp_session"


def _settings_dir() -> Path:
    return Path(os.getenv("SETTINGS_DIR", str(Path(__file__).resolve().parent.parent / "data")))


def _secret_key_file() -> Path:
    return _settings_dir() / "secret_key"


def _secret() -> str:
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key
    key_file = _secret_key_file()
    if key_file.exists():
        return key_file.read_text().strip()
    return "dev-secret-change-me"


_signer_instance = None


def _signer() -> TimestampSigner:
    global _signer_instance
    if _signer_instance is None:
        _signer_instance = TimestampSigner(_secret())
    return _signer_instance


def ensure_secret_key() -> str:
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key
    key_file = _secret_key_file()
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_urlsafe(64)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key)
    global _signer_instance
    _signer_instance = None
    return key


def _max_age_seconds() -> int:
    return int(os.getenv("SESSION_MAX_AGE_SECONDS", str(60 * 60 * 24 * 7)))


def _hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or secrets.token_bytes(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)
    return salt, key


def _format_hash(salt: bytes, key: bytes) -> str:
    return salt.hex() + "$" + key.hex()


def _parse_hash(stored: str) -> tuple[bytes, bytes]:
    salt_hex, key_hex = stored.split("$", 1)
    return bytes.fromhex(salt_hex), bytes.fromhex(key_hex)


def hash_password(password: str) -> str:
    salt, key = _hash_password(password)
    return _format_hash(salt, key)


def check_password(password: str, stored_hash: str) -> bool:
    try:
        salt, expected = _parse_hash(stored_hash)
        _, actual = _hash_password(password, salt)
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def get_admin_count() -> int:
    db = SessionLocal()
    try:
        return db.query(User).filter(User.is_admin.is_(True)).count()
    finally:
        db.close()


def has_users() -> bool:
    db = SessionLocal()
    try:
        return db.query(User).count() > 0
    finally:
        db.close()


def create_first_admin(username: str, password: str) -> User:
    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            raise RuntimeError("Users already exist")
        user = User(
            username=username,
            password_hash=hash_password(password),
            is_admin=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    finally:
        db.close()


def verify_credentials(username: str, password: str) -> bool:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            return False
        return check_password(password, user.password_hash)
    finally:
        db.close()


def migrate_env_users() -> None:
    admin_user = os.getenv("ADMIN_USERNAME", "")
    admin_pass = os.getenv("ADMIN_PASSWORD", "")
    db = SessionLocal()
    try:
        if not admin_user or not admin_pass:
            return
        if db.query(User).count() > 0:
            return
        user = User(
            username=admin_user,
            password_hash=hash_password(admin_pass),
            is_admin=True,
        )
        db.add(user)
        db.commit()
    finally:
        db.close()


def create_session_token(username: str) -> str:
    return _signer().sign(username.encode("utf-8")).decode("utf-8")


def parse_session_token(token: str) -> str | None:
    if not token:
        return None
    try:
        value = _signer().unsign(token, max_age=_max_age_seconds())
        return value.decode("utf-8")
    except BadSignature:
        return None


GUEST_USER = "guest"


def is_guest(user: str | None) -> bool:
    return user == GUEST_USER


def session_expiry_dt() -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(seconds=_max_age_seconds())
