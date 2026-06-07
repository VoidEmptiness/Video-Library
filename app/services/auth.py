from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

from itsdangerous import BadSignature, TimestampSigner


SESSION_COOKIE = "vp_session"


def _secret() -> str:
    return os.getenv("SECRET_KEY", "dev-secret-change-me")


_signer = TimestampSigner(_secret())


def _max_age_seconds() -> int:
    return int(os.getenv("SESSION_MAX_AGE_SECONDS", str(60 * 60 * 24 * 7)))


def _admin_user() -> str:
    return os.getenv("ADMIN_USERNAME", "admin")


def _admin_pass() -> str:
    return os.getenv("ADMIN_PASSWORD", "admin")


def verify_credentials(username: str, password: str) -> bool:
    return secrets.compare_digest(username or "", _admin_user()) and secrets.compare_digest(
        password or "", _admin_pass()
    )


def create_session_token(username: str) -> str:
    return _signer.sign(username.encode("utf-8")).decode("utf-8")


def parse_session_token(token: str) -> str | None:
    if not token:
        return None
    try:
        value = _signer.unsign(token, max_age=_max_age_seconds())
        return value.decode("utf-8")
    except BadSignature:
        return None


GUEST_USER = "guest"


def is_guest(user: str | None) -> bool:
    return user == GUEST_USER


def session_expiry_dt() -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(seconds=_max_age_seconds())
