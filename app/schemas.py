"""
Pydantic v2 schemas (request / response DTOs).

Naming convention:
  *Create  – body for POST endpoints
  *Update  – body for PATCH endpoints (all fields optional)
  *Out     – response shape (camelCase via alias_generator)

We use `model_config = ConfigDict(populate_by_name=True)` so tests can
pass snake_case dicts directly.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ── Shared ────────────────────────────────────────────────────────────────────

class _Base(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        use_enum_values=True,
        str_strip_whitespace=True,
        from_attributes=True,   # allow ORM → schema conversion
    )


SeverityLiteral = Literal["critical", "high", "medium", "low", "info"]
PriorityInt = int  # 1-5


# ── Pagination ────────────────────────────────────────────────────────────────

class PageMeta(_Base):
    total: int
    limit: int
    offset: int


class Paginated[T](_Base):
    items: list[T]
    meta: PageMeta


# ── Organizations ─────────────────────────────────────────────────────────────

class OrgCreate(_Base):
    slug: str = Field(..., min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    name: str = Field(..., min_length=1, max_length=255)
    settings: dict[str, Any] = Field(default_factory=dict)


class OrgOut(_Base):
    id: uuid.UUID
    slug: str
    name: str
    status: str
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# ── Assets ────────────────────────────────────────────────────────────────────

class AssetCreate(_Base):
    name: str = Field(..., min_length=1, max_length=255)
    asset_type: str = Field(default="generic", max_length=60)
    criticality: int = Field(default=3, ge=1, le=5)
    external_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssetUpdate(_Base):
    name: str | None = Field(None, min_length=1)
    asset_type: str | None = None
    criticality: int | None = Field(None, ge=1, le=5)
    status: Literal["active", "inactive", "retired"] | None = None
    metadata: dict[str, Any] | None = None


class AssetOut(_Base):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    asset_type: str
    criticality: int
    status: str
    external_key: str | None
    metadata_: dict[str, Any] = Field(alias="metadata_", serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime


# ── Incidents ─────────────────────────────────────────────────────────────────

class IncidentCreate(_Base):
    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = None
    category: str | None = None
    asset_id: uuid.UUID | None = None
    source: str = "api"
    external_key: str | None = None
    impact: int | None = Field(None, ge=1, le=5)
    urgency: int | None = Field(None, ge=1, le=5)
    priority: int = Field(..., ge=1, le=5)
    sla_due_at: datetime | None = None


class IncidentUpdate(_Base):
    status: Literal["open", "acknowledged", "resolved", "closed", "cancelled"] | None = None
    priority: int | None = Field(None, ge=1, le=5)
    impact: int | None = Field(None, ge=1, le=5)
    urgency: int | None = Field(None, ge=1, le=5)
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    description: str | None = None


class IncidentOut(_Base):
    id: uuid.UUID
    org_id: uuid.UUID
    asset_id: uuid.UUID | None
    external_key: str | None
    source: str
    category: str | None
    title: str
    description: str | None
    status: str
    impact: int | None
    urgency: int | None
    priority: int
    priority_score: float
    priority_explanation: dict[str, Any]
    opened_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None
    sla_due_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ── Rules ─────────────────────────────────────────────────────────────────────

class RuleCreate(_Base):
    key: str = Field(..., pattern=r"^[a-z][a-z0-9_.-]{1,99}$")
    name: str = Field(..., min_length=1)
    description: str | None = None
    enabled: bool = True


class RuleVersionCreate(_Base):
    engine: Literal["threshold", "rolling_window", "expression", "model"]
    definition: dict[str, Any]
    default_severity: SeverityLiteral
    default_priority: int = Field(..., ge=1, le=5)


class RuleOut(_Base):
    id: uuid.UUID
    org_id: uuid.UUID
    key: str
    name: str
    description: str | None
    enabled: bool
    active_version_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class RuleVersionOut(_Base):
    id: uuid.UUID
    rule_id: uuid.UUID
    version: int
    engine: str
    definition: dict[str, Any]
    default_severity: str
    default_priority: int
    published_at: datetime | None
    retired_at: datetime | None
    created_at: datetime


# ── Findings ──────────────────────────────────────────────────────────────────

class FindingCreate(_Base):
    fingerprint: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = None
    severity: SeverityLiteral
    priority: int = Field(..., ge=1, le=5)
    priority_score: float = Field(..., ge=0, le=1000)
    priority_explanation: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    asset_id: uuid.UUID | None = None
    incident_id: uuid.UUID | None = None
    rule_version_id: uuid.UUID | None = None


class FindingUpdate(_Base):
    status: Literal["open", "acknowledged", "suppressed", "resolved"] | None = None
    severity: SeverityLiteral | None = None
    priority: int | None = Field(None, ge=1, le=5)
    description: str | None = None


class FindingOut(_Base):
    id: uuid.UUID
    org_id: uuid.UUID
    asset_id: uuid.UUID | None
    incident_id: uuid.UUID | None
    rule_version_id: uuid.UUID | None
    fingerprint: str
    title: str
    description: str | None
    severity: str
    priority: int
    priority_score: float
    priority_explanation: dict[str, Any]
    evidence: dict[str, Any]
    status: str
    first_seen_at: datetime
    last_seen_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ── Finding Occurrences ───────────────────────────────────────────────────────

class OccurrenceCreate(_Base):
    observed_at: datetime
    metric: str | None = None
    value: float | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    raw_event_id: uuid.UUID | None = None


class OccurrenceOut(_Base):
    id: uuid.UUID
    finding_id: uuid.UUID
    raw_event_id: uuid.UUID | None
    observed_at: datetime
    metric: str | None
    value: float | None
    evidence: dict[str, Any]
    created_at: datetime


# ── Cases ─────────────────────────────────────────────────────────────────────

class CaseCreate(_Base):
    title: str = Field(..., min_length=1, max_length=500)
    priority: int = Field(..., ge=1, le=5)
    finding_ids: list[uuid.UUID] = Field(default_factory=list)
    assigned_to: uuid.UUID | None = None
    external_key: str | None = None
    due_at: datetime | None = None

    @field_validator("assigned_to", mode="before")
    @classmethod
    def parse_assigned_to(cls, v: Any) -> uuid.UUID | None:
        if not v:
            return None
        if isinstance(v, uuid.UUID):
            return v
        try:
            return uuid.UUID(str(v))
        except (ValueError, TypeError):
            return None


class CaseUpdate(_Base):
    status: Literal["open", "in_progress", "waiting", "resolved", "closed", "cancelled"] | None = None
    priority: int | None = Field(None, ge=1, le=5)
    assigned_to: uuid.UUID | None = None
    due_at: datetime | None = None
    resolution_notes: str | None = None

    @field_validator("assigned_to", mode="before")
    @classmethod
    def parse_assigned_to(cls, v: Any) -> uuid.UUID | None:
        if not v:
            return None
        if isinstance(v, uuid.UUID):
            return v
        try:
            return uuid.UUID(str(v))
        except (ValueError, TypeError):
            return None



class CaseOut(_Base):
    id: uuid.UUID
    org_id: uuid.UUID
    external_key: str | None
    title: str
    status: str
    priority: int
    assigned_to: uuid.UUID | None
    due_at: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None
    resolution_notes: str | None
    created_at: datetime
    updated_at: datetime


# ── Ingest (raw events) ───────────────────────────────────────────────────────

class RawEventCreate(_Base):
    source: str = Field(..., min_length=1)
    source_event_id: str | None = None
    event_type: str = "telemetry"
    occurred_at: datetime
    asset_id: uuid.UUID | None = None
    payload: dict[str, Any] | list[Any]


class RawEventOut(_Base):
    id: uuid.UUID
    org_id: uuid.UUID
    asset_id: uuid.UUID | None
    source: str
    event_type: str
    occurred_at: datetime
    received_at: datetime
    processing_status: str


class IngestBatchCreate(_Base):
    events: list[RawEventCreate] = Field(..., min_length=1, max_length=500)


class IngestBatchOut(_Base):
    accepted: int
    rejected: int
    ids: list[uuid.UUID]


# ── SLA Policies ─────────────────────────────────────────────────────────────

class SlaPolicyCreate(_Base):
    name: str = Field(..., min_length=1)
    priority: int = Field(..., ge=1, le=5)
    target_minutes: int = Field(..., gt=0)
    active: bool = True


class SlaPolicyOut(_Base):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    priority: int
    target_minutes: int
    active: bool
    created_at: datetime
    updated_at: datetime


# ── Reports ───────────────────────────────────────────────────────────────────

class SlaBreachRow(_Base):
    priority: int
    total_incidents: int
    breached: int
    breach_rate_pct: float
    p95_resolution_minutes: float | None


class TopAnomalousAsset(_Base):
    asset_id: uuid.UUID
    high_severity_alerts: int
    metrics: list[str]


class ReportOut(_Base):
    generated_at: datetime
    sla_summary: list[SlaBreachRow]
    top_anomalous_assets: list[TopAnomalousAsset]


# ── Health ────────────────────────────────────────────────────────────────────

class HealthOut(_Base):
    status: Literal["ok", "degraded", "down"]
    database: Literal["ok", "error"]
    cache: Literal["ok", "unavailable"]
    version: str


# ── AI Triage & Simulation ────────────────────────────────────────────────────

class FactorOut(_Base):
    label: str
    detail: str
    contribution: float


class AITriageRequest(_Base):
    title: str = Field(..., min_length=1)
    severity: SeverityLiteral
    asset_type: str = "Generic"
    asset_criticality: int = Field(3, ge=1, le=5)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    occurrence_count: int = Field(1, ge=1)
    impact: int | None = Field(None, ge=1, le=5)
    urgency: int | None = Field(None, ge=1, le=5)


class AITriageResponse(_Base):
    predicted_priority: int
    priority_score: float
    sla_breach_probability: float
    confidence_score: float
    root_cause_summary: str
    recommended_actions: list[str]
    factors: list[FactorOut]
    metadata: dict[str, Any] = Field(default_factory=dict)


class CounterfactualRequest(_Base):
    concurrency_capacity: int = Field(3, ge=1, le=20)
    incident_count: int = Field(200, ge=10, le=1000)


class CounterfactualResponse(_Base):
    total_incidents: int
    baseline_breaches: int
    baseline_breach_rate_pct: float
    baseline_p95_resolution_minutes: float
    ai_breaches: int
    ai_breach_rate_pct: float
    ai_p95_resolution_minutes: float
    breach_reduction_count: int
    breach_reduction_pct: float
    p95_improvement_minutes: float
    high_priority_sla_compliance_pct: float


class HvacEvalResponse(_Base):
    total_samples: int
    ground_truth_faults: int
    detected_faults: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1_score: float
    early_detection_lead_minutes: float


# ── Authentication & Security ─────────────────────────────────────────────────

class LoginRequest(_Base):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=6)
    org_slug: str | None = None


class RegisterRequest(_Base):
    org_name: str = Field(..., min_length=2, max_length=255)
    org_slug: str = Field(..., min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8)
    display_name: str | None = None


class UserCreate(_Base):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8)
    display_name: str | None = Field(None, min_length=1, max_length=255)
    role: Literal["admin", "analyst", "operator", "viewer"] = "viewer"


class RefreshTokenRequest(_Base):
    refresh_token: str = Field(..., min_length=1)


class UserProfileOut(_Base):
    id: uuid.UUID
    email: str | None
    display_name: str | None
    org_id: uuid.UUID
    role: str


class TokenResponse(_Base):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserProfileOut


class LogoutResponse(_Base):
    message: str = "Successfully logged out"


class ChangePasswordRequest(_Base):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8, max_length=128)

