"""
Authentication router: Login, registration, token refresh, logout, and profile inspection.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db, set_org_context
from app.dependencies import AuthenticatedUser, get_current_user, require_roles
from app.models import Organization, OrganizationMembership, User
from app.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    LogoutResponse,
    RefreshTokenRequest,
    RegisterRequest,
    TokenResponse,
    UserCreate,
    UserProfileOut,
)
from app.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    is_token_revoked,
    revoke_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Authenticate user and issue JWT tokens",
)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    # 1. Fetch user by email
    stmt = select(User).where(User.email == body.email)
    user = (await db.scalars(stmt)).first()

    # Constant-time verification & anti-enumeration defense
    dummy_hash = "$2b$12$e80yq9VwG9Z1xZg0Bv9eDu2qV7u4d1D8UvV6Hj0iI.4YFf9T7h5hO"
    target_hash = user.hashed_password if user and user.hashed_password else dummy_hash
    password_valid = verify_password(body.password, target_hash)

    if not user or not password_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2. Resolve organization membership
    mem_stmt = select(OrganizationMembership).where(OrganizationMembership.user_id == user.id)
    if body.org_slug:
        mem_stmt = (
            mem_stmt.join(Organization, Organization.id == OrganizationMembership.org_id)
            .where(Organization.slug == body.org_slug)
        )
    membership = (await db.scalars(mem_stmt)).first()

    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not have an active organization membership",
        )

    # 3. Generate tokens
    access_token, jti, expire = create_access_token(
        user_id=user.id,
        org_id=membership.org_id,
        email=user.email or "",
        role=membership.role,
    )
    refresh_token, _, _ = create_refresh_token(
        user_id=user.id,
        org_id=membership.org_id,
    )

    expires_in = int((expire - datetime.now(timezone.utc)).total_seconds())

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=expires_in,
        user=UserProfileOut(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            org_id=membership.org_id,
            role=membership.role,
        ),
    )


@router.get("/users", response_model=list[UserProfileOut], summary="List active organization members")
async def list_organization_users(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[UserProfileOut]:
    rows = (await db.execute(
        select(User, OrganizationMembership.role)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .where(OrganizationMembership.org_id == current_user.org_id)
        .order_by(User.display_name, User.email)
    )).all()
    return [
        UserProfileOut(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            org_id=current_user.org_id,
            role=role,
        )
        for user, role in rows
    ]


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new organization and admin user",
)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    # 1. Validate slug uniqueness
    existing_org = (await db.scalars(select(Organization).where(Organization.slug == body.org_slug))).first()
    if existing_org:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Organization slug already exists")

    # 2. Validate email uniqueness
    existing_user = (await db.scalars(select(User).where(User.email == body.email))).first()
    if existing_user:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User email already registered")

    # 3. Create Org, User, Membership
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()

    org = Organization(
        id=org_id,
        slug=body.org_slug,
        name=body.org_name,
        settings={"timezone": "UTC"},
    )
    db.add(org)
    await db.flush()

    hashed = hash_password(body.password)
    user = User(
        id=user_id,
        external_subject=f"auth0|{user_id}",
        email=body.email,
        display_name=body.display_name or body.email.split("@")[0],
        hashed_password=hashed,
    )
    db.add(user)
    await db.flush()

    membership = OrganizationMembership(
        org_id=org_id,
        user_id=user_id,
        role="admin",
    )
    db.add(membership)

    await db.commit()
    await db.refresh(user)

    # 4. Generate tokens
    access_token, jti, expire = create_access_token(
        user_id=user.id,
        org_id=org_id,
        email=user.email,
        role="admin",
    )
    refresh_token, _, _ = create_refresh_token(
        user_id=user.id,
        org_id=org_id,
    )
    expires_in = int((expire - datetime.now(timezone.utc)).total_seconds())

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=expires_in,
        user=UserProfileOut(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            org_id=org_id,
            role="admin",
        ),
    )


@router.post(
    "/users",
    response_model=UserProfileOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user in the authenticated organization",
)
async def create_user(
    body: UserCreate,
    current_user: AuthenticatedUser = Depends(require_roles(["admin"])),
    db: AsyncSession = Depends(get_db),
) -> UserProfileOut:
    normalized_email = body.email.lower()
    existing_user = (await db.scalars(select(User).where(User.email == normalized_email))).first()
    if existing_user:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User email already registered")

    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        external_subject=f"local|{user_id}",
        email=normalized_email,
        display_name=body.display_name or normalized_email.split("@")[0],
        hashed_password=hash_password(body.password),
    )
    membership = OrganizationMembership(
        org_id=current_user.org_id,
        user_id=user_id,
        role=body.role,
    )
    db.add(user)
    await db.flush()
    db.add(membership)
    await db.commit()

    return UserProfileOut(
        id=user_id,
        email=user.email,
        display_name=user.display_name,
        org_id=current_user.org_id,
        role=body.role,
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh access token using valid refresh token",
)
async def refresh_token(body: RefreshTokenRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    payload = decode_token(body.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )

    r_jti = payload.get("jti")
    if not r_jti or await is_token_revoked(r_jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = uuid.UUID(payload["sub"])
    org_id = uuid.UUID(payload["org_id"])

    # Load membership
    mem_stmt = select(OrganizationMembership).where(
        OrganizationMembership.user_id == user_id,
        OrganizationMembership.org_id == org_id,
    )
    membership = (await db.scalars(mem_stmt)).first()
    if not membership:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization membership no longer valid")

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Issue new access token
    access_token, jti, expire = create_access_token(
        user_id=user.id,
        org_id=membership.org_id,
        email=user.email or "",
        role=membership.role,
    )
    expires_in = int((expire - datetime.now(timezone.utc)).total_seconds())

    return TokenResponse(
        access_token=access_token,
        refresh_token=body.refresh_token,
        token_type="bearer",
        expires_in=expires_in,
        user=UserProfileOut(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            org_id=membership.org_id,
            role=membership.role,
        ),
    )


@router.post(
    "/logout",
    response_model=LogoutResponse,
    summary="Terminate session and revoke JWT access token",
)
async def logout(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> LogoutResponse:
    # Revoke jti in Redis
    await revoke_token(current_user.jti, remaining_seconds=settings.jwt_expire_minutes * 60)
    return LogoutResponse(message="Successfully logged out and session revoked")


@router.get(
    "/me",
    response_model=UserProfileOut,
    summary="Get authenticated user profile and active organization context",
)
async def get_me(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserProfileOut:
    user = await db.get(User, current_user.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserProfileOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        org_id=current_user.org_id,
        role=current_user.role,
    )


@router.post(
    "/change-password",
    response_model=dict[str, str],
    summary="Change password for authenticated user",
)
async def change_password(
    body: ChangePasswordRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    user = await db.get(User, current_user.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not user.hashed_password or not verify_password(body.old_password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")

    if len(body.new_password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must be at least 8 characters")

    user.hashed_password = hash_password(body.new_password)
    user.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return {"message": "Password changed successfully"}

