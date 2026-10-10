"""
Security utilities: Password hashing, JWT generation, and token revocation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
from fastapi import HTTPException, status
from jose import JWTError, jwt

from app import cache as redis_cache
from app.config import settings

ALGORITHM = settings.jwt_algorithm
SECRET_KEY = settings.jwt_secret
ACCESS_TOKEN_EXPIRE_MINUTES = settings.jwt_expire_minutes
REFRESH_TOKEN_EXPIRE_DAYS = 7


# ── Password Hashing ────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Hash plaintext password using bcrypt with standard work factor."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str | None) -> bool:
    """Verify plaintext password against bcrypt hash."""
    if not hashed_password:
        return False
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False


# ── JWT Token Generation ───────────────────────────────────────────────────────

def create_access_token(
    *,
    user_id: str | uuid.UUID,
    org_id: str | uuid.UUID,
    email: str,
    role: str,
    expires_delta: timedelta | None = None,
) -> tuple[str, str, datetime]:
    """
    Generate a signed JWT access token.
    Returns: (token_str, jti, expire_datetime)
    """
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    jti = str(uuid.uuid4())

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "email": email,
        "role": role,
        "jti": jti,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    encoded_jwt = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt, jti, expire


def create_refresh_token(
    *,
    user_id: str | uuid.UUID,
    org_id: str | uuid.UUID,
    expires_delta: timedelta | None = None,
) -> tuple[str, str, datetime]:
    """
    Generate a signed refresh token.
    Returns: (token_str, jti, expire_datetime)
    """
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))
    jti = str(uuid.uuid4())

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "jti": jti,
        "type": "refresh",
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    encoded_jwt = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt, jti, expire


def decode_token(token: str) -> dict[str, Any]:
    """
    Validate signature and decode token payload.
    Raises HTTPException(401) on expired or invalid token.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Token Revocation & Session Blacklisting ───────────────────────────────────

async def revoke_token(jti: str, remaining_seconds: int = 3600) -> None:
    """Blacklist a token by its jti in Redis until its natural expiration."""
    key = f"revoked_token:{jti}"
    # At least 60 seconds TTL
    ttl = max(60, remaining_seconds)
    await redis_cache.cache_set(key, {"revoked_at": datetime.now(timezone.utc).isoformat()}, ttl=ttl)


async def is_token_revoked(jti: str) -> bool:
    """Check if token jti exists in revocation blocklist."""
    key = f"revoked_token:{jti}"
    revoked = await redis_cache.cache_get(key)
    return revoked is not None
