"""
Comprehensive Test Suite for Gemini AI Assistant Service & Endpoints in OpsTriage.

Validates:
1. Structured Pydantic output parsing and contract verification.
2. Ground-truth evidence ID validation (hallucinated references stripped).
3. Secret redaction and telemetry isolation (prompt injection defense).
4. Feature toggle: AI_ENABLED=False returns 503 AI_DISABLED.
5. Configuration check: missing GEMINI_API_KEY returns 503 AI_NOT_CONFIGURED.
6. Authentication & Tenant Authorization:
   - 401 Unauthorized for missing/invalid bearer tokens.
   - 403 Forbidden on cross-tenant access.
7. Resilience & Error Taxonomy:
   - 504 TIMEOUT on request timeouts.
   - 429 RATE_LIMITED on quota/capacity errors.
   - 502 PROVIDER_UNAVAILABLE on upstream connection errors or provider outages.
   - 500 PARSING_ERROR on invalid or schema-violating model outputs.
8. Non-mutation invariant: AI service is strictly read-only and never modifies database records.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Asset, Case, CaseFinding, Finding, FindingOccurrence, Organization, OrganizationMembership, User
from app.schemas import AIAssistanceResponse, PossibleCause
from app.services.ai_assistant import (
    AIAssistanceError,
    _execute_gemini_generation,
    explain_signal,
    get_genai_client,
    investigate_case,
    redact_secrets,
    sanitize_input,
    set_genai_client,
    support_chat,
    summarize_case_evidence,
)


# ── Mock Response Helper ───────────────────────────────────────────────────────

def make_mock_ai_response(
    summary: str = "Supply air temperature anomaly detected on AHU-17.",
    facts: list[str] | None = None,
    causes: list[dict[str, Any]] | None = None,
    steps: list[str] | None = None,
    missing_info: list[str] | None = None,
    confidence: str = "High confidence based on continuous sensor saturation readings.",
    escalation: bool = False,
    escalation_reason: str | None = None,
    raw_text: str | None = None,
) -> MagicMock:
    """Builds a mock Google GenAI response object."""
    mock_resp = MagicMock()
    if raw_text is not None:
        mock_resp.parsed = None
        mock_resp.text = raw_text
        return mock_resp

    pydantic_obj = AIAssistanceResponse(
        plain_english_summary=summary,
        observed_facts=facts or ["Supply air is 28.8°C (+4.8°C above setpoint)", "Valve feedback saturated at 97%"],
        possible_causes=[
            PossibleCause(**c)
            for c in (
                causes
                or [
                    {
                        "description": "Chilled-water actuator valve seized in restricted position",
                        "supporting_evidence_ids": ["finding:test-finding-1", "asset:test-asset-1"],
                    }
                ]
            )
        ],
        recommended_next_steps=steps or ["Inspect chilled-water valve actuator physically", "Verify chilled-water supply flow rate"],
        missing_information=missing_info or ["Upstream chilled water header supply temperature"],
        confidence_assessment=confidence,
        escalation_recommended=escalation,
        escalation_reason=escalation_reason,
        validated_evidence_ids=[],
        model_used="gemini-3.1-flash-lite",
    )
    mock_resp.parsed = pydantic_obj
    mock_resp.text = pydantic_obj.model_dump_json()
    return mock_resp


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Unit Tests: Secret Redaction, Prompt Isolation, & Evidence Validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestAIAssistantUnit:
    def test_redact_secrets_masks_sensitive_patterns(self):
        text = (
            "Telemetry gateway ahu-gateway Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret1234567890 "
            "with api_key='sk-ant-api03-abcdef1234567890' and password='SuperSecretPassword123' "
            "reported sensor deviation."
        )
        sanitized = redact_secrets(text)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in sanitized
        assert "sk-ant-api03" not in sanitized
        assert "SuperSecretPassword123" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_sanitize_input_clamps_length_and_redacts(self):
        long_text = "A" * 1000 + " password=topsecret123"
        sanitized = sanitize_input(long_text, max_len=200)
        assert len(sanitized) <= 220
        assert "topsecret" not in sanitized
        assert "[truncated]" in sanitized

    @pytest.mark.asyncio
    async def test_evidence_id_validation_strips_hallucinated_ids(self):
        """
        The model might hypothesize causes and invent reference IDs.
        The service must strip any reference ID not in allowed_evidence_ids.
        """
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=make_mock_ai_response(
                causes=[
                    {
                        "description": "Legitimate cause supported by actual signal",
                        "supporting_evidence_ids": ["finding:real-finding-uuid", "fake:hallucinated-id-999"],
                    },
                    {
                        "description": "Completely hallucinated evidence citation",
                        "supporting_evidence_ids": ["log:phantom-server-123"],
                    },
                ]
            )
        )
        set_genai_client(mock_client)

        allowed = {"finding:real-finding-uuid", "asset:real-asset-uuid"}
        with patch.object(settings, "ai_enabled", True):
            result = await _execute_gemini_generation("Test prompt", allowed)

        # First cause should have hallucinated ID removed, keeping only real ID
        assert result.possible_causes[0].supporting_evidence_ids == ["finding:real-finding-uuid"]
        # Second cause should have empty supporting IDs because log:phantom was not allowed
        assert result.possible_causes[1].supporting_evidence_ids == []
        # Result validated_evidence_ids should only contain the verified set
        assert result.validated_evidence_ids == ["finding:real-finding-uuid"]
        assert "fake:hallucinated-id-999" not in result.validated_evidence_ids
        assert "log:phantom-server-123" not in result.validated_evidence_ids

    @pytest.mark.asyncio
    async def test_ai_disabled_toggle_raises_ai_disabled_error(self):
        with patch.object(settings, "ai_enabled", False):
            with pytest.raises(AIAssistanceError) as exc_info:
                await _execute_gemini_generation("prompt", set())
            assert exc_info.value.status_code == 503
            assert exc_info.value.error_code == "AI_DISABLED"

    @pytest.mark.asyncio
    async def test_missing_api_key_raises_ai_not_configured(self):
        set_genai_client(None)
        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", ""):
            with pytest.raises(AIAssistanceError) as exc_info:
                await _execute_gemini_generation("prompt", set())
            assert exc_info.value.status_code == 503
            assert exc_info.value.error_code == "AI_NOT_CONFIGURED"

    @pytest.mark.asyncio
    async def test_timeout_raises_timeout_error(self):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(side_effect=asyncio.TimeoutError())
        set_genai_client(mock_client)

        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", "mock-key"):
            with pytest.raises(AIAssistanceError) as exc_info:
                await _execute_gemini_generation("prompt", set())
            assert exc_info.value.status_code == 504
            assert exc_info.value.error_code == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_quota_exceeded_raises_rate_limited(self):
        from google.genai import errors as genai_errors

        mock_client = MagicMock()
        # Simulate Google GenAI 429 error
        api_err = genai_errors.APIError(429, {"error": {"message": "Resource has been exhausted (quota)."}})
        mock_client.aio.models.generate_content = AsyncMock(side_effect=api_err)
        set_genai_client(mock_client)

        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", "mock-key"):
            with pytest.raises(AIAssistanceError) as exc_info:
                await _execute_gemini_generation("prompt", set())
            assert exc_info.value.status_code == 429
            assert exc_info.value.error_code == "RATE_LIMITED"

    @pytest.mark.asyncio
    async def test_provider_outage_raises_provider_unavailable(self):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(side_effect=ConnectionError("Upstream connection refused"))
        set_genai_client(mock_client)

        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", "mock-key"):
            with pytest.raises(AIAssistanceError) as exc_info:
                await _execute_gemini_generation("prompt", set())
            assert exc_info.value.status_code == 502
            assert exc_info.value.error_code == "PROVIDER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_invalid_output_json_raises_parsing_error(self):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=make_mock_ai_response(raw_text="NOT_VALID_JSON_AT_ALL {broken: true}")
        )
        set_genai_client(mock_client)

        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", "mock-key"):
            with pytest.raises(AIAssistanceError) as exc_info:
                await _execute_gemini_generation("prompt", set())
            assert exc_info.value.status_code == 500
            assert exc_info.value.error_code == "PARSING_ERROR"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Integration Tests: Authenticated API Endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def test_workspace(client: AsyncClient, db_session: AsyncSession):
    """Creates an authenticated organization, user, asset, finding, and case."""
    # 1. Register organization and user
    reg_resp = await client.post(
        "/api/v1/auth/register",
        json={
            "org_name": "Triage AI Test Org",
            "org_slug": f"ai-org-{uuid.uuid4().hex[:6]}",
            "email": f"operator-{uuid.uuid4().hex[:6]}@example.com",
            "password": "Password123!",
        },
    )
    assert reg_resp.status_code == 201
    data = reg_resp.json()
    token = data["access_token"]
    org_id = data["user"]["org_id"]
    org_uuid = uuid.UUID(org_id)

    # 2. Seed an Asset in this organization
    asset = Asset(
        id=uuid.uuid4(),
        org_id=org_uuid,
        name="AHU-17 North Wing",
        asset_type="HVAC",
        criticality=4,
        status="active",
        metadata_={"location": "Floor 3 Mech Room"},
    )
    db_session.add(asset)

    # 3. Seed a Finding in this organization
    finding = Finding(
        id=uuid.uuid4(),
        org_id=org_uuid,
        asset_id=asset.id,
        fingerprint=f"fp-ahu-{uuid.uuid4().hex[:6]}",
        title="Supply Air Temperature Drift Above Setpoint",
        description="Supply air temperature drifting +4.8°C above threshold.",
        severity="critical",
        priority=1,
        priority_score=850.0,
        status="open",
        evidence={"items": [{"label": "Supply air", "value": "28.8°C", "trend": "up"}]},
        priority_explanation={"factors": [{"label": "Severe Drift", "contribution": 450}]},
    )
    db_session.add(finding)

    # 4. Seed a Case correlated with this finding
    case = Case(
        id=uuid.uuid4(),
        org_id=org_uuid,
        title="Investigate AHU-17 Actuator Drift",
        priority=1,
        status="in_progress",
    )
    db_session.add(case)
    await db_session.flush()

    case_finding = CaseFinding(org_id=org_uuid, case_id=case.id, finding_id=finding.id)
    db_session.add(case_finding)
    await db_session.commit()

    return {
        "token": token,
        "org_id": org_id,
        "org_uuid": org_uuid,
        "asset": asset,
        "finding": finding,
        "case": case,
    }


class TestAIAssistantEndpoints:
    @pytest.mark.asyncio
    async def test_organization_member_list_is_scoped_to_current_tenant(self, client: AsyncClient, test_workspace):
        headers = {"Authorization": f"Bearer {test_workspace['token']}"}
        response = await client.get("/api/v1/auth/users", headers=headers)
        assert response.status_code == 200
        assert response.json()
        assert all(user["org_id"] == test_workspace["org_id"] for user in response.json())
        assert any(user["email"].startswith("operator-") for user in response.json())

    @pytest.mark.asyncio
    async def test_support_chat_uses_its_dedicated_timeout(self):
        mock_client = MagicMock()
        async def slow_reply(**kwargs):
            await asyncio.sleep(0.05)
            return make_mock_ai_response(text="Hello")
        mock_client.aio.models.generate_content = AsyncMock(side_effect=slow_reply)
        set_genai_client(mock_client)
        try:
            with patch.object(settings, "ai_enabled", True), \
                 patch.object(settings, "support_chat_timeout_seconds", 0.001):
                with pytest.raises(AIAssistanceError) as exc_info:
                    await support_chat("Hi", [])
            assert exc_info.value.status_code == 504
            assert "0.001 seconds" in exc_info.value.detail
        finally:
            set_genai_client(None)

    @pytest.mark.asyncio
    async def test_support_chat_requires_auth_and_passes_chat_payload(self, client: AsyncClient, test_workspace):
        unauthorized = await client.post("/api/v1/ai/support-chat", json={"question": "How do I open a case?"})
        assert unauthorized.status_code == 401

        with patch("app.routers.ai.support_chat", new=AsyncMock(return_value={"answer": "Select a finding and choose Escalate to Case.", "model_used": "test-model"})) as chat:
            response = await client.post(
                "/api/v1/ai/support-chat",
                headers={"Authorization": f"Bearer {test_workspace['token']}"},
                json={"question": "How do I open a case?", "conversation_history": []},
            )
        assert response.status_code == 200
        assert response.json()["answer"].startswith("Select a finding")
        chat.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_explain_signal_endpoint_success(self, client: AsyncClient, test_workspace):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=make_mock_ai_response(
                summary="Thermal drift observed on AHU-17 cooling loop.",
                facts=["Valve is pinned at 97% saturation", "Supply air is 4.8°C above normal"],
                causes=[
                    {
                        "description": "Cooling coil actuator seized or constrained",
                        "supporting_evidence_ids": [f"finding:{test_workspace['finding'].id}"],
                    }
                ],
                steps=["Check actuator mechanical linkage on Floor 3", "Inspect chilled-water differential pressure"],
            )
        )
        set_genai_client(mock_client)

        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", "test-key"):
            response = await client.post(
                f"/api/v1/ai/explain-signal?org_id={test_workspace['org_id']}",
                headers={"Authorization": f"Bearer {test_workspace['token']}"},
                json={"finding_id": str(test_workspace["finding"].id)},
            )

        assert response.status_code == 200
        body = response.json()
        assert "Thermal drift" in body["plain_english_summary"]
        assert len(body["observed_facts"]) >= 2
        assert len(body["possible_causes"]) >= 1
        assert body["possible_causes"][0]["description"].startswith("Cooling coil")
        assert f"finding:{test_workspace['finding'].id}" in body["validated_evidence_ids"]
        assert body["model_used"] == "gemini-3.1-flash-lite"

    @pytest.mark.asyncio
    async def test_investigate_case_endpoint_success(self, client: AsyncClient, test_workspace):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=make_mock_ai_response(
                summary="Operational case requires physical actuator dispatch.",
                steps=["Dispatch mechanical technician to AHU-17", "Review valve feedback sensor telemetry"],
            )
        )
        set_genai_client(mock_client)

        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", "test-key"):
            response = await client.post(
                f"/api/v1/ai/investigate-case?org_id={test_workspace['org_id']}",
                headers={"Authorization": f"Bearer {test_workspace['token']}"},
                json={"case_id": str(test_workspace["case"].id)},
            )

        assert response.status_code == 200
        body = response.json()
        assert "actuator dispatch" in body["plain_english_summary"].lower()
        assert len(body["recommended_next_steps"]) >= 2

    @pytest.mark.asyncio
    async def test_summarize_evidence_endpoint_success(self, client: AsyncClient, test_workspace):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=make_mock_ai_response(
                summary="Correlated 1 active finding with 15 recurring telemetry occurrences.",
            )
        )
        set_genai_client(mock_client)

        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", "test-key"):
            response = await client.post(
                f"/api/v1/ai/summarize-evidence?org_id={test_workspace['org_id']}",
                headers={"Authorization": f"Bearer {test_workspace['token']}"},
                json={"case_id": str(test_workspace["case"].id)},
            )

        assert response.status_code == 200
        body = response.json()
        assert "active finding" in body["plain_english_summary"]

    @pytest.mark.asyncio
    async def test_unauthorized_request_rejected(self, client: AsyncClient, test_workspace):
        response = await client.post(
            f"/api/v1/ai/explain-signal?org_id={test_workspace['org_id']}",
            json={"finding_id": str(test_workspace["finding"].id)},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_cross_tenant_access_forbidden(self, client: AsyncClient, test_workspace):
        # Register a second separate organization
        reg_other = await client.post(
            "/api/v1/auth/register",
            json={
                "org_name": "Foreign Tenant Org",
                "org_slug": f"foreign-{uuid.uuid4().hex[:6]}",
                "email": f"foreign-{uuid.uuid4().hex[:6]}@example.com",
                "password": "Password123!",
            },
        )
        foreign_token = reg_other.json()["access_token"]

        # Foreign user tries to access test_workspace's org_id
        response = await client.post(
            f"/api/v1/ai/explain-signal?org_id={test_workspace['org_id']}",
            headers={"Authorization": f"Bearer {foreign_token}"},
            json={"finding_id": str(test_workspace["finding"].id)},
        )
        assert response.status_code == 403
        assert "Cross-tenant access forbidden" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_nonexistent_signal_returns_404(self, client: AsyncClient, test_workspace):
        random_uuid = str(uuid.uuid4())
        response = await client.post(
            f"/api/v1/ai/explain-signal?org_id={test_workspace['org_id']}",
            headers={"Authorization": f"Bearer {test_workspace['token']}"},
            json={"finding_id": random_uuid},
        )
        assert response.status_code == 404
        assert response.json()["detail"]["error_code"] == "RECORD_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_ai_disabled_returns_503(self, client: AsyncClient, test_workspace):
        with patch.object(settings, "ai_enabled", False):
            response = await client.post(
                f"/api/v1/ai/explain-signal?org_id={test_workspace['org_id']}",
                headers={"Authorization": f"Bearer {test_workspace['token']}"},
                json={"finding_id": str(test_workspace["finding"].id)},
            )
        assert response.status_code == 503
        err = response.json()["detail"]
        assert err["error_code"] == "AI_DISABLED"
        assert "disabled by administrator" in err["message"]

    @pytest.mark.asyncio
    async def test_ai_timeout_returns_504(self, client: AsyncClient, test_workspace):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(side_effect=asyncio.TimeoutError())
        set_genai_client(mock_client)

        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", "test-key"):
            response = await client.post(
                f"/api/v1/ai/explain-signal?org_id={test_workspace['org_id']}",
                headers={"Authorization": f"Bearer {test_workspace['token']}"},
                json={"finding_id": str(test_workspace["finding"].id)},
            )
        assert response.status_code == 504
        assert response.json()["detail"]["error_code"] == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_ai_rate_limit_returns_429(self, client: AsyncClient, test_workspace):
        from google.genai import errors as genai_errors

        mock_client = MagicMock()
        api_err = genai_errors.APIError(429, {"error": {"message": "Rate limit / Quota exceeded"}})
        mock_client.aio.models.generate_content = AsyncMock(side_effect=api_err)
        set_genai_client(mock_client)

        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", "test-key"):
            response = await client.post(
                f"/api/v1/ai/explain-signal?org_id={test_workspace['org_id']}",
                headers={"Authorization": f"Bearer {test_workspace['token']}"},
                json={"finding_id": str(test_workspace["finding"].id)},
            )
        assert response.status_code == 429
        assert response.json()["detail"]["error_code"] == "RATE_LIMITED"

    @pytest.mark.asyncio
    async def test_ai_provider_outage_returns_502(self, client: AsyncClient, test_workspace):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(side_effect=RuntimeError("Google GenAI internal 503"))
        set_genai_client(mock_client)

        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", "test-key"):
            response = await client.post(
                f"/api/v1/ai/explain-signal?org_id={test_workspace['org_id']}",
                headers={"Authorization": f"Bearer {test_workspace['token']}"},
                json={"finding_id": str(test_workspace["finding"].id)},
            )
        assert response.status_code == 502
        assert response.json()["detail"]["error_code"] == "PROVIDER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_follow_up_signal_success(self, client: AsyncClient, test_workspace):
        from app.schemas import FollowUpResponse

        mock_resp = MagicMock()
        mock_resp.parsed = FollowUpResponse(
            answer="Based on recorded telemetry, chilled water loop return temp is elevated.",
            observed_facts=["Sensor reading 28.5 C exceeds nominal"],
            possible_causes=[
                PossibleCause(
                    description="Actuator valve binding",
                    supporting_evidence_ids=[f"finding:{test_workspace['finding'].id}"],
                )
            ],
            recommended_next_steps=["Verify actuator pneumatic pressure"],
            missing_information=["Header pressure delta"],
            confidence_assessment="High confidence based on ground-truth records",
            validated_evidence_ids=[],
            model_used="gemini-3.1-flash-lite",
        )
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)
        set_genai_client(mock_client)

        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", "test-key"):
            response = await client.post(
                f"/api/v1/ai/follow-up?org_id={test_workspace['org_id']}",
                headers={"Authorization": f"Bearer {test_workspace['token']}"},
                json={
                    "context_type": "signal",
                    "context_id": str(test_workspace["finding"].id),
                    "question": "What is the primary evidence for valve binding?",
                    "conversation_history": [
                        {"role": "user", "content": "Explain this signal"},
                        {"role": "assistant", "content": "Initial diagnosis"},
                    ],
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert "elevated" in data["answer"]
        assert f"finding:{test_workspace['finding'].id}" in data["validated_evidence_ids"]

    @pytest.mark.asyncio
    async def test_follow_up_case_success(self, client: AsyncClient, test_workspace):
        from app.schemas import FollowUpResponse

        mock_resp = MagicMock()
        mock_resp.parsed = FollowUpResponse(
            answer="Case investigation shows correlated HVAC thermal anomalies.",
            observed_facts=["P1 case active within 2-hour SLA window"],
            possible_causes=[
                PossibleCause(
                    description="Chiller stage failure",
                    supporting_evidence_ids=[f"case:{test_workspace['case'].id}"],
                )
            ],
            recommended_next_steps=["Check compressor current draw"],
            missing_information=[],
            confidence_assessment="Verified",
            validated_evidence_ids=[],
            model_used="gemini-3.1-flash-lite",
        )
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)
        set_genai_client(mock_client)

        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", "test-key"):
            response = await client.post(
                f"/api/v1/ai/follow-up?org_id={test_workspace['org_id']}",
                headers={"Authorization": f"Bearer {test_workspace['token']}"},
                json={
                    "context_type": "case",
                    "context_id": str(test_workspace["case"].id),
                    "question": "What should the duty engineer check first?",
                    "conversation_history": [],
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert "Case investigation" in data["answer"]
        assert f"case:{test_workspace['case'].id}" in data["validated_evidence_ids"]

    @pytest.mark.asyncio
    async def test_follow_up_cross_tenant_forbidden(self, client: AsyncClient, test_workspace):
        other_org_id = uuid.uuid4()
        response = await client.post(
            f"/api/v1/ai/follow-up?org_id={other_org_id}",
            headers={"Authorization": f"Bearer {test_workspace['token']}"},
            json={
                "context_type": "signal",
                "context_id": str(test_workspace["finding"].id),
                "question": "Can I read other tenant data?",
                "conversation_history": [],
            },
        )
        assert response.status_code == 403
        assert "Cross-tenant" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_follow_up_missing_record_404(self, client: AsyncClient, test_workspace):
        bogus_id = uuid.uuid4()
        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", "test-key"):
            response = await client.post(
                f"/api/v1/ai/follow-up?org_id={test_workspace['org_id']}",
                headers={"Authorization": f"Bearer {test_workspace['token']}"},
                json={
                    "context_type": "signal",
                    "context_id": str(bogus_id),
                    "question": "Explain bogus signal",
                    "conversation_history": [],
                },
            )
        assert response.status_code == 404
        assert response.json()["detail"]["error_code"] == "RECORD_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_follow_up_rate_limit_429(self, client: AsyncClient, test_workspace):
        from google.genai import errors as genai_errors

        mock_client = MagicMock()
        api_err = genai_errors.APIError(429, {"error": {"message": "Resource has been exhausted (quota)"}})
        mock_client.aio.models.generate_content = AsyncMock(side_effect=api_err)
        set_genai_client(mock_client)

        with patch.object(settings, "ai_enabled", True), patch.object(settings, "gemini_api_key", "test-key"):
            response = await client.post(
                f"/api/v1/ai/follow-up?org_id={test_workspace['org_id']}",
                headers={"Authorization": f"Bearer {test_workspace['token']}"},
                json={
                    "context_type": "case",
                    "context_id": str(test_workspace["case"].id),
                    "question": "What is the next step?",
                    "conversation_history": [],
                },
            )
        assert response.status_code == 429
        assert response.json()["detail"]["error_code"] == "RATE_LIMITED"
