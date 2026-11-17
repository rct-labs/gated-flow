"""Data contracts, canonicalization, and errors for the standalone learner."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

LIFECYCLE_STATES = ("seen", "matched", "proposed", "closed")
SOURCE_CLASSES = ("github_trending", "github_popular", "github_releases", "arxiv")
CLAIM_KINDS = ("source_claim", "model_inference", "verified_observation")
TRUST_CLASSES = (
    "public_official",
    "public_community",
    "public_academic",
    "private_project",
)
OUTCOME_STATUSES = ("accepted", "rejected", "inconclusive")
RETENTION_POLICIES = ("purge_private", "retain_redacted")
DISPATCH_STATES = ("reserved", "dispatched", "settled", "unknown", "released", "failed")
TRIAGE_DECISIONS = ("reject", "defer", "qualify")

PILOT_MAX_METADATA_ITEMS_PER_DAY = 60
PILOT_MAX_HYDROGEN_CALLS_PER_DAY = 5
PILOT_MAX_DEEP_CANDIDATES_PER_WEEK = 2
PILOT_MAX_CARBON_CALLS_PER_WEEK = 2
PILOT_MAX_DAILY_FETCHES = 10
PILOT_MAX_WALL_SECONDS = 1200.0

_TRACKING_PARAMS = frozenset(
    {
        "ref",
        "ref_src",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "source",
    }
)
_ARXIV_VERSION_RE = re.compile(r"^(/abs/|/pdf/)([^/]+?)(v\d+)?(?:\.pdf)?$")
_SEMVER_RE = re.compile(r"^[vV](\d+(?:\.\d+)*(?:[-+][A-Za-z0-9._-]+)?)$")


class LearnerError(Exception):
    """Base error for all learner failures."""


class ValidationError(LearnerError):
    """Raised when input contracts or state transitions are invalid."""


class PrivacyViolationError(LearnerError):
    """Raised when private terms or data attempt to cross a public boundary."""


class UnsafeUrlError(LearnerError):
    """Raised when a URL targets a forbidden scheme or private/local network."""


class LeaseConflictError(LearnerError):
    """Raised when an overlap lease is already held by another active run."""


class DispatchBlockedError(LearnerError):
    """Raised when budget, learning enablement, or unknown recovery blocks dispatch."""

    def __init__(self, reason: str, message: str, reservation_id: str | None = None):
        super().__init__(message)
        self.reason = reason
        self.reservation_id = reservation_id


class IntegrityError(LearnerError):
    """Raised when immutable content-addressed storage detects tampering."""


def sha256_bytes(data: bytes) -> str:
    """Return lowercase hexadecimal SHA-256 digest for raw bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_json(payload: Any) -> str:
    """Return deterministic SHA-256 digest for JSON-serializable data."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return sha256_bytes(encoded)


def canonicalize_version(version: str | None) -> str:
    """Normalize version strings (e.g. 'v1.2.0' -> '1.2.0') without inventing missing versions."""
    if not version:
        return ""
    cleaned = " ".join(str(version).strip().split())
    if not cleaned:
        return ""
    match = _SEMVER_RE.match(cleaned)
    if match:
        return match.group(1)
    return cleaned


def canonicalize_url(url: str) -> str:
    """Canonicalize a URL for deduplication while preserving meaningful path/query."""
    raw = str(url or "").strip()
    if not raw:
        raise ValidationError("URL cannot be empty")
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if not scheme or not parts.netloc:
        raise ValidationError(f"Invalid URL: {url!r}")
    hostname = (parts.hostname or "").lower()
    if not hostname:
        raise ValidationError(f"URL missing host: {url!r}")
    port = parts.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        netloc = f"{netloc}:{port}"

    path = parts.path or "/"
    if hostname in ("github.com", "www.github.com"):
        hostname = "github.com"
        netloc = f"github.com:{port}" if port is not None else "github.com"
        segments = [seg for seg in path.split("/") if seg]
        if len(segments) >= 2 and segments[0] not in ("search", "trending", "topics", "orgs"):
            segments[0] = segments[0].lower()
            segments[1] = segments[1].lower()
            if segments[1].endswith(".git"):
                segments[1] = segments[1][:-4]
        path = "/" + "/".join(segments) if segments else "/"
    elif hostname == "arxiv.org":
        match = _ARXIV_VERSION_RE.match(path)
        if match:
            path = f"/abs/{match.group(2)}"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    filtered_query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    filtered_query.sort()
    query = urlencode(filtered_query)
    return urlunsplit((scheme, netloc, path, query, ""))


def make_source_id(canonical_url: str, canonical_version: str = "") -> str:
    """Derive a deterministic source identifier from canonical URL and version."""
    return sha256_json({"url": canonical_url, "version": canonical_version})[:24]


def _tuple_of_strs(values: Any) -> tuple[str, ...]:
    if not values:
        return ()
    return tuple(str(v).strip() for v in values if str(v).strip())


def _require_strict_int(
    value: Any,
    field_name: str,
    *,
    min_value: int = 0,
    max_value: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field_name} must be an integer, got {type(value).__name__}")
    if value < min_value:
        raise ValidationError(f"{field_name} must be >= {min_value}, got {value}")
    if max_value is not None and value > max_value:
        raise ValidationError(f"{field_name} must be <= {max_value}, got {value}")
    return value


def _require_strict_float(
    value: Any,
    field_name: str,
    *,
    min_value: float = 0.0,
    max_value: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field_name} must be a number, got {type(value).__name__}")
    fval = float(value)
    if fval != fval or fval <= min_value:
        raise ValidationError(f"{field_name} must be > {min_value}, got {value}")
    if max_value is not None and fval > max_value:
        raise ValidationError(f"{field_name} must be <= {max_value}, got {value}")
    return fval


@dataclass(frozen=True)
class SourcePolicy:
    allowed_classes: tuple[str, ...] = SOURCE_CLASSES
    allowed_hosts: tuple[str, ...] = ()
    max_items_per_source: int = 20
    max_items_per_sweep: int = 60

    def __post_init__(self) -> None:
        unknown = [c for c in self.allowed_classes if c not in SOURCE_CLASSES]
        if unknown:
            raise ValidationError(f"Unsupported source classes: {unknown}")
        _require_strict_int(
            self.max_items_per_source,
            "max_items_per_source",
            min_value=1,
            max_value=PILOT_MAX_METADATA_ITEMS_PER_DAY,
        )
        _require_strict_int(
            self.max_items_per_sweep,
            "max_items_per_sweep",
            min_value=1,
            max_value=PILOT_MAX_METADATA_ITEMS_PER_DAY,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_classes": list(self.allowed_classes),
            "allowed_hosts": list(self.allowed_hosts),
            "max_items_per_source": self.max_items_per_source,
            "max_items_per_sweep": self.max_items_per_sweep,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SourcePolicy":
        if not data:
            return cls()
        if not isinstance(data, dict):
            raise ValidationError("source_policy must be an object")
        return cls(
            allowed_classes=_tuple_of_strs(data.get("allowed_classes", SOURCE_CLASSES)),
            allowed_hosts=_tuple_of_strs(data.get("allowed_hosts", ())),
            max_items_per_source=_require_strict_int(
                data.get("max_items_per_source", 20),
                "max_items_per_source",
                min_value=1,
                max_value=PILOT_MAX_METADATA_ITEMS_PER_DAY,
            ),
            max_items_per_sweep=_require_strict_int(
                data.get("max_items_per_sweep", 60),
                "max_items_per_sweep",
                min_value=1,
                max_value=PILOT_MAX_METADATA_ITEMS_PER_DAY,
            ),
        )


@dataclass(frozen=True)
class ExportPolicy:
    allow_public_concept_queries: bool = True
    allow_shared_public_evidence: bool = True
    private_retention: str = "purge_private"

    def __post_init__(self) -> None:
        if self.private_retention not in RETENTION_POLICIES:
            raise ValidationError(f"Invalid private_retention: {self.private_retention!r}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ExportPolicy":
        if not data:
            return cls()
        if not isinstance(data, dict):
            raise ValidationError("export_policy must be an object")
        return cls(
            allow_public_concept_queries=bool(data.get("allow_public_concept_queries", True)),
            allow_shared_public_evidence=bool(data.get("allow_shared_public_evidence", True)),
            private_retention=str(data.get("private_retention", "purge_private")),
        )


@dataclass(frozen=True)
class BudgetPolicy:
    daily_calls: int = 5
    weekly_deep_calls: int = 2
    daily_fetches: int = 10

    def __post_init__(self) -> None:
        _require_strict_int(self.daily_calls, "daily_calls", min_value=0, max_value=100)
        _require_strict_int(self.weekly_deep_calls, "weekly_deep_calls", min_value=0, max_value=20)
        _require_strict_int(self.daily_fetches, "daily_fetches", min_value=0, max_value=100)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BudgetPolicy":
        if not data:
            return cls()
        if not isinstance(data, dict):
            raise ValidationError("budget must be an object")
        return cls(
            daily_calls=_require_strict_int(data.get("daily_calls", 5), "daily_calls", min_value=0, max_value=100),
            weekly_deep_calls=_require_strict_int(
                data.get("weekly_deep_calls", 2), "weekly_deep_calls", min_value=0, max_value=20
            ),
            daily_fetches=_require_strict_int(
                data.get("daily_fetches", 10), "daily_fetches", min_value=0, max_value=100
            ),
        )


@dataclass(frozen=True)
class SharedEnvelope:
    """Single shared persistent pilot envelope across both modes and all registered profiles."""

    daily_metadata_items: int = PILOT_MAX_METADATA_ITEMS_PER_DAY
    daily_hydrogen_calls: int = PILOT_MAX_HYDROGEN_CALLS_PER_DAY
    weekly_deep_candidates: int = PILOT_MAX_DEEP_CANDIDATES_PER_WEEK
    weekly_carbon_calls: int = PILOT_MAX_CARBON_CALLS_PER_WEEK
    daily_fetches: int = PILOT_MAX_DAILY_FETCHES
    wall_limit_s: float = PILOT_MAX_WALL_SECONDS
    max_unvalidated_proposals: int = 3
    max_packet_bytes: int = 24_000
    max_primary_evidence_bytes: int = 262_144

    def __post_init__(self) -> None:
        _require_strict_int(
            self.daily_metadata_items,
            "daily_metadata_items",
            min_value=1,
            max_value=PILOT_MAX_METADATA_ITEMS_PER_DAY,
        )
        _require_strict_int(
            self.daily_hydrogen_calls,
            "daily_hydrogen_calls",
            min_value=0,
            max_value=PILOT_MAX_HYDROGEN_CALLS_PER_DAY,
        )
        _require_strict_int(
            self.weekly_deep_candidates,
            "weekly_deep_candidates",
            min_value=0,
            max_value=PILOT_MAX_DEEP_CANDIDATES_PER_WEEK,
        )
        _require_strict_int(
            self.weekly_carbon_calls,
            "weekly_carbon_calls",
            min_value=0,
            max_value=PILOT_MAX_CARBON_CALLS_PER_WEEK,
        )
        _require_strict_int(
            self.daily_fetches,
            "daily_fetches",
            min_value=0,
            max_value=PILOT_MAX_DAILY_FETCHES,
        )
        _require_strict_float(self.wall_limit_s, "wall_limit_s", min_value=0.0, max_value=PILOT_MAX_WALL_SECONDS)
        _require_strict_int(self.max_unvalidated_proposals, "max_unvalidated_proposals", min_value=1, max_value=20)
        _require_strict_int(self.max_packet_bytes, "max_packet_bytes", min_value=512, max_value=100_000)
        _require_strict_int(
            self.max_primary_evidence_bytes, "max_primary_evidence_bytes", min_value=512, max_value=524_288
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProjectProfile:
    project_id: str
    name: str
    root_path: str
    adapter: str
    mission: str
    stage: str = "active"
    existing_capabilities: tuple[str, ...] = ()
    goals: tuple[str, ...] = ()
    known_gaps: tuple[str, ...] = ()
    desired_outcomes: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    public_concepts: tuple[str, ...] = ()
    private_terms: tuple[str, ...] = ()
    source_policy: SourcePolicy = field(default_factory=SourcePolicy)
    export_policy: ExportPolicy = field(default_factory=ExportPolicy)
    learning_enabled: bool = True
    execution_paused: bool = False
    budget: BudgetPolicy = field(default_factory=BudgetPolicy)
    provenance: dict[str, Any] = field(default_factory=dict)
    revision: int = 1
    status: str = "active"

    def __post_init__(self) -> None:
        if not self.project_id or not self.project_id.strip():
            raise ValidationError("project_id is required")
        if not self.name or not self.name.strip():
            raise ValidationError("name is required")
        if not self.root_path or not self.root_path.strip():
            raise ValidationError("root_path is required")
        if not self.adapter or not self.adapter.strip():
            raise ValidationError("adapter is required")
        if not self.mission or not self.mission.strip():
            raise ValidationError("mission is required")
        _require_strict_int(self.revision, "revision", min_value=1)
        if self.status not in ("active", "draft", "unregistered"):
            raise ValidationError(f"Invalid profile status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "root_path": self.root_path,
            "adapter": self.adapter,
            "mission": self.mission,
            "stage": self.stage,
            "existing_capabilities": list(self.existing_capabilities),
            "goals": list(self.goals),
            "known_gaps": list(self.known_gaps),
            "desired_outcomes": list(self.desired_outcomes),
            "exclusions": list(self.exclusions),
            "public_concepts": list(self.public_concepts),
            "private_terms": list(self.private_terms),
            "source_policy": self.source_policy.to_dict(),
            "export_policy": self.export_policy.to_dict(),
            "learning_enabled": self.learning_enabled,
            "execution_paused": self.execution_paused,
            "budget": self.budget.to_dict(),
            "provenance": dict(self.provenance),
            "revision": self.revision,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectProfile":
        return cls(
            project_id=str(data["project_id"]).strip(),
            name=str(data["name"]).strip(),
            root_path=str(data["root_path"]).strip(),
            adapter=str(data.get("adapter", "standalone")).strip(),
            mission=str(data["mission"]).strip(),
            stage=str(data.get("stage", "active")).strip(),
            existing_capabilities=_tuple_of_strs(data.get("existing_capabilities")),
            goals=_tuple_of_strs(data.get("goals")),
            known_gaps=_tuple_of_strs(data.get("known_gaps")),
            desired_outcomes=_tuple_of_strs(data.get("desired_outcomes")),
            exclusions=_tuple_of_strs(data.get("exclusions")),
            public_concepts=_tuple_of_strs(data.get("public_concepts")),
            private_terms=_tuple_of_strs(data.get("private_terms")),
            source_policy=SourcePolicy.from_dict(data.get("source_policy")),
            export_policy=ExportPolicy.from_dict(data.get("export_policy")),
            learning_enabled=bool(data.get("learning_enabled", True)),
            execution_paused=bool(data.get("execution_paused", False)),
            budget=BudgetPolicy.from_dict(data.get("budget")),
            provenance=dict(data.get("provenance") or {}),
            revision=_require_strict_int(data.get("revision", 1), "revision", min_value=1),
            status=str(data.get("status", "active")),
        )


@dataclass(frozen=True)
class ProjectProfileDraft:
    profile: ProjectProfile
    activated: bool = False
    source_docs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "activated": self.activated,
            "source_docs": list(self.source_docs),
            "warnings": list(self.warnings),
            "profile": self.profile.to_dict(),
        }


@dataclass(frozen=True)
class SourceArtifact:
    source_id: str
    canonical_url: str
    raw_url: str
    source_class: str
    publisher: str
    title: str
    summary: str
    canonical_version: str
    raw_version: str
    content_digest: str
    evidence_digest: str
    trust_class: str
    observed_at: float
    released_at: str | None = None
    license_id: str | None = None
    retrieval_status: str = "ok"
    is_actual_trending: bool = False
    measured_star_velocity: bool = False
    popularity_signal_only: bool = True
    degraded_fallback: bool = False
    untrusted_directive_flags: tuple[str, ...] = ()
    source_claims: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source_class not in SOURCE_CLASSES:
            raise ValidationError(f"Invalid source_class: {self.source_class!r}")
        if self.trust_class not in TRUST_CLASSES:
            raise ValidationError(f"Invalid trust_class: {self.trust_class!r}")
        if self.trust_class == "private_project":
            raise PrivacyViolationError("Private project records cannot be stored as shared SourceArtifact")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "canonical_url": self.canonical_url,
            "raw_url": self.raw_url,
            "source_class": self.source_class,
            "publisher": self.publisher,
            "title": self.title,
            "summary": self.summary,
            "canonical_version": self.canonical_version,
            "raw_version": self.raw_version,
            "content_digest": self.content_digest,
            "evidence_digest": self.evidence_digest,
            "trust_class": self.trust_class,
            "observed_at": self.observed_at,
            "released_at": self.released_at,
            "license_id": self.license_id,
            "retrieval_status": self.retrieval_status,
            "is_actual_trending": self.is_actual_trending,
            "measured_star_velocity": self.measured_star_velocity,
            "popularity_signal_only": self.popularity_signal_only,
            "degraded_fallback": self.degraded_fallback,
            "untrusted_directive_flags": list(self.untrusted_directive_flags),
            "source_claims": list(self.source_claims),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class IngestionCoverage:
    run_id: str
    source_class: str
    query_key: str
    mode: str
    status: str
    incomplete: bool
    items_found: int
    items_new: int
    error: str | None
    last_success_at: float | None
    recorded_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AssessmentContext:
    assessment_id: str
    project_id: str
    profile_revision: int
    source_id: str
    source_digest: str
    canonical_url: str
    source_version: str
    lifecycle_state: str
    last_completed_step: str
    decision: str
    reason: str
    reopen_triggers: tuple[str, ...]
    goal_ref: str | None = None
    capability_ref: str | None = None
    capability_overlap: tuple[str, ...] = ()
    assessment_status: str = "unconfigured"
    claim_kind: str = "source_claim"
    source_claims: tuple[dict[str, Any], ...] = ()
    model_inferences: tuple[dict[str, Any], ...] = ()
    verified_observations: tuple[dict[str, Any], ...] = ()
    predecessor_id: str | None = None
    superseded_by: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if self.lifecycle_state not in LIFECYCLE_STATES:
            raise ValidationError(f"Invalid lifecycle_state: {self.lifecycle_state!r}")
        if self.claim_kind not in CLAIM_KINDS:
            raise ValidationError(f"Invalid claim_kind: {self.claim_kind!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "project_id": self.project_id,
            "profile_revision": self.profile_revision,
            "source_id": self.source_id,
            "source_digest": self.source_digest,
            "canonical_url": self.canonical_url,
            "source_version": self.source_version,
            "lifecycle_state": self.lifecycle_state,
            "last_completed_step": self.last_completed_step,
            "decision": self.decision,
            "reason": self.reason,
            "reopen_triggers": list(self.reopen_triggers),
            "goal_ref": self.goal_ref,
            "capability_ref": self.capability_ref,
            "capability_overlap": list(self.capability_overlap),
            "assessment_status": self.assessment_status,
            "claim_kind": self.claim_kind,
            "source_claims": list(self.source_claims),
            "model_inferences": list(self.model_inferences),
            "verified_observations": list(self.verified_observations),
            "predecessor_id": self.predecessor_id,
            "superseded_by": self.superseded_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class OutcomeReference:
    outcome_id: str
    project_id: str
    assessment_id: str
    external_experiment_ref: str
    external_release_ref: str | None
    status: str
    baseline_summary: str
    outcome_summary: str
    evaluator: str
    feedback_note: str | None
    recorded_at: float

    def __post_init__(self) -> None:
        if self.status not in OUTCOME_STATUSES:
            raise ValidationError(f"Invalid outcome status: {self.status!r}")
        if not self.external_experiment_ref or not self.external_experiment_ref.strip():
            raise ValidationError("external_experiment_ref is required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DispatchReservation:
    reservation_id: str
    idempotency_key: str
    project_id: str
    scope_type: str
    budget_bucket: str
    units: int
    window_key: str
    state: str
    actual_units: int | None
    receipt: dict[str, Any] | None
    error: str | None
    created_at: float
    updated_at: float
    lease_expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
