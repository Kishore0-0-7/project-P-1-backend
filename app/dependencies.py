"""
Authentication and Authorization dependencies.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db, set_org_context
from app.models import OrganizationMembership, User
from app.security import decode_token, is_token_revoked

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: uuid.UUID
    org_id: uuid.UUID
    email: str
    role: str
    jti: str


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    """
    Validate incoming Bearer JWT, check revocation list, and bind tenant RLS context.
    """
    raw_token = token
    if not raw_token and authorization and authorization.lower().startswith("bearer "):
        raw_token = authorization[7:].strip()

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(raw_token)
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )

    jti = payload.get("jti")
    if not jti or await is_token_revoked(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has expired or token was revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = uuid.UUID(payload["sub"])
        org_id = uuid.UUID(payload["org_id"])
    except (KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token claims",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Set PostgreSQL tenant context for Row-Level Security
    await set_org_context(db, str(org_id))

    return AuthenticatedUser(
        user_id=user_id,
        org_id=org_id,
        email=payload.get("email", ""),
        role=payload.get("role", "viewer"),
        jti=jti,
    )


def require_roles(allowed_roles: list[str]) -> Callable:
    """Dependency factory ensuring user holds one of the required roles."""
    async def role_checker(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Operation requires one of the following roles: {', '.join(allowed_roles)}",
            )
        return current_user

    return role_checker
