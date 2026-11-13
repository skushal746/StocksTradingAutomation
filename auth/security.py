"""Password hashing (Argon2) and JWT access tokens (HS256)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from config import ConfigError, Settings

MIN_SECRET_LENGTH = 32
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def _signing_key(settings: Settings) -> str:
    key = settings.jwt_secret_key.get_secret_value()
    if len(key) < MIN_SECRET_LENGTH:
        raise ConfigError(f"JWT_SECRET_KEY must be set to at least {MIN_SECRET_LENGTH} characters")
    return key


def create_access_token(user_id: int, email: str, settings: Settings) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user_id),
        "email": email,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
    }
    return jwt.encode(claims, _signing_key(settings), algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, settings: Settings) -> dict[str, Any]:
    """Raises jwt.InvalidTokenError for a bad signature, a malformed token or an expired one."""
    # Passing the algorithm list explicitly stops tokens that claim "alg: none" from being accepted.
    return jwt.decode(token, _signing_key(settings), algorithms=[settings.jwt_algorithm])
