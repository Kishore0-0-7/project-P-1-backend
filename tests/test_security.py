"""
Security & Penetration Testing Suite for OpsTriage Backend.

Validates:
1. Password hashing & salt verification (bcrypt)
2. JWT lifecycle: creation, decoding, expiration, tamper detection
3. Session management & Redis token revocation (logout)
4. Auth API endpoints: register, login, refresh, logout, /auth/me
5. OWASP Top 10 defenses:
   - Security headers injection (nosniff, DENY, HSTS, etc.)
   - JWT 'alg: none' and signature forgery resilience
   - Multi-tenant data isolation & BOLA/IDOR prevention
   - Role-Based Access Control (RBAC) privilege escalation prevention
   - SQL Injection (SQLi) parameterization resilience
   - User enumeration mitigation
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from jose import jwt

from app.config import settings
from app.dependencies import AuthenticatedUser, require_roles
from app.models import Finding, Organization, OrganizationMembership, User
from app.security import (
    ALGORITHM,
    SECRET_KEY,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    is_token_revoked,
    revoke_token,
    verify_password,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Password Hashing & Verification Primitives
# ═══════════════════════════════════════════════════════════════════════════════

class TestPasswordSecurity:
    def test_bcrypt_hashing_and_verification(self):
        password = "SuperSecretPassword123!"
        hashed = hash_password(password)

        assert hashed != password
        assert hashed.startswith("$2b$")
        assert verify_password(password, hashed) is True
        assert verify_password("WrongPassword123!", hashed) is False

    def test_bcrypt_produces_unique_salts(self):
        password = "IdenticalPassword"
        hash1 = hash_password(password)
        hash2 = hash_password(password)

        assert hash1 != hash2
        assert verify_password(password, hash1) is True
        assert verify_password(password, hash2) is True

    def test_verify_empty_or_none_hash(self):
        assert verify_password("anything", None) is False
        assert verify_password("anything", "") is False
        assert verify_password("anything", "invalid-hash-format") is False


# ═══════════════════════════════════════════════════════════════════════════════
# 2. JWT Lifecycle & Session Revocation
# ═══════════════════════════════════════════════════════════════════════════════

class TestJwtLifecycle:
    def test_create_and_decode_access_token(self):
        user_id = uuid.uuid4()
        org_id = uuid.uuid4()
        token, jti, expire = create_access_token(
            user_id=user_id,
            org_id=org_id,
            email="analyst@opstriage.io",
            role="analyst",
        )

        assert isinstance(token, str)
        assert isinstance(jti, str)
        assert expire > datetime.now(timezone.utc)

        payload = decode_token(token)
        assert payload["sub"] == str(user_id)
        assert payload["org_id"] == str(org_id)
        assert payload["email"] == "analyst@opstriage.io"
        assert payload["role"] == "analyst"
        assert payload["type"] == "access"
        assert payload["jti"] == jti

    def test_create_and_decode_refresh_token(self):
        user_id = uuid.uuid4()
        org_id = uuid.uuid4()
        token, jti, expire = create_refresh_token(user_id=user_id, org_id=org_id)

        payload = decode_token(token)
        assert payload["sub"] == str(user_id)
        assert payload["org_id"] == str(org_id)
        assert payload["type"] == "refresh"
        assert payload["jti"] == jti

    def test_expired_token_rejection(self):
        user_id = uuid.uuid4()
        org_id = uuid.uuid4()
        expired_token, _, _ = create_access_token(
            user_id=user_id,
            org_id=org_id,
            email="expired@opstriage.io",
            role="viewer",
            expires_delta=timedelta(seconds=-10),
        )

        with pytest.raises(Exception) as exc_info:
            decode_token(expired_token)
        assert "Token has expired" in str(exc_info.value)

    def test_tampered_token_payload(self):
        token, _, _ = create_access_token(
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            email="victim@opstriage.io",
            role="viewer",
        )
        parts = token.split(".")
        # Tamper with payload (second segment)
        tampered = f"{parts[0]}.eyJzdWIiOiAiaGFja2VyIn0.{parts[2]}"

        with pytest.raises(Exception) as exc_info:
            decode_token(tampered)
        assert "Invalid authentication token" in str(exc_info.value)

    def test_invalid_signature_wrong_secret(self):
        payload = {
            "sub": str(uuid.uuid4()),
            "org_id": str(uuid.uuid4()),
            "email": "forged@opstriage.io",
            "role": "admin",
            "type": "access",
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        forged_token = jwt.encode(payload, "WRONG_SECRET_KEY_FORGERY_ATTEMPT", algorithm=ALGORITHM)

        with pytest.raises(Exception) as exc_info:
            decode_token(forged_token)
        assert "Invalid authentication token" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_token_revocation_in_redis(self):
        test_jti = str(uuid.uuid4())
        assert await is_token_revoked(test_jti) is False

        await revoke_token(test_jti, remaining_seconds=300)
        assert await is_token_revoked(test_jti) is True


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Auth API Endpoints (Register, Login, Session, Logout, Refresh)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuthEndpoints:
    @pytest.mark.asyncio
    async def test_register_and_login_flow(self, client: AsyncClient):
        reg_payload = {
            "org_name": "Acme Security Corp",
            "org_slug": f"acme-{uuid.uuid4().hex[:6]}",
            "email": f"secops-{uuid.uuid4().hex[:6]}@acme.com",
            "password": "StrongPassword123!",
            "display_name": "SecOps Lead",
        }

        # 1. Register
        reg_resp = await client.post("/api/v1/auth/register", json=reg_payload)
        assert reg_resp.status_code == 201
        data = reg_resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["email"] == reg_payload["email"]
        assert data["user"]["role"] == "admin"

        # 2. Login with valid credentials
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": reg_payload["email"], "password": reg_payload["password"]},
        )
        assert login_resp.status_code == 200
        login_data = login_resp.json()
        assert "access_token" in login_data
        access_token = login_data["access_token"]
        refresh_token_val = login_data["refresh_token"]

        # 3. Access protected /auth/me
        me_resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert me_resp.status_code == 200
        me_data = me_resp.json()
        assert me_data["email"] == reg_payload["email"]
        assert me_data["role"] == "admin"

        # 4. Refresh token
        refresh_resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token_val},
        )
        assert refresh_resp.status_code == 200
        assert "access_token" in refresh_resp.json()

        # 5. Logout & verify revocation
        logout_resp = await client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert logout_resp.status_code == 200
        assert "revoked" in logout_resp.json()["message"].lower()

        # Subsequent call with revoked token must fail with 401
        post_logout_me = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert post_logout_me.status_code == 401
        assert "revoked" in post_logout_me.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_register_duplicate_org_slug_fails(self, client: AsyncClient):
        slug = f"org-{uuid.uuid4().hex[:6]}"
        payload1 = {
            "org_name": "First Org",
            "org_slug": slug,
            "email": f"user1-{uuid.uuid4().hex[:6]}@example.com",
            "password": "Password123!",
        }
        res1 = await client.post("/api/v1/auth/register", json=payload1)
        assert res1.status_code == 201

        payload2 = {
            "org_name": "Duplicate Org",
            "org_slug": slug,
            "email": f"user2-{uuid.uuid4().hex[:6]}@example.com",
            "password": "Password123!",
        }
        res2 = await client.post("/api/v1/auth/register", json=payload2)
        assert res2.status_code == 409
        assert "Organization slug already exists" in res2.json()["detail"]

    @pytest.mark.asyncio
    async def test_register_duplicate_email_fails(self, client: AsyncClient):
        email = f"shared-{uuid.uuid4().hex[:6]}@example.com"
        payload1 = {
            "org_name": "Org Alpha",
            "org_slug": f"alpha-{uuid.uuid4().hex[:6]}",
            "email": email,
            "password": "Password123!",
        }
        res1 = await client.post("/api/v1/auth/register", json=payload1)
        assert res1.status_code == 201

        payload2 = {
            "org_name": "Org Beta",
            "org_slug": f"beta-{uuid.uuid4().hex[:6]}",
            "email": email,
            "password": "Password123!",
        }
        res2 = await client.post("/api/v1/auth/register", json=payload2)
        assert res2.status_code == 409
        assert "User email already registered" in res2.json()["detail"]

    @pytest.mark.asyncio
    async def test_admin_can_create_user_in_current_organization(self, client: AsyncClient):
        admin_email = f"admin-{uuid.uuid4().hex[:6]}@example.com"
        registration = await client.post(
            "/api/v1/auth/register",
            json={
                "org_name": "User Management Org",
                "org_slug": f"users-{uuid.uuid4().hex[:6]}",
                "email": admin_email,
                "password": "AdminPassword123!",
            },
        )
        admin_token = registration.json()["access_token"]

        response = await client.post(
            "/api/v1/auth/users",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "email": "new.operator@example.com",
                "password": "OperatorPassword123!",
                "display_name": "New Operator",
                "role": "operator",
            },
        )

        assert response.status_code == 201
        created = response.json()
        assert created["email"] == "new.operator@example.com"
        assert created["display_name"] == "New Operator"
        assert created["role"] == "operator"
        assert created["org_id"] == registration.json()["user"]["org_id"]
        assert "password" not in created
        assert "hashed_password" not in created

        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "new.operator@example.com",
                "password": "OperatorPassword123!",
            },
        )
        assert login.status_code == 200
        assert login.json()["user"]["role"] == "operator"

    @pytest.mark.asyncio
    async def test_only_admin_can_create_user(self, client: AsyncClient):
        registration = await client.post(
            "/api/v1/auth/register",
            json={
                "org_name": "Role Boundary Org",
                "org_slug": f"roles-{uuid.uuid4().hex[:6]}",
                "email": f"admin-{uuid.uuid4().hex[:6]}@example.com",
                "password": "AdminPassword123!",
            },
        )
        admin_token = registration.json()["access_token"]
        created = await client.post(
            "/api/v1/auth/users",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "email": f"viewer-{uuid.uuid4().hex[:6]}@example.com",
                "password": "ViewerPassword123!",
                "role": "viewer",
            },
        )
        viewer_token = (await client.post(
            "/api/v1/auth/login",
            json={
                "email": created.json()["email"],
                "password": "ViewerPassword123!",
            },
        )).json()["access_token"]

        response = await client.post(
            "/api/v1/auth/users",
            headers={"Authorization": f"Bearer {viewer_token}"},
            json={
                "email": f"blocked-{uuid.uuid4().hex[:6]}@example.com",
                "password": "BlockedPassword123!",
            },
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_create_user_rejects_duplicate_email(self, client: AsyncClient):
        registration = await client.post(
            "/api/v1/auth/register",
            json={
                "org_name": "Duplicate User Org",
                "org_slug": f"duplicate-users-{uuid.uuid4().hex[:6]}",
                "email": f"admin-{uuid.uuid4().hex[:6]}@example.com",
                "password": "AdminPassword123!",
            },
        )
        token = registration.json()["access_token"]
        email = f"duplicate-{uuid.uuid4().hex[:6]}@example.com"
        payload = {"email": email, "password": "Password123!", "role": "analyst"}

        first = await client.post(
            "/api/v1/auth/users",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        second = await client.post(
            "/api/v1/auth/users",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

        assert first.status_code == 201
        assert second.status_code == 409
        assert "User email already registered" in second.json()["detail"]

    @pytest.mark.asyncio
    async def test_login_invalid_credentials(self, client: AsyncClient):
        # Non-existent user
        resp_no_user = await client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@opstriage.io", "password": "AnyPassword123!"},
        )
        assert resp_no_user.status_code == 401
        assert "Invalid email or password" in resp_no_user.json()["detail"]

        # Existing user with wrong password
        email = f"user-{uuid.uuid4().hex[:6]}@example.com"
        await client.post(
            "/api/v1/auth/register",
            json={
                "org_name": "Test Org",
                "org_slug": f"org-{uuid.uuid4().hex[:6]}",
                "email": email,
                "password": "CorrectPassword123!",
            },
        )
        resp_wrong_pw = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "WrongPassword999!"},
        )
        assert resp_wrong_pw.status_code == 401
        assert "Invalid email or password" in resp_wrong_pw.json()["detail"]

    @pytest.mark.asyncio
    async def test_auth_me_unauthorized_cases(self, client: AsyncClient):
        # Missing token
        r1 = await client.get("/api/v1/auth/me")
        assert r1.status_code == 401
        assert "Authentication required" in r1.json()["detail"]

        # Malformed bearer scheme
        r2 = await client.get("/api/v1/auth/me", headers={"Authorization": "Basic 123456"})
        assert r2.status_code == 401

        # Invalid token string
        r3 = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-valid-jwt"})
        assert r3.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_token_validation(self, client: AsyncClient):
        user_id = uuid.uuid4()
        org_id = uuid.uuid4()

        # Access token used at refresh endpoint must be rejected
        access_tok, _, _ = create_access_token(
            user_id=user_id,
            org_id=org_id,
            email="test@test.com",
            role="analyst",
        )
        resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": access_tok})
        assert resp.status_code == 401
        assert "Invalid token type" in resp.json()["detail"]


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Penetration Testing & OWASP Top 10 Defenses
# ═══════════════════════════════════════════════════════════════════════════════

class TestPenetrationDefenses:
    @pytest.mark.asyncio
    async def test_owasp_security_headers_present(self, client: AsyncClient):
        """
        Verify HTTP security response headers prevent MIME-sniffing, clickjacking,
        and insecure transports across all endpoints.
        """
        resp = await client.get("/health")
        assert resp.status_code == 200
        headers = resp.headers

        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("X-Frame-Options") == "DENY"
        assert headers.get("X-XSS-Protection") == "1; mode=block"
        assert "max-age=" in headers.get("Strict-Transport-Security", "")
        assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert "X-Request-ID" in headers

    @pytest.mark.asyncio
    async def test_pentest_alg_none_jwt_attack(self, client: AsyncClient):
        """
        Attempt 'alg: none' JWT signature bypass attack.
        The backend must strictly reject unsigned/none tokens.
        """
        import base64
        import json

        header_b64 = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
        payload = {
            "sub": str(uuid.uuid4()),
            "org_id": str(uuid.uuid4()),
            "email": "hacker@evil.com",
            "role": "admin",
            "type": "access",
            "jti": str(uuid.uuid4()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        forged_unsigned_token = f"{header_b64}.{payload_b64}."

        resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {forged_unsigned_token}"},
        )
        assert resp.status_code == 401
        assert "Invalid authentication token" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_rbac_privilege_escalation_prevention(self):
        """
        Verify require_roles dependency halts unauthorized execution with 403 Forbidden.
        """
        role_checker = require_roles(["admin"])

        # Viewer attempting admin action
        viewer_user = AuthenticatedUser(
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            email="viewer@test.com",
            role="viewer",
            jti=str(uuid.uuid4()),
        )
        with pytest.raises(Exception) as exc_info:
            await role_checker(viewer_user)
        assert "Operation requires one of the following roles: admin" in str(exc_info.value)

        # Admin attempting admin action -> succeeds
        admin_user = AuthenticatedUser(
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            email="admin@test.com",
            role="admin",
            jti=str(uuid.uuid4()),
        )
        result = await role_checker(admin_user)
        assert result.role == "admin"

    @pytest.mark.asyncio
    async def test_tenant_isolation_bola_idor_prevention(self, client: AsyncClient):
        """
        BOLA/IDOR Pentest:
        Verify Tenant B cannot view or access Tenant A's findings even with a valid UUID.
        """
        org_a = str(uuid.uuid4())
        org_b = str(uuid.uuid4())

        # Create finding in Org A
        create_resp = await client.post(
            "/api/v1/findings",
            params={"org_id": org_a},
            json={
                "fingerprint": f"fp-{uuid.uuid4()}",
                "title": "Secret Infrastructure Alert",
                "severity": "critical",
                "priority": 1,
                "priority_score": 500.0,
            },
        )
        assert create_resp.status_code == 201
        finding_id = create_resp.json()["id"]

        # Org B attempts to read Org A's finding via IDOR attack
        idor_resp = await client.get(
            f"/api/v1/findings/{finding_id}",
            params={"org_id": org_b},
        )
        assert idor_resp.status_code == 404
        assert "Finding not found" in idor_resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_sql_injection_resilience(self, client: AsyncClient):
        """
        Fuzzing endpoints with standard SQL Injection payloads.
        SQLAlchemy ORM and Pydantic validation must sanitize/reject inputs
        without 500 internal server crashes.
        """
        org_id = str(uuid.uuid4())

        sqli_payloads = [
            "' OR '1'='1",
            "'; DROP TABLE opstriage.users; --",
            "' UNION SELECT NULL, NULL, NULL --",
            "admin' --",
            "1; WAITFOR DELAY '0:0:5'--",
        ]

        for payload in sqli_payloads:
            # 1. In enum/filter query param -> 422 or empty/safe result
            resp = await client.get(
                "/api/v1/findings",
                params={"org_id": org_id, "severity": payload},
            )
            # Expect either 422 validation failure on enum, or 200 with 0 results
            assert resp.status_code in (200, 422)
            assert resp.status_code != 500

            # 2. In login email field -> safe 401, no SQL syntax exception
            login_resp = await client.post(
                "/api/v1/auth/login",
                json={"email": payload, "password": "password"},
            )
            assert login_resp.status_code in (401, 422)
            assert login_resp.status_code != 500
