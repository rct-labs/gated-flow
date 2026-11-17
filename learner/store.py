"""Transactional SQLite store and immutable content-addressed evidence storage."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Iterator, Sequence
import uuid

from learner.contracts import (
    AssessmentContext,
    DispatchBlockedError,
    DispatchReservation,
    IngestionCoverage,
    IntegrityError,
    LeaseConflictError,
    LIFECYCLE_STATES,
    OutcomeReference,
    PILOT_MAX_CARBON_CALLS_PER_WEEK,
    PILOT_MAX_DAILY_FETCHES,
    PILOT_MAX_DEEP_CANDIDATES_PER_WEEK,
    PILOT_MAX_HYDROGEN_CALLS_PER_DAY,
    PILOT_MAX_METADATA_ITEMS_PER_DAY,
    PrivacyViolationError,
    ProjectProfile,
    RETENTION_POLICIES,
    SourceArtifact,
    ValidationError,
    canonicalize_url,
    canonicalize_version,
    make_source_id,
    sha256_bytes,
    sha256_json,
)
from learner.privacy import contains_secret_like_term

_APPLICABILITY_FIELDS = (
    "mission",
    "existing_capabilities",
    "goals",
    "known_gaps",
    "desired_outcomes",
    "exclusions",
    "public_concepts",
)
MODEL_DISPATCH_SCOPES = (
    "shared_triage",
    "query_planning",
    "deep_hydrogen",
    "deep_carbon",
    "deep_synthesis",
    "model_call",
)
_TRIAL_SETTING_KEY = "trial:grant"
_TRIAL_HISTORY_KEY = "trial:history"
_UNCAPPED_MODEL_BUCKETS = frozenset({"daily_calls", "weekly_deep_calls", "weekly_carbon_calls"})


def _parse_trial_expiry(value: float | int | str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        fval = float(value)
        if not math.isfinite(fval) or fval <= 0:
            raise ValidationError("expires_at must be a finite positive timestamp")
        return fval
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValidationError(f"Invalid ISO-8601 expires_at: {exc}") from exc
        if dt.tzinfo is None:
            raise ValidationError("expires_at ISO-8601 timestamp must include timezone")
        return dt.timestamp()
    raise ValidationError("expires_at must be an epoch timestamp or timezone-aware ISO-8601 string")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS project_profiles (
    project_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    learning_enabled INTEGER NOT NULL,
    execution_paused INTEGER NOT NULL,
    profile_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_revisions (
    project_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    profile_json TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    PRIMARY KEY (project_id, revision)
);

CREATE TABLE IF NOT EXISTS evidence_blobs (
    digest TEXT PRIMARY KEY,
    visibility TEXT NOT NULL,
    project_id TEXT,
    byte_size INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS source_artifacts (
    source_id TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL,
    canonical_version TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    source_class TEXT NOT NULL,
    trust_class TEXT NOT NULL,
    observed_at REAL NOT NULL,
    artifact_json TEXT NOT NULL,
    UNIQUE(canonical_url, canonical_version)
);

CREATE TABLE IF NOT EXISTS ingestion_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_class TEXT NOT NULL,
    query_key TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    incomplete INTEGER NOT NULL,
    items_found INTEGER NOT NULL,
    items_new INTEGER NOT NULL,
    error TEXT,
    last_success_at REAL,
    recorded_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS assessments (
    assessment_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    profile_revision INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    source_version TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    last_completed_step TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    reopen_triggers_json TEXT NOT NULL,
    goal_ref TEXT,
    capability_ref TEXT,
    capability_overlap_json TEXT NOT NULL,
    assessment_status TEXT NOT NULL,
    claim_kind TEXT NOT NULL,
    source_claims_json TEXT NOT NULL,
    model_inferences_json TEXT NOT NULL,
    verified_observations_json TEXT NOT NULL,
    predecessor_id TEXT,
    superseded_by TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assessments_project_active
ON assessments(project_id, superseded_by, lifecycle_state);

CREATE TABLE IF NOT EXISTS outcome_references (
    outcome_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    assessment_id TEXT NOT NULL,
    external_experiment_ref TEXT NOT NULL,
    external_release_ref TEXT,
    status TEXT NOT NULL,
    baseline_summary TEXT NOT NULL,
    outcome_summary TEXT NOT NULL,
    evaluator TEXT NOT NULL,
    feedback_note TEXT,
    recorded_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatch_reservations (
    reservation_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    project_id TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    budget_bucket TEXT NOT NULL,
    units INTEGER NOT NULL,
    window_key TEXT NOT NULL,
    state TEXT NOT NULL,
    actual_units INTEGER,
    receipt_json TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    lease_expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatch_window
ON dispatch_reservations(project_id, budget_bucket, window_key, state);

CREATE TABLE IF NOT EXISTS dispatch_scopes (
    reservation_id TEXT PRIMARY KEY,
    target_project_ids_json TEXT NOT NULL,
    step_keys_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    lock_name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS learner_settings (
    setting_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class LearnerStore:
    """Durable SQLite + immutable content-addressed store for standalone learning."""

    def __init__(self, storage_root: str | Path, clock: Callable[[], float] | None = None) -> None:
        self.storage_root = Path(storage_root).resolve()
        self.db_path = self.storage_root / "learner.sqlite3"
        self.evidence_dir = self.storage_root / "evidence"
        self.clock: Callable[[], float] = clock or time.time
        self._lock = threading.RLock()
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._conn().executescript(_SCHEMA_SQL)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA busy_timeout=30000;")
            self._local.conn = conn
            self._connections.append(conn)
        return conn

    def close(self) -> None:
        with self._lock:
            while self._connections:
                conn = self._connections.pop()
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            if hasattr(self._local, "conn"):
                delattr(self._local, "conn")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE;")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            else:
                conn.execute("COMMIT;")

    def now(self) -> float:
        return float(self.clock())

    def window_key(self, bucket: str, at_ts: float | None = None) -> str:
        ts = self.now() if at_ts is None else at_ts
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if bucket in ("weekly_deep_calls", "weekly_carbon_calls"):
            iso = dt.isocalendar()
            return f"{iso.year}-W{iso.week:02d}"
        return dt.strftime("%Y-%m-%d")

    # ---- Profiles ----

    def register_profile(self, profile: ProjectProfile) -> ProjectProfile:
        if profile.status == "draft":
            profile = ProjectProfile.from_dict(profile.to_dict() | {"status": "active"})
        if profile.status != "active":
            raise ValidationError("register_profile requires an active profile")
        now = self.now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT revision, status FROM project_profiles WHERE project_id = ?",
                (profile.project_id,),
            ).fetchone()
            if row is not None and row["status"] == "active":
                raise ValidationError(f"Profile {profile.project_id!r} is already registered")
            next_rev = max(profile.revision, (int(row["revision"]) + 1) if row is not None else 1)
            stored = ProjectProfile.from_dict(profile.to_dict() | {"revision": next_rev, "status": "active"})
            payload = json.dumps(stored.to_dict(), sort_keys=True)
            conn.execute(
                """
                INSERT INTO project_profiles(
                    project_id, revision, status, learning_enabled, execution_paused,
                    profile_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    revision = excluded.revision,
                    status = excluded.status,
                    learning_enabled = excluded.learning_enabled,
                    execution_paused = excluded.execution_paused,
                    profile_json = excluded.profile_json,
                    updated_at = excluded.updated_at
                """,
                (
                    stored.project_id,
                    stored.revision,
                    stored.status,
                    int(stored.learning_enabled),
                    int(stored.execution_paused),
                    payload,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO profile_revisions(project_id, revision, profile_json, change_summary, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (stored.project_id, stored.revision, payload, "registered", now),
            )
        return stored

    def get_profile(self, project_id: str) -> ProjectProfile | None:
        with self._lock:
            row = self._conn().execute(
                "SELECT profile_json, status FROM project_profiles WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None or row["status"] == "unregistered":
                return None
            return ProjectProfile.from_dict(json.loads(row["profile_json"]))

    def list_profiles(self, *, include_unregistered: bool = False) -> list[ProjectProfile]:
        with self._lock:
            sql = "SELECT profile_json, status FROM project_profiles"
            if not include_unregistered:
                sql += " WHERE status = 'active'"
            sql += " ORDER BY project_id"
            rows = self._conn().execute(sql).fetchall()
            out: list[ProjectProfile] = []
            for row in rows:
                data = json.loads(row["profile_json"])
                if data.get("status") == "redacted_unregistered":
                    continue
                out.append(ProjectProfile.from_dict(data))
            return out

    def get_profile_revisions(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn().execute(
                """
                SELECT revision, change_summary, recorded_at, profile_json
                FROM profile_revisions WHERE project_id = ? ORDER BY revision ASC
                """,
                (project_id,),
            ).fetchall()
            return [
                {
                    "revision": int(r["revision"]),
                    "change_summary": r["change_summary"],
                    "recorded_at": float(r["recorded_at"]),
                    "profile": json.loads(r["profile_json"]),
                }
                for r in rows
            ]

    def revise_profile(
        self,
        project_id: str,
        updates: dict[str, Any],
        *,
        note: str = "",
    ) -> tuple[ProjectProfile, list[str]]:
        """Create a new profile revision and reopen only applicability affected by changed fields."""
        now = self.now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT profile_json, status FROM project_profiles WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None or row["status"] != "active":
                raise ValidationError(f"Active profile not found: {project_id!r}")
            current = ProjectProfile.from_dict(json.loads(row["profile_json"]))
            merged = current.to_dict()
            for key, value in updates.items():
                if key in ("project_id", "revision", "status"):
                    continue
                merged[key] = value
            merged["revision"] = current.revision + 1
            merged["status"] = "active"
            prov = dict(merged.get("provenance") or {})
            prov["revised_at"] = now
            if note:
                prov["revision_note"] = note
            merged["provenance"] = prov
            revised = ProjectProfile.from_dict(merged)

            changed_fields = [
                k for k in current.to_dict()
                if k not in ("revision", "provenance") and current.to_dict()[k] != revised.to_dict()[k]
            ]
            changed_app = [f for f in changed_fields if f in _APPLICABILITY_FIELDS]

            payload = json.dumps(revised.to_dict(), sort_keys=True)
            conn.execute(
                """
                UPDATE project_profiles
                SET revision = ?, learning_enabled = ?, execution_paused = ?,
                    profile_json = ?, updated_at = ?
                WHERE project_id = ?
                """,
                (
                    revised.revision,
                    int(revised.learning_enabled),
                    int(revised.execution_paused),
                    payload,
                    now,
                    project_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO profile_revisions(project_id, revision, profile_json, change_summary, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (project_id, revised.revision, payload, ",".join(changed_fields) or "metadata_only", now),
            )

            reopened_ids: list[str] = []
            if changed_app:
                reopened_ids = self._invalidate_affected_assessments(
                    conn, current, revised, changed_app, now
                )
        return revised, reopened_ids

    def _invalidate_affected_assessments(
        self,
        conn: sqlite3.Connection,
        old_profile: ProjectProfile,
        new_profile: ProjectProfile,
        changed_app: list[str],
        now: float,
    ) -> list[str]:
        rows = conn.execute(
            """
            SELECT * FROM assessments
            WHERE project_id = ? AND superseded_by IS NULL AND profile_revision < ?
            """,
            (old_profile.project_id, new_profile.revision),
        ).fetchall()
        global_change = any(
            f in changed_app
            for f in ("mission", "existing_capabilities", "known_gaps", "desired_outcomes", "exclusions", "public_concepts")
        )
        new_goals = set(new_profile.goals)
        reopened_ids: list[str] = []
        for row in rows:
            goal_ref = row["goal_ref"]
            triggers = json.loads(row["reopen_triggers_json"])
            is_closed = row["lifecycle_state"] == "closed"
            if is_closed and not any(t in triggers for t in ("profile_goal_changed", "profile_changed")):
                continue
            if not global_change and goal_ref is not None and goal_ref in new_goals:
                # Only goals changed, and this assessment's referenced goal is unchanged.
                continue

            old_id = row["assessment_id"]
            new_id = _new_id("asmt")
            reason = f"profile_revised_r{new_profile.revision}:changed={','.join(changed_app)}"
            conn.execute(
                """
                UPDATE assessments
                SET superseded_by = ?, decision = 'invalidated', reason = ?, updated_at = ?
                WHERE assessment_id = ?
                """,
                (new_id, reason, now, old_id),
            )
            conn.execute(
                """
                INSERT INTO assessments(
                    assessment_id, project_id, profile_revision, source_id, source_digest,
                    canonical_url, source_version, lifecycle_state, last_completed_step,
                    decision, reason, reopen_triggers_json, goal_ref, capability_ref,
                    capability_overlap_json, assessment_status, claim_kind,
                    source_claims_json, model_inferences_json, verified_observations_json,
                    predecessor_id, superseded_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'seen', 'reopened_by_profile_revision',
                    'pending', ?, ?, ?, ?, ?, 'unconfigured', 'source_claim',
                    ?, '[]', ?, ?, NULL, ?, ?)
                """,
                (
                    new_id,
                    row["project_id"],
                    new_profile.revision,
                    row["source_id"],
                    row["source_digest"],
                    row["canonical_url"],
                    row["source_version"],
                    reason,
                    row["reopen_triggers_json"],
                    goal_ref if (goal_ref is None or goal_ref in new_goals) else None,
                    row["capability_ref"],
                    row["capability_overlap_json"],
                    row["source_claims_json"],
                    row["verified_observations_json"],
                    old_id,
                    now,
                    now,
                ),
            )
            reopened_ids.append(new_id)
        return reopened_ids

    def set_learning_enabled(self, project_id: str, enabled: bool) -> ProjectProfile:
        """Toggle learning_enabled independently without altering execution_paused."""
        return self._set_flag(project_id, "learning_enabled", bool(enabled))

    def set_execution_paused(self, project_id: str, paused: bool) -> ProjectProfile:
        """Toggle execution_paused independently without altering learning_enabled."""
        return self._set_flag(project_id, "execution_paused", bool(paused))

    def _set_flag(self, project_id: str, field_name: str, value: bool) -> ProjectProfile:
        now = self.now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT profile_json, status FROM project_profiles WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None or row["status"] != "active":
                raise ValidationError(f"Active profile not found: {project_id!r}")
            data = json.loads(row["profile_json"])
            data[field_name] = value
            updated = ProjectProfile.from_dict(data)
            conn.execute(
                f"UPDATE project_profiles SET {field_name} = ?, profile_json = ?, updated_at = ? WHERE project_id = ?",
                (int(value), json.dumps(updated.to_dict(), sort_keys=True), now, project_id),
            )
        return updated

    def unregister_profile(
        self,
        project_id: str,
        *,
        retention: str | None = None,
    ) -> dict[str, Any]:
        """Unregister a profile, revoke pending work, and apply retention without touching shared public evidence."""
        now = self.now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT profile_json, status FROM project_profiles WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None or row["status"] != "active":
                raise ValidationError(f"Active profile not found: {project_id!r}")
            profile = ProjectProfile.from_dict(json.loads(row["profile_json"]))
            policy = retention or profile.export_policy.private_retention
            if policy not in RETENTION_POLICIES:
                raise ValidationError(f"Invalid retention policy: {policy!r}")

            revoked_cur = conn.execute(
                """
                UPDATE assessments
                SET lifecycle_state = 'closed', decision = 'revoked',
                    reason = 'profile_unregistered', last_completed_step = 'unregistered',
                    updated_at = ?
                WHERE project_id = ? AND superseded_by IS NULL AND lifecycle_state IN ('seen', 'matched')
                """,
                (now, project_id),
            )
            revoked_count = revoked_cur.rowcount

            released_cur = conn.execute(
                """
                UPDATE dispatch_reservations
                SET state = 'released', error = 'profile_unregistered', updated_at = ?
                WHERE project_id = ? AND state = 'reserved'
                """,
                (now, project_id),
            )
            released_count = released_cur.rowcount

            purged_blobs = 0
            private_rows = conn.execute(
                "SELECT digest FROM evidence_blobs WHERE visibility = 'private' AND project_id = ?",
                (project_id,),
            ).fetchall()
            if policy == "purge_private":
                for brow in private_rows:
                    path = self._evidence_path(brow["digest"])
                    if path.exists():
                        path.unlink()
                    purged_blobs += 1
                conn.execute(
                    "DELETE FROM evidence_blobs WHERE visibility = 'private' AND project_id = ?",
                    (project_id,),
                )
                conn.execute("DELETE FROM outcome_references WHERE project_id = ?", (project_id,))
                conn.execute("DELETE FROM assessments WHERE project_id = ?", (project_id,))
                conn.execute("DELETE FROM profile_revisions WHERE project_id = ?", (project_id,))
                conn.execute("DELETE FROM learner_settings WHERE setting_key LIKE ?", (f"project:{project_id}:%",))
                tombstone = json.dumps(
                    {
                        "project_id": project_id,
                        "status": "redacted_unregistered",
                        "retention": policy,
                        "unregistered_at": now,
                    },
                    sort_keys=True,
                )
                conn.execute(
                    """
                    UPDATE project_profiles
                    SET status = 'unregistered', learning_enabled = 0, execution_paused = 1,
                        profile_json = ?, updated_at = ?
                    WHERE project_id = ?
                    """,
                    (tombstone, now, project_id),
                )
            else:
                redacted = json.dumps(
                    {
                        "project_id": project_id,
                        "status": "redacted_unregistered",
                        "retention": policy,
                        "adapter": profile.adapter,
                        "revision": profile.revision,
                        "unregistered_at": now,
                    },
                    sort_keys=True,
                )
                conn.execute(
                    """
                    UPDATE project_profiles
                    SET status = 'unregistered', learning_enabled = 0, execution_paused = 1,
                        profile_json = ?, updated_at = ?
                    WHERE project_id = ?
                    """,
                    (redacted, now, project_id),
                )
                conn.execute(
                    "UPDATE profile_revisions SET profile_json = ? WHERE project_id = ?",
                    (redacted, project_id),
                )
                conn.execute(
                    "UPDATE outcome_references SET feedback_note = '[redacted]', baseline_summary = '[redacted]', outcome_summary = '[redacted]' WHERE project_id = ?",
                    (project_id,),
                )
                conn.execute(
                    "UPDATE learner_settings SET value_json = '{\"redacted\":true}' WHERE setting_key LIKE ?",
                    (f"project:{project_id}:%",),
                )

        return {
            "project_id": project_id,
            "retention": policy,
            "revoked_assessments": revoked_count,
            "released_reservations": released_count,
            "purged_private_blobs": purged_blobs,
        }

    # ---- Content-Addressed Immutable Evidence ----

    def _evidence_path(self, digest: str) -> Path:
        return self.evidence_dir / digest[:2] / digest

    def put_evidence(
        self,
        data: bytes,
        *,
        visibility: str = "public",
        project_id: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Persist immutable content-addressed bytes; private/internal data cannot enter public source evidence."""
        if visibility not in ("public", "private", "internal"):
            raise ValidationError(f"Invalid visibility: {visibility!r}")
        if visibility == "private" and not project_id:
            raise ValidationError("Private evidence requires project_id")
        if visibility == "public":
            if project_id is not None:
                raise PrivacyViolationError("Shared public evidence cannot be tagged with a private project_id")
            text = data.decode("utf-8", errors="ignore")
            if contains_secret_like_term(text):
                raise PrivacyViolationError("Blocked secret-like term in shared public evidence")
        elif visibility == "internal":
            text = data.decode("utf-8", errors="ignore")
            if contains_secret_like_term(text):
                raise PrivacyViolationError("Blocked secret-like term in internal evidence")

        digest = sha256_bytes(data)
        path = self._evidence_path(digest)
        now = self.now()
        with self._tx() as conn:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                existing = path.read_bytes()
                if sha256_bytes(existing) != digest or existing != data:
                    raise IntegrityError(f"Evidence digest mismatch at {digest}")
            else:
                path.write_bytes(data)

            row = conn.execute(
                "SELECT visibility FROM evidence_blobs WHERE digest = ?",
                (digest,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO evidence_blobs(digest, visibility, project_id, byte_size, content_type, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (digest, visibility, project_id, len(data), content_type, now),
                )
        return digest

    def get_evidence(self, digest: str) -> bytes:
        path = self._evidence_path(digest)
        if not path.is_file():
            raise ValidationError(f"Evidence blob not found: {digest}")
        data = path.read_bytes()
        if sha256_bytes(data) != digest:
            raise IntegrityError(f"Corrupted evidence blob for digest {digest}")
        return data

    # ---- Source Artifacts & Coverage ----

    def record_source_artifact(self, artifact: SourceArtifact) -> tuple[SourceArtifact, bool]:
        """Store shared public SourceArtifact deduplicated by canonical URL and version. Returns (artifact, is_new)."""
        if artifact.trust_class == "private_project":
            raise PrivacyViolationError("Private project material cannot be recorded as shared SourceArtifact")
        canonical_url = canonicalize_url(artifact.canonical_url)
        canonical_ver = canonicalize_version(artifact.canonical_version)
        source_id = make_source_id(canonical_url, canonical_ver)
        normalized = SourceArtifact(
            source_id=source_id,
            canonical_url=canonical_url,
            raw_url=artifact.raw_url,
            source_class=artifact.source_class,
            publisher=artifact.publisher,
            title=artifact.title,
            summary=artifact.summary,
            canonical_version=canonical_ver,
            raw_version=artifact.raw_version,
            content_digest=artifact.content_digest,
            evidence_digest=artifact.evidence_digest,
            trust_class=artifact.trust_class,
            observed_at=artifact.observed_at,
            released_at=artifact.released_at,
            license_id=artifact.license_id,
            retrieval_status=artifact.retrieval_status,
            is_actual_trending=artifact.is_actual_trending,
            measured_star_velocity=artifact.measured_star_velocity,
            popularity_signal_only=artifact.popularity_signal_only,
            degraded_fallback=artifact.degraded_fallback,
            untrusted_directive_flags=artifact.untrusted_directive_flags,
            source_claims=artifact.source_claims,
            metadata=artifact.metadata,
        )
        with self._tx() as conn:
            row = conn.execute(
                "SELECT artifact_json FROM source_artifacts WHERE canonical_url = ? AND canonical_version = ?",
                (canonical_url, canonical_ver),
            ).fetchone()
            if row is not None:
                existing = json.loads(row["artifact_json"])
                return self._artifact_from_dict(existing), False
            conn.execute(
                """
                INSERT INTO source_artifacts(
                    source_id, canonical_url, canonical_version, content_digest, evidence_digest,
                    source_class, trust_class, observed_at, artifact_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.source_id,
                    normalized.canonical_url,
                    normalized.canonical_version,
                    normalized.content_digest,
                    normalized.evidence_digest,
                    normalized.source_class,
                    normalized.trust_class,
                    normalized.observed_at,
                    json.dumps(normalized.to_dict(), sort_keys=True),
                ),
            )
        return normalized, True

    @staticmethod
    def _artifact_from_dict(data: dict[str, Any]) -> SourceArtifact:
        return SourceArtifact(
            source_id=data["source_id"],
            canonical_url=data["canonical_url"],
            raw_url=data["raw_url"],
            source_class=data["source_class"],
            publisher=data["publisher"],
            title=data["title"],
            summary=data["summary"],
            canonical_version=data["canonical_version"],
            raw_version=data["raw_version"],
            content_digest=data["content_digest"],
            evidence_digest=data["evidence_digest"],
            trust_class=data["trust_class"],
            observed_at=float(data["observed_at"]),
            released_at=data.get("released_at"),
            license_id=data.get("license_id"),
            retrieval_status=data.get("retrieval_status", "ok"),
            is_actual_trending=bool(data.get("is_actual_trending", False)),
            measured_star_velocity=bool(data.get("measured_star_velocity", False)),
            popularity_signal_only=bool(data.get("popularity_signal_only", True)),
            degraded_fallback=bool(data.get("degraded_fallback", False)),
            untrusted_directive_flags=tuple(data.get("untrusted_directive_flags", ())),
            source_claims=tuple(data.get("source_claims", ())),
            metadata=dict(data.get("metadata") or {}),
        )

    def list_source_artifacts(self, source_class: str | None = None) -> list[SourceArtifact]:
        with self._lock:
            if source_class:
                rows = self._conn().execute(
                    "SELECT artifact_json FROM source_artifacts WHERE source_class = ? ORDER BY observed_at DESC",
                    (source_class,),
                ).fetchall()
            else:
                rows = self._conn().execute(
                    "SELECT artifact_json FROM source_artifacts ORDER BY observed_at DESC"
                ).fetchall()
            return [self._artifact_from_dict(json.loads(r["artifact_json"])) for r in rows]

    def record_coverage(self, cov: IngestionCoverage) -> IngestionCoverage:
        """Record coverage/error independently of items, preserving last_success_at across later failures."""
        with self._tx() as conn:
            prev_row = conn.execute(
                """
                SELECT MAX(last_success_at) AS prev_success
                FROM ingestion_coverage
                WHERE source_class = ? AND query_key = ?
                """,
                (cov.source_class, cov.query_key),
            ).fetchone()
            prev_success = prev_row["prev_success"] if prev_row is not None else None
            effective_last_success = cov.last_success_at
            if cov.status == "ok" and not cov.incomplete:
                effective_last_success = cov.recorded_at if effective_last_success is None else effective_last_success
            elif effective_last_success is None:
                effective_last_success = prev_success

            stored = IngestionCoverage(
                run_id=cov.run_id,
                source_class=cov.source_class,
                query_key=cov.query_key,
                mode=cov.mode,
                status=cov.status,
                incomplete=cov.incomplete,
                items_found=cov.items_found,
                items_new=cov.items_new,
                error=cov.error,
                last_success_at=effective_last_success,
                recorded_at=cov.recorded_at,
            )
            conn.execute(
                """
                INSERT INTO ingestion_coverage(
                    run_id, source_class, query_key, mode, status, incomplete,
                    items_found, items_new, error, last_success_at, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored.run_id,
                    stored.source_class,
                    stored.query_key,
                    stored.mode,
                    stored.status,
                    int(stored.incomplete),
                    stored.items_found,
                    stored.items_new,
                    stored.error,
                    stored.last_success_at,
                    stored.recorded_at,
                ),
            )
        return stored

    def list_coverage(self, run_id: str | None = None) -> list[IngestionCoverage]:
        with self._lock:
            if run_id:
                rows = self._conn().execute(
                    "SELECT * FROM ingestion_coverage WHERE run_id = ? ORDER BY id ASC",
                    (run_id,),
                ).fetchall()
            else:
                rows = self._conn().execute(
                    "SELECT * FROM ingestion_coverage ORDER BY id ASC"
                ).fetchall()
            return [
                IngestionCoverage(
                    run_id=r["run_id"],
                    source_class=r["source_class"],
                    query_key=r["query_key"],
                    mode=r["mode"],
                    status=r["status"],
                    incomplete=bool(r["incomplete"]),
                    items_found=int(r["items_found"]),
                    items_new=int(r["items_new"]),
                    error=r["error"],
                    last_success_at=r["last_success_at"],
                    recorded_at=float(r["recorded_at"]),
                )
                for r in rows
            ]

    # ---- Assessments & Outcome References ----

    def ensure_assessment_for_artifact(
        self,
        profile: ProjectProfile,
        artifact: SourceArtifact,
        *,
        goal_ref: str | None = None,
        capability_ref: str | None = None,
        capability_overlap: Sequence[str] = (),
        reason: str = "ingested_for_review",
        reopen_triggers: Sequence[str] = ("new_source_version", "profile_goal_changed"),
    ) -> tuple[AssessmentContext, bool]:
        """Create or reuse project-scoped AssessmentContext, reopening with lineage only on new version."""
        now = self.now()
        with self._tx() as conn:
            # 1. Exact duplicate check for active profile_revision + source_id (or digest).
            existing = conn.execute(
                """
                SELECT * FROM assessments
                WHERE project_id = ? AND profile_revision = ? AND source_id = ? AND superseded_by IS NULL
                """,
                (profile.project_id, profile.revision, artifact.source_id),
            ).fetchone()
            if existing is not None:
                return self._assessment_from_row(existing), False

            # 2. Check for prior assessment on same canonical_url or release tag family.
            prior = conn.execute(
                """
                SELECT * FROM assessments
                WHERE project_id = ? AND canonical_url = ? AND superseded_by IS NULL
                ORDER BY updated_at DESC LIMIT 1
                """,
                (profile.project_id, artifact.canonical_url),
            ).fetchone()
            if prior is None and "/releases/tag/" in artifact.canonical_url:
                release_prefix = artifact.canonical_url.split("/releases/tag/", 1)[0] + "/releases/tag/%"
                prior = conn.execute(
                    """
                    SELECT * FROM assessments
                    WHERE project_id = ? AND canonical_url LIKE ? AND superseded_by IS NULL
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (profile.project_id, release_prefix),
                ).fetchone()

            predecessor_id: str | None = None
            new_id = _new_id("asmt")
            if prior is not None:
                if prior["source_digest"] == artifact.content_digest and int(prior["profile_revision"]) == profile.revision:
                    return self._assessment_from_row(prior), False
                triggers = json.loads(prior["reopen_triggers_json"])
                version_changed = prior["source_version"] != artifact.canonical_version or prior["source_digest"] != artifact.content_digest
                if prior["lifecycle_state"] == "closed" and version_changed and "new_source_version" not in triggers:
                    return self._assessment_from_row(prior), False
                predecessor_id = prior["assessment_id"]
                conn.execute(
                    "UPDATE assessments SET superseded_by = ?, updated_at = ? WHERE assessment_id = ?",
                    (new_id, now, predecessor_id),
                )
                if version_changed:
                    reason = f"reopened_new_source_version:{prior['source_version']}->{artifact.canonical_version}"

            asmt = AssessmentContext(
                assessment_id=new_id,
                project_id=profile.project_id,
                profile_revision=profile.revision,
                source_id=artifact.source_id,
                source_digest=artifact.content_digest,
                canonical_url=artifact.canonical_url,
                source_version=artifact.canonical_version,
                lifecycle_state="seen",
                last_completed_step="intake",
                decision="pending",
                reason=reason,
                reopen_triggers=tuple(reopen_triggers),
                goal_ref=goal_ref,
                capability_ref=capability_ref,
                capability_overlap=tuple(capability_overlap),
                assessment_status="unconfigured",
                claim_kind="source_claim",
                source_claims=artifact.source_claims,
                model_inferences=(),
                verified_observations=(),
                predecessor_id=predecessor_id,
                superseded_by=None,
                created_at=now,
                updated_at=now,
            )
            conn.execute(
                """
                INSERT INTO assessments(
                    assessment_id, project_id, profile_revision, source_id, source_digest,
                    canonical_url, source_version, lifecycle_state, last_completed_step,
                    decision, reason, reopen_triggers_json, goal_ref, capability_ref,
                    capability_overlap_json, assessment_status, claim_kind,
                    source_claims_json, model_inferences_json, verified_observations_json,
                    predecessor_id, superseded_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asmt.assessment_id,
                    asmt.project_id,
                    asmt.profile_revision,
                    asmt.source_id,
                    asmt.source_digest,
                    asmt.canonical_url,
                    asmt.source_version,
                    asmt.lifecycle_state,
                    asmt.last_completed_step,
                    asmt.decision,
                    asmt.reason,
                    json.dumps(list(asmt.reopen_triggers)),
                    asmt.goal_ref,
                    asmt.capability_ref,
                    json.dumps(list(asmt.capability_overlap)),
                    asmt.assessment_status,
                    asmt.claim_kind,
                    json.dumps(list(asmt.source_claims)),
                    json.dumps(list(asmt.model_inferences)),
                    json.dumps(list(asmt.verified_observations)),
                    asmt.predecessor_id,
                    asmt.superseded_by,
                    asmt.created_at,
                    asmt.updated_at,
                ),
            )
        return asmt, True

    def get_assessment(self, assessment_id: str) -> AssessmentContext | None:
        with self._lock:
            row = self._conn().execute(
                "SELECT * FROM assessments WHERE assessment_id = ?",
                (assessment_id,),
            ).fetchone()
            return self._assessment_from_row(row) if row is not None else None

    def transition_assessment(
        self,
        assessment_id: str,
        *,
        lifecycle_state: str,
        last_completed_step: str,
        decision: str,
        reason: str,
        reopen_triggers: Sequence[str] | None = None,
        model_inferences: Sequence[dict[str, Any]] | None = None,
        assessment_status: str | None = None,
        goal_ref: str | None = None,
        capability_ref: str | None = None,
        capability_overlap: Sequence[str] | None = None,
        source_claims: Sequence[dict[str, Any]] | None = None,
        claim_kind: str | None = None,
    ) -> AssessmentContext:
        if lifecycle_state not in LIFECYCLE_STATES:
            raise ValidationError(f"Invalid lifecycle_state: {lifecycle_state!r}")
        now = self.now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM assessments WHERE assessment_id = ?",
                (assessment_id,),
            ).fetchone()
            if row is None:
                raise ValidationError(f"Assessment not found: {assessment_id!r}")
            new_triggers = json.dumps(list(reopen_triggers)) if reopen_triggers is not None else row["reopen_triggers_json"]
            new_inf = json.dumps(list(model_inferences)) if model_inferences is not None else row["model_inferences_json"]
            new_status = assessment_status if assessment_status is not None else row["assessment_status"]
            new_goal = goal_ref if goal_ref is not None else row["goal_ref"]
            new_cap = capability_ref if capability_ref is not None else row["capability_ref"]
            new_overlap = (
                json.dumps(list(capability_overlap))
                if capability_overlap is not None
                else row["capability_overlap_json"]
            )
            new_claims = (
                json.dumps(list(source_claims))
                if source_claims is not None
                else row["source_claims_json"]
            )
            new_claim_kind = claim_kind if claim_kind is not None else row["claim_kind"]
            conn.execute(
                """
                UPDATE assessments
                SET lifecycle_state = ?, last_completed_step = ?, decision = ?, reason = ?,
                    reopen_triggers_json = ?, model_inferences_json = ?, assessment_status = ?,
                    goal_ref = ?, capability_ref = ?, capability_overlap_json = ?,
                    source_claims_json = ?, claim_kind = ?, updated_at = ?
                WHERE assessment_id = ?
                """,
                (
                    lifecycle_state,
                    last_completed_step,
                    decision,
                    reason,
                    new_triggers,
                    new_inf,
                    new_status,
                    new_goal,
                    new_cap,
                    new_overlap,
                    new_claims,
                    new_claim_kind,
                    now,
                    assessment_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM assessments WHERE assessment_id = ?",
                (assessment_id,),
            ).fetchone()
            return self._assessment_from_row(updated)

    def reopen_assessment_with_updated_evidence(
        self,
        assessment_id: str,
        *,
        source_claims: Sequence[dict[str, Any]],
        reason: str = "primary_evidence_materially_updated",
        last_completed_step: str = "primary_fetched",
        decision: str = "qualify",
        assessment_status: str = "assessed",
    ) -> AssessmentContext:
        """Supersede an assessment with lineage when primary evidence changes, preserving prior assessment and receipts."""
        now = self.now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM assessments WHERE assessment_id = ?",
                (assessment_id,),
            ).fetchone()
            if row is None:
                raise ValidationError(f"Assessment not found: {assessment_id!r}")
            if row["superseded_by"] is not None:
                raise ValidationError(f"Assessment {assessment_id!r} is already superseded")
            new_id = _new_id("asmt")
            conn.execute(
                "UPDATE assessments SET superseded_by = ?, updated_at = ? WHERE assessment_id = ?",
                (new_id, now, assessment_id),
            )
            conn.execute(
                """
                INSERT INTO assessments(
                    assessment_id, project_id, profile_revision, source_id, source_digest,
                    canonical_url, source_version, lifecycle_state, last_completed_step,
                    decision, reason, reopen_triggers_json, goal_ref, capability_ref,
                    capability_overlap_json, assessment_status, claim_kind,
                    source_claims_json, model_inferences_json, verified_observations_json,
                    predecessor_id, superseded_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'matched', ?, ?, ?, ?, ?, ?, ?, ?, 'source_claim',
                    ?, '[]', ?, ?, NULL, ?, ?)
                """,
                (
                    new_id,
                    row["project_id"],
                    int(row["profile_revision"]),
                    row["source_id"],
                    row["source_digest"],
                    row["canonical_url"],
                    row["source_version"],
                    last_completed_step,
                    decision,
                    reason[:200],
                    row["reopen_triggers_json"],
                    row["goal_ref"],
                    row["capability_ref"],
                    row["capability_overlap_json"],
                    assessment_status,
                    json.dumps(list(source_claims)),
                    row["verified_observations_json"],
                    assessment_id,
                    now,
                    now,
                ),
            )
            created = conn.execute(
                "SELECT * FROM assessments WHERE assessment_id = ?",
                (new_id,),
            ).fetchone()
            return self._assessment_from_row(created)

    def count_unvalidated_proposals(self, project_id: str) -> int:
        with self._lock:
            row = self._conn().execute(
                """
                SELECT COUNT(*) AS cnt FROM assessments
                WHERE project_id = ? AND superseded_by IS NULL
                  AND lifecycle_state = 'proposed'
                  AND verified_observations_json = '[]'
                """,
                (project_id,),
            ).fetchone()
            return int(row["cnt"]) if row is not None else 0

    def reopen_backlog_deferred_assessments(self, project_id: str) -> list[str]:
        now = self.now()
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT assessment_id FROM assessments
                WHERE project_id = ? AND superseded_by IS NULL
                  AND decision = 'deferred_proposal_backlog'
                """,
                (project_id,),
            ).fetchall()
            reopened: list[str] = []
            for r in rows:
                conn.execute(
                    """
                    UPDATE assessments
                    SET decision = 'pending', reason = 'reopened_after_outcome_feedback', updated_at = ?
                    WHERE assessment_id = ?
                    """,
                    (now, r["assessment_id"]),
                )
                reopened.append(r["assessment_id"])
            return reopened

    def get_setting(self, setting_key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn().execute(
                "SELECT value_json FROM learner_settings WHERE setting_key = ?",
                (setting_key,),
            ).fetchone()
            if row is None:
                return default
            return json.loads(row["value_json"])

    def set_setting(self, setting_key: str, value: Any) -> None:
        now = self.now()
        payload = json.dumps(value, sort_keys=True)
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO learner_settings(setting_key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (setting_key, payload, now),
            )

    def list_assessments(
        self,
        project_id: str | None = None,
        *,
        include_superseded: bool = False,
    ) -> list[AssessmentContext]:
        with self._lock:
            clauses: list[str] = []
            params: list[Any] = []
            if project_id is not None:
                clauses.append("project_id = ?")
                params.append(project_id)
            if not include_superseded:
                clauses.append("superseded_by IS NULL")
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = self._conn().execute(
                f"SELECT * FROM assessments {where} ORDER BY created_at ASC, assessment_id ASC",
                params,
            ).fetchall()
            return [self._assessment_from_row(r) for r in rows]

    @staticmethod
    def _assessment_from_row(row: sqlite3.Row) -> AssessmentContext:
        return AssessmentContext(
            assessment_id=row["assessment_id"],
            project_id=row["project_id"],
            profile_revision=int(row["profile_revision"]),
            source_id=row["source_id"],
            source_digest=row["source_digest"],
            canonical_url=row["canonical_url"],
            source_version=row["source_version"],
            lifecycle_state=row["lifecycle_state"],
            last_completed_step=row["last_completed_step"],
            decision=row["decision"],
            reason=row["reason"],
            reopen_triggers=tuple(json.loads(row["reopen_triggers_json"])),
            goal_ref=row["goal_ref"],
            capability_ref=row["capability_ref"],
            capability_overlap=tuple(json.loads(row["capability_overlap_json"])),
            assessment_status=row["assessment_status"],
            claim_kind=row["claim_kind"],
            source_claims=tuple(json.loads(row["source_claims_json"])),
            model_inferences=tuple(json.loads(row["model_inferences_json"])),
            verified_observations=tuple(json.loads(row["verified_observations_json"])),
            predecessor_id=row["predecessor_id"],
            superseded_by=row["superseded_by"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def record_outcome_reference(
        self,
        *,
        project_id: str,
        assessment_id: str,
        external_experiment_ref: str,
        status: str,
        baseline_summary: str,
        outcome_summary: str,
        evaluator: str,
        external_release_ref: str | None = None,
        feedback_note: str | None = None,
    ) -> OutcomeReference:
        """Store a lightweight outcome reference (not an experiment ledger) and attach a verified observation."""
        now = self.now()
        outcome = OutcomeReference(
            outcome_id=_new_id("out"),
            project_id=project_id,
            assessment_id=assessment_id,
            external_experiment_ref=external_experiment_ref,
            external_release_ref=external_release_ref,
            status=status,
            baseline_summary=baseline_summary,
            outcome_summary=outcome_summary,
            evaluator=evaluator,
            feedback_note=feedback_note,
            recorded_at=now,
        )
        with self._tx() as conn:
            asmt_row = conn.execute(
                "SELECT project_id, verified_observations_json FROM assessments WHERE assessment_id = ?",
                (assessment_id,),
            ).fetchone()
            if asmt_row is None or asmt_row["project_id"] != project_id:
                raise ValidationError(f"Assessment {assessment_id!r} not found for project {project_id!r}")
            conn.execute(
                """
                INSERT INTO outcome_references(
                    outcome_id, project_id, assessment_id, external_experiment_ref,
                    external_release_ref, status, baseline_summary, outcome_summary,
                    evaluator, feedback_note, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome.outcome_id,
                    outcome.project_id,
                    outcome.assessment_id,
                    outcome.external_experiment_ref,
                    outcome.external_release_ref,
                    outcome.status,
                    outcome.baseline_summary,
                    outcome.outcome_summary,
                    outcome.evaluator,
                    outcome.feedback_note,
                    outcome.recorded_at,
                ),
            )
            observations = list(json.loads(asmt_row["verified_observations_json"]))
            observations.append(
                {
                    "claim_kind": "verified_observation",
                    "outcome_id": outcome.outcome_id,
                    "status": outcome.status,
                    "external_experiment_ref": outcome.external_experiment_ref,
                    "external_release_ref": outcome.external_release_ref,
                    "evaluator": outcome.evaluator,
                    "recorded_at": outcome.recorded_at,
                }
            )
            conn.execute(
                "UPDATE assessments SET verified_observations_json = ?, updated_at = ? WHERE assessment_id = ?",
                (json.dumps(observations), now, assessment_id),
            )
        return outcome

    def list_outcomes(self, project_id: str | None = None) -> list[OutcomeReference]:
        with self._lock:
            if project_id:
                rows = self._conn().execute(
                    "SELECT * FROM outcome_references WHERE project_id = ? ORDER BY recorded_at ASC",
                    (project_id,),
                ).fetchall()
            else:
                rows = self._conn().execute(
                    "SELECT * FROM outcome_references ORDER BY recorded_at ASC"
                ).fetchall()
            return [
                OutcomeReference(
                    outcome_id=r["outcome_id"],
                    project_id=r["project_id"],
                    assessment_id=r["assessment_id"],
                    external_experiment_ref=r["external_experiment_ref"],
                    external_release_ref=r["external_release_ref"],
                    status=r["status"],
                    baseline_summary=r["baseline_summary"],
                    outcome_summary=r["outcome_summary"],
                    evaluator=r["evaluator"],
                    feedback_note=r["feedback_note"],
                    recorded_at=float(r["recorded_at"]),
                )
                for r in rows
            ]

    # ---- Leases, Atomic Budget Reservation & Crash Recovery ----

    def acquire_lease(self, lock_name: str, owner_id: str, *, ttl_s: float = 300.0) -> bool:
        now = self.now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT owner_id, expires_at FROM leases WHERE lock_name = ?",
                (lock_name,),
            ).fetchone()
            if row is not None and float(row["expires_at"]) > now and row["owner_id"] != owner_id:
                raise LeaseConflictError(f"Lease {lock_name!r} is currently held by {row['owner_id']!r}")
            conn.execute(
                """
                INSERT INTO leases(lock_name, owner_id, acquired_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lock_name) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at
                """,
                (lock_name, owner_id, now, now + ttl_s),
            )
        return True

    def release_lease(self, lock_name: str, owner_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM leases WHERE lock_name = ? AND owner_id = ?",
                (lock_name, owner_id),
            )

    @staticmethod
    def _shared_envelope_limit(budget_bucket: str) -> int:
        limits = {
            "daily_fetches": PILOT_MAX_DAILY_FETCHES,
            "daily_calls": PILOT_MAX_HYDROGEN_CALLS_PER_DAY,
            "weekly_deep_calls": PILOT_MAX_DEEP_CANDIDATES_PER_WEEK,
            "weekly_carbon_calls": PILOT_MAX_CARBON_CALLS_PER_WEEK,
            "daily_metadata_items": PILOT_MAX_METADATA_ITEMS_PER_DAY,
        }
        if budget_bucket not in limits:
            raise ValidationError(f"Unknown shared budget_bucket: {budget_bucket!r}")
        return limits[budget_bucket]

    def _compute_shared_cap(self, conn: sqlite3.Connection, budget_bucket: str, cap_override: int | None) -> int | None:
        hard_cap = self._shared_envelope_limit(budget_bucket)
        active_rows = conn.execute(
            "SELECT profile_json FROM project_profiles WHERE status = 'active' AND learning_enabled = 1"
        ).fetchall()
        if not active_rows:
            raise DispatchBlockedError("no_active_profiles", "No active learning-enabled profiles registered")
        if cap_override is not None:
            if isinstance(cap_override, bool) or not isinstance(cap_override, int) or cap_override < 0:
                raise ValidationError(f"Invalid cap_override: {cap_override!r}")
        if budget_bucket in _UNCAPPED_MODEL_BUCKETS and self._is_unlimited_active_tx(conn):
            return None
        if cap_override is not None:
            return min(cap_override, hard_cap)
        if budget_bucket == "daily_fetches":
            profile_min = min(
                ProjectProfile.from_dict(json.loads(r["profile_json"])).budget.daily_fetches
                for r in active_rows
            )
            return min(hard_cap, profile_min)
        return hard_cap

    def remaining_shared_budget(self, budget_bucket: str, *, cap_override: int | None = None, at_ts: float | None = None) -> int | None:
        window = self.window_key(budget_bucket, at_ts)
        with self._lock:
            conn = self._conn()
            cap = self._compute_shared_cap(conn, budget_bucket, cap_override)
            if cap is None:
                return None
            used = self.budget_used("__shared__", budget_bucket, at_ts=at_ts)
            return max(0, cap - used)

    def get_shared_budget_status(self, *, at_ts: float | None = None) -> dict[str, dict[str, Any]]:
        """Return per-bucket shared usage and effective limit (`None` when uncapped)."""
        buckets = (
            "daily_calls",
            "weekly_deep_calls",
            "weekly_carbon_calls",
            "daily_metadata_items",
            "daily_fetches",
        )
        with self._lock:
            conn = self._conn()
            unlimited_active = self._is_unlimited_active_tx(conn)
            active_rows = conn.execute(
                "SELECT profile_json FROM project_profiles WHERE status = 'active' AND learning_enabled = 1"
            ).fetchall()
            out: dict[str, dict[str, Any]] = {}
            for bucket in buckets:
                used = self.budget_used("__shared__", bucket, at_ts=at_ts)
                if unlimited_active and bucket in _UNCAPPED_MODEL_BUCKETS:
                    limit: int | None = None
                else:
                    hard_cap = self._shared_envelope_limit(bucket)
                    if bucket == "daily_fetches" and active_rows:
                        profile_min = min(
                            ProjectProfile.from_dict(json.loads(r["profile_json"])).budget.daily_fetches
                            for r in active_rows
                        )
                        limit = min(hard_cap, profile_min)
                    else:
                        limit = hard_cap
                out[bucket] = {
                    "used": used,
                    "limit": limit,
                    "remaining": None if limit is None else max(0, limit - used),
                    "unlimited": limit is None,
                }
            return out

    @staticmethod
    def _parse_trial_grant_mode(grant: dict[str, Any]) -> str:
        mode = str(grant.get("mode") or "finite").strip().lower()
        return mode if mode in ("finite", "unlimited") else "finite"

    @classmethod
    def _is_unlimited_active_tx(cls, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT value_json FROM learner_settings WHERE setting_key = ?",
            (_TRIAL_SETTING_KEY,),
        ).fetchone()
        if row is None:
            return False
        grant = json.loads(row["value_json"])
        return cls._parse_trial_grant_mode(grant) == "unlimited" and bool(grant.get("enabled"))

    @staticmethod
    def _load_trial_history_tx(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        row = conn.execute(
            "SELECT value_json FROM learner_settings WHERE setting_key = ?",
            (_TRIAL_HISTORY_KEY,),
        ).fetchone()
        if row is None:
            return []
        data = json.loads(row["value_json"])
        return list(data) if isinstance(data, list) else []

    @staticmethod
    def _cumulative_model_calls_tx(conn: sqlite3.Connection) -> int:
        placeholders = ",".join("?" for _ in MODEL_DISPATCH_SCOPES)
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(
                CASE
                    WHEN state = 'settled' THEN COALESCE(actual_units, units)
                    WHEN state IN ('reserved', 'dispatched', 'unknown') THEN units
                    ELSE 0
                END
            ), 0) AS used
            FROM dispatch_reservations
            WHERE scope_type IN ({placeholders})
            """,
            MODEL_DISPATCH_SCOPES,
        ).fetchone()
        return int(row["used"]) if row is not None else 0

    def set_trial_grant(
        self,
        *,
        max_model_calls: int | None = None,
        expires_at: float | int | str | None = None,
        unlimited: bool = False,
        grant_id: str | None = None,
        scope: str = "flow_pilot",
        granted_by: str | None = None,
        authority: str | None = None,
    ) -> dict[str, Any]:
        """Persist an explicit finite or uncapped local model-dispatch authorization (preserving history)."""
        if not str(scope or "").strip():
            raise ValidationError("trial scope must be non-empty")
        authority_clean = str(authority).strip() if authority is not None and str(authority).strip() else None
        if unlimited:
            if max_model_calls is not None or expires_at is not None:
                raise ValidationError("unlimited grant must not specify finite max_model_calls or expires_at")
            if not authority_clean:
                raise ValidationError("authority is required when enabling unlimited mode")
            exp_ts: float | None = None
            eff_granted_by = str(granted_by or "owner").strip()
        else:
            if max_model_calls is None or expires_at is None:
                raise ValidationError("finite trial grant requires max_model_calls and expires_at")
            if isinstance(max_model_calls, bool) or not isinstance(max_model_calls, int) or max_model_calls < 0:
                raise ValidationError("max_model_calls must be a non-negative integer")
            exp_ts = _parse_trial_expiry(expires_at)
            eff_granted_by = str(granted_by or "host").strip()

        now = self.now()
        with self._tx() as conn:
            history = self._load_trial_history_tx(conn)
            existing_row = conn.execute(
                "SELECT value_json FROM learner_settings WHERE setting_key = ?",
                (_TRIAL_SETTING_KEY,),
            ).fetchone()
            baseline = self._cumulative_model_calls_tx(conn)
            effective_id = str(grant_id).strip() if grant_id and str(grant_id).strip() else _new_id("trial")
            prior_ids = {
                str(h.get("grant_id"))
                for h in history
                if isinstance(h, dict) and h.get("grant_id")
            }
            prev_grant: dict[str, Any] | None = None
            if existing_row is not None:
                prev_grant = json.loads(existing_row["value_json"])
                prev_id = prev_grant.get("grant_id")
                if prev_id:
                    prior_ids.add(str(prev_id))
                if not grant_id or not str(grant_id).strip() or effective_id in prior_ids:
                    raise ValidationError(
                        "Existing trial grant cannot be implicitly reset; supply a distinct explicit grant_id"
                    )
            elif effective_id in prior_ids:
                raise ValidationError(f"grant_id {effective_id!r} was already used in grant history")

            if prev_grant is not None:
                archived = dict(prev_grant)
                archived["superseded_at"] = now
                archived["superseded_by_grant_id"] = effective_id
                archived["cumulative_calls_at_supersede"] = baseline
                history.append(archived)
                conn.execute(
                    """
                    INSERT INTO learner_settings(setting_key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(setting_key) DO UPDATE SET
                        value_json = excluded.value_json,
                        updated_at = excluded.updated_at
                    """,
                    (_TRIAL_HISTORY_KEY, json.dumps(history, sort_keys=True), now),
                )

            grant: dict[str, Any] = {
                "mode": "unlimited" if unlimited else "finite",
                "enabled": True,
                "grant_id": effective_id,
                "scope": str(scope).strip(),
                "granted_by": eff_granted_by,
                "authority": authority_clean,
                "granted_at": now,
                "expires_at": exp_ts,
                "max_model_calls": None if unlimited else max_model_calls,
                "baseline_calls": baseline,
                "prior_grant_id": prev_grant.get("grant_id") if prev_grant is not None else None,
            }
            conn.execute(
                """
                INSERT INTO learner_settings(setting_key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (_TRIAL_SETTING_KEY, json.dumps(grant, sort_keys=True), now),
            )
        return self.get_trial_status()

    def revoke_trial_grant(self, *, reason: str = "revoked") -> dict[str, Any]:
        """Deactivate any active trial grant while preserving cumulative reservation history."""
        now = self.now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT value_json FROM learner_settings WHERE setting_key = ?",
                (_TRIAL_SETTING_KEY,),
            ).fetchone()
            if row is not None:
                grant = json.loads(row["value_json"])
                grant["enabled"] = False
                grant["revoked_at"] = now
                grant["revoke_reason"] = str(reason or "revoked")
                conn.execute(
                    "UPDATE learner_settings SET value_json = ?, updated_at = ? WHERE setting_key = ?",
                    (json.dumps(grant, sort_keys=True), now, _TRIAL_SETTING_KEY),
                )
        return self.get_trial_status()

    def get_trial_status(self, *, at_ts: float | None = None) -> dict[str, Any]:
        """Return persisted trial grant state, mode, expiry, and cumulative usage (default inactive)."""
        now = self.now() if at_ts is None else float(at_ts)
        with self._lock:
            conn = self._conn()
            total = self._cumulative_model_calls_tx(conn)
            history = self._load_trial_history_tx(conn)
            row = conn.execute(
                "SELECT value_json FROM learner_settings WHERE setting_key = ?",
                (_TRIAL_SETTING_KEY,),
            ).fetchone()
            if row is None:
                return {
                    "state": "inactive",
                    "mode": "inactive",
                    "unlimited": False,
                    "enabled": False,
                    "active": False,
                    "grant_id": None,
                    "scope": None,
                    "granted_by": None,
                    "authority": None,
                    "granted_at": None,
                    "used": 0,
                    "cumulative_model_calls": total,
                    "remaining": None,
                    "max_model_calls": None,
                    "expires_at": None,
                    "grant_history": history,
                }
            grant = json.loads(row["value_json"])
            mode = self._parse_trial_grant_mode(grant)
            baseline = int(grant.get("baseline_calls") or 0)
            used = max(0, total - baseline)
            enabled = bool(grant.get("enabled"))
            if mode == "unlimited":
                state = "active" if enabled else "revoked"
                return {
                    "state": state,
                    "mode": "unlimited",
                    "unlimited": state == "active",
                    "enabled": enabled,
                    "active": state == "active",
                    "grant_id": grant.get("grant_id"),
                    "scope": grant.get("scope"),
                    "granted_by": grant.get("granted_by"),
                    "authority": grant.get("authority"),
                    "granted_at": grant.get("granted_at"),
                    "used": used,
                    "cumulative_model_calls": total,
                    "remaining": None,
                    "max_model_calls": None,
                    "expires_at": None,
                    "prior_grant_id": grant.get("prior_grant_id"),
                    "grant_history": history,
                    **({"revoked_at": grant.get("revoked_at"), "revoke_reason": grant.get("revoke_reason")} if not enabled and grant.get("revoked_at") is not None else {}),
                }

            max_calls = int(grant.get("max_model_calls") or 0)
            exp_ts = float(grant["expires_at"])
            if now >= exp_ts:
                state = "expired"
            elif used >= max_calls:
                state = "exhausted"
            elif not enabled:
                state = "revoked"
            else:
                state = "active"
            return {
                "state": state,
                "mode": "finite",
                "unlimited": False,
                "enabled": enabled,
                "active": state == "active",
                "grant_id": grant.get("grant_id"),
                "scope": grant.get("scope"),
                "granted_by": grant.get("granted_by"),
                "authority": grant.get("authority"),
                "granted_at": grant.get("granted_at"),
                "used": used,
                "cumulative_model_calls": total,
                "remaining": max(0, max_calls - used),
                "max_model_calls": max_calls,
                "expires_at": exp_ts,
                "prior_grant_id": grant.get("prior_grant_id"),
                "grant_history": history,
            }

    @classmethod
    def _check_trial_grant_tx(
        cls,
        conn: sqlite3.Connection,
        *,
        scope_type: str,
        units: int,
        now: float,
        require_trial_grant: bool = False,
    ) -> None:
        if scope_type not in MODEL_DISPATCH_SCOPES:
            return
        row = conn.execute(
            "SELECT value_json FROM learner_settings WHERE setting_key = ?",
            (_TRIAL_SETTING_KEY,),
        ).fetchone()
        if row is None:
            if require_trial_grant:
                raise DispatchBlockedError(
                    "trial_inactive",
                    "Live model dispatch requires an active trial grant",
                )
            return
        grant = json.loads(row["value_json"])
        mode = cls._parse_trial_grant_mode(grant)
        if mode == "unlimited":
            if not grant.get("enabled"):
                raise DispatchBlockedError(
                    "trial_revoked",
                    f"Local trial grant {grant.get('grant_id')!r} is revoked",
                )
            return
        exp_ts = float(grant["expires_at"])
        if now >= exp_ts:
            raise DispatchBlockedError(
                "trial_expired",
                f"Local trial grant {grant.get('grant_id')!r} expired at {exp_ts}",
            )
        total = cls._cumulative_model_calls_tx(conn)
        used = max(0, total - int(grant.get("baseline_calls") or 0))
        max_calls = int(grant.get("max_model_calls") or 0)
        if used + units > max_calls:
            raise DispatchBlockedError(
                "trial_ceiling_exhausted",
                f"Local trial ceiling exhausted ({used}+{units}>{max_calls}) without reset",
            )
        if not grant.get("enabled"):
            raise DispatchBlockedError(
                "trial_revoked",
                f"Local trial grant {grant.get('grant_id')!r} is revoked",
            )

    def reserve_dispatch(
        self,
        *,
        idempotency_key: str,
        project_id: str,
        scope_type: str,
        budget_bucket: str,
        units: int = 1,
        ttl_s: float = 300.0,
        cap_override: int | None = None,
        target_project_ids: Sequence[str] = (),
        step_keys: Sequence[str] = (),
        require_trial_grant: bool = False,
    ) -> tuple[DispatchReservation, bool]:
        """Atomically reserve budget before dispatch. Returns (reservation, should_dispatch)."""
        if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
            raise ValidationError("Reservation units must be a positive integer")
        if cap_override is not None and (isinstance(cap_override, bool) or not isinstance(cap_override, int) or cap_override < 0):
            raise ValidationError("cap_override must be a non-negative integer")
        now = self.now()
        window = self.window_key(budget_bucket, now)
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT * FROM dispatch_reservations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                res = self._reservation_from_row(existing)
                if res.state == "unknown":
                    raise DispatchBlockedError(
                        "unknown_pending_reconciliation",
                        f"Idempotency key {idempotency_key!r} is in unknown state and requires reconciliation",
                        reservation_id=res.reservation_id,
                    )
                if res.state in ("reserved", "dispatched"):
                    raise DispatchBlockedError(
                        "duplicate_inflight_dispatch",
                        f"Idempotency key {idempotency_key!r} is already inflight ({res.state})",
                        reservation_id=res.reservation_id,
                    )
                if res.state == "settled":
                    return res, False
                raise DispatchBlockedError(
                    f"prior_terminal_{res.state}",
                    f"Idempotency key {idempotency_key!r} terminated as {res.state}; use a new key for explicit retry",
                    reservation_id=res.reservation_id,
                )

            unlimited_active = self._is_unlimited_active_tx(conn)
            cap: int | None
            if project_id == "__shared__":
                cap = self._compute_shared_cap(conn, budget_bucket, cap_override)
            else:
                prow = conn.execute(
                    "SELECT profile_json, status, learning_enabled FROM project_profiles WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if prow is None or prow["status"] != "active":
                    raise DispatchBlockedError("not_registered", f"Project {project_id!r} is not registered/active")
                if not int(prow["learning_enabled"]):
                    raise DispatchBlockedError(
                        "learning_disabled",
                        f"Learning is disabled for project {project_id!r}; queued work preserved",
                    )
                profile = ProjectProfile.from_dict(json.loads(prow["profile_json"]))
                if budget_bucket not in ("daily_calls", "weekly_deep_calls", "weekly_carbon_calls", "daily_fetches"):
                    raise ValidationError(f"Unknown budget_bucket: {budget_bucket!r}")
                if unlimited_active and budget_bucket in _UNCAPPED_MODEL_BUCKETS:
                    cap = None
                elif cap_override is not None:
                    cap = cap_override
                elif budget_bucket == "daily_calls":
                    cap = min(profile.budget.daily_calls, PILOT_MAX_HYDROGEN_CALLS_PER_DAY)
                elif budget_bucket == "weekly_deep_calls":
                    cap = min(profile.budget.weekly_deep_calls, PILOT_MAX_DEEP_CANDIDATES_PER_WEEK)
                elif budget_bucket == "weekly_carbon_calls":
                    cap = PILOT_MAX_CARBON_CALLS_PER_WEEK
                else:
                    cap = profile.budget.daily_fetches

            self._check_trial_grant_tx(
                conn,
                scope_type=scope_type,
                units=units,
                now=now,
                require_trial_grant=require_trial_grant,
            )

            used_row = conn.execute(
                """
                SELECT COALESCE(SUM(
                    CASE
                        WHEN state = 'settled' THEN COALESCE(actual_units, units)
                        WHEN state IN ('reserved', 'dispatched', 'unknown') THEN units
                        ELSE 0
                    END
                ), 0) AS used
                FROM dispatch_reservations
                WHERE project_id = ? AND budget_bucket = ? AND window_key = ?
                """,
                (project_id, budget_bucket, window),
            ).fetchone()
            used = int(used_row["used"])
            if cap is not None and used + units > cap:
                raise DispatchBlockedError(
                    "budget_exhausted",
                    f"Budget exhausted for {project_id}/{budget_bucket} ({used}+{units}>{cap})",
                )

            res_id = _new_id("res")
            conn.execute(
                """
                INSERT INTO dispatch_reservations(
                    reservation_id, idempotency_key, project_id, scope_type, budget_bucket,
                    units, window_key, state, actual_units, receipt_json, error,
                    created_at, updated_at, lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', NULL, NULL, NULL, ?, ?, ?)
                """,
                (
                    res_id,
                    idempotency_key,
                    project_id,
                    scope_type,
                    budget_bucket,
                    units,
                    window,
                    now,
                    now,
                    now + ttl_s,
                ),
            )
            eff_pids = list(dict.fromkeys(str(p) for p in target_project_ids if str(p)))
            if not eff_pids and project_id != "__shared__":
                eff_pids = [project_id]
            eff_steps = list(dict.fromkeys(str(s) for s in step_keys if str(s)))
            conn.execute(
                """
                INSERT OR REPLACE INTO dispatch_scopes(reservation_id, target_project_ids_json, step_keys_json)
                VALUES (?, ?, ?)
                """,
                (res_id, json.dumps(eff_pids), json.dumps(eff_steps)),
            )
            created = conn.execute(
                "SELECT * FROM dispatch_reservations WHERE reservation_id = ?",
                (res_id,),
            ).fetchone()
            return self._reservation_from_row(created), True

    def mark_dispatched(self, reservation_id: str) -> DispatchReservation:
        now = self.now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state FROM dispatch_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise ValidationError(f"Reservation not found: {reservation_id!r}")
            if row["state"] != "reserved":
                raise ValidationError(f"Cannot mark_dispatched from state {row['state']!r}")
            conn.execute(
                "UPDATE dispatch_reservations SET state = 'dispatched', updated_at = ? WHERE reservation_id = ?",
                (now, reservation_id),
            )
            updated = conn.execute(
                "SELECT * FROM dispatch_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            return self._reservation_from_row(updated)

    def settle_dispatch(
        self,
        reservation_id: str,
        *,
        actual_units: int,
        receipt: dict[str, Any] | None = None,
    ) -> DispatchReservation:
        if actual_units < 0:
            raise ValidationError("actual_units cannot be negative")
        now = self.now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state FROM dispatch_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise ValidationError(f"Reservation not found: {reservation_id!r}")
            if row["state"] not in ("reserved", "dispatched"):
                raise ValidationError(f"Cannot settle from state {row['state']!r}")
            conn.execute(
                """
                UPDATE dispatch_reservations
                SET state = 'settled', actual_units = ?, receipt_json = ?, updated_at = ?
                WHERE reservation_id = ?
                """,
                (
                    actual_units,
                    json.dumps(receipt, sort_keys=True) if receipt is not None else None,
                    now,
                    reservation_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM dispatch_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            return self._reservation_from_row(updated)

    def release_dispatch(self, reservation_id: str, *, reason: str = "") -> DispatchReservation:
        """Release an undispatched reservation (e.g., local validation failure before external call)."""
        now = self.now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state FROM dispatch_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise ValidationError(f"Reservation not found: {reservation_id!r}")
            if row["state"] != "reserved":
                raise ValidationError(f"Only undispatched 'reserved' rows can be released, got {row['state']!r}")
            conn.execute(
                """
                UPDATE dispatch_reservations
                SET state = 'released', actual_units = 0, error = ?, updated_at = ?
                WHERE reservation_id = ?
                """,
                (reason or "released_before_dispatch", now, reservation_id),
            )
            updated = conn.execute(
                "SELECT * FROM dispatch_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            return self._reservation_from_row(updated)

    def mark_unknown(
        self,
        reservation_id: str,
        *,
        error: str = "timeout_or_unknown_completion",
        receipt: dict[str, Any] | None = None,
    ) -> DispatchReservation:
        """Mark an inflight dispatch as unknown (requiring explicit reconciliation, never blind retry)."""
        now = self.now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state FROM dispatch_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise ValidationError(f"Reservation not found: {reservation_id!r}")
            if row["state"] not in ("reserved", "dispatched"):
                raise ValidationError(f"Cannot mark_unknown from state {row['state']!r}")
            conn.execute(
                """
                UPDATE dispatch_reservations
                SET state = 'unknown', error = ?, receipt_json = ?, updated_at = ?
                WHERE reservation_id = ?
                """,
                (
                    error,
                    json.dumps(receipt, sort_keys=True) if receipt is not None else None,
                    now,
                    reservation_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM dispatch_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            return self._reservation_from_row(updated)

    def recover_stale_dispatches(self, *, force_all_inflight: bool = False) -> list[DispatchReservation]:
        """Transition interrupted reserved/dispatched rows to 'unknown' requiring explicit reconciliation."""
        now = self.now()
        with self._tx() as conn:
            if force_all_inflight:
                rows = conn.execute(
                    "SELECT reservation_id FROM dispatch_reservations WHERE state IN ('reserved', 'dispatched')"
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT reservation_id FROM dispatch_reservations
                    WHERE state IN ('reserved', 'dispatched') AND lease_expires_at <= ?
                    """,
                    (now,),
                ).fetchall()
            recovered: list[DispatchReservation] = []
            for r in rows:
                conn.execute(
                    """
                    UPDATE dispatch_reservations
                    SET state = 'unknown', error = 'crash_or_timeout_before_receipt', updated_at = ?
                    WHERE reservation_id = ?
                    """,
                    (now, r["reservation_id"]),
                )
                updated = conn.execute(
                    "SELECT * FROM dispatch_reservations WHERE reservation_id = ?",
                    (r["reservation_id"],),
                ).fetchone()
                recovered.append(self._reservation_from_row(updated))
            return recovered

    def reconcile_dispatch(
        self,
        reservation_id: str,
        *,
        outcome: str,
        actual_units: int = 0,
        receipt: dict[str, Any] | None = None,
        note: str = "",
    ) -> DispatchReservation:
        """Explicitly reconcile an 'unknown' reservation to 'settled' or 'failed'."""
        if outcome not in ("settled", "failed"):
            raise ValidationError(f"Invalid reconcile outcome: {outcome!r}")
        if actual_units < 0:
            raise ValidationError("actual_units cannot be negative")
        now = self.now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state FROM dispatch_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise ValidationError(f"Reservation not found: {reservation_id!r}")
            if row["state"] != "unknown":
                raise ValidationError(f"Only 'unknown' dispatches can be reconciled, got {row['state']!r}")
            conn.execute(
                """
                UPDATE dispatch_reservations
                SET state = ?, actual_units = ?, receipt_json = ?, error = ?, updated_at = ?
                WHERE reservation_id = ?
                """,
                (
                    outcome,
                    actual_units,
                    json.dumps(receipt, sort_keys=True) if receipt is not None else None,
                    note or (None if outcome == "settled" else "reconciled_failed"),
                    now,
                    reservation_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM dispatch_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            return self._reservation_from_row(updated)

    def find_unresolved_dispatch(
        self,
        *,
        scope_types: Sequence[str] = (),
        step_keys: Sequence[str] = (),
        project_ids: Sequence[str] = (),
    ) -> DispatchReservation | None:
        """Return any unresolved ('unknown', 'reserved', 'dispatched') dispatch matching scope/step/project."""
        wanted_scopes = set(scope_types)
        wanted_steps = set(step_keys)
        wanted_pids = set(project_ids)
        with self._lock:
            rows = self._conn().execute(
                """
                SELECT r.*, s.target_project_ids_json, s.step_keys_json
                FROM dispatch_reservations r
                LEFT JOIN dispatch_scopes s ON s.reservation_id = r.reservation_id
                WHERE r.state IN ('unknown', 'reserved', 'dispatched')
                ORDER BY
                    CASE WHEN r.state = 'unknown' THEN 0 ELSE 1 END,
                    r.created_at ASC,
                    r.reservation_id ASC
                """
            ).fetchall()
            for row in rows:
                if wanted_scopes and row["scope_type"] not in wanted_scopes:
                    continue
                row_steps = set(json.loads(row["step_keys_json"])) if row["step_keys_json"] else set()
                row_pids = set(json.loads(row["target_project_ids_json"])) if row["target_project_ids_json"] else set()
                if not row_pids and row["project_id"] != "__shared__":
                    row_pids = {row["project_id"]}
                if wanted_steps and row_steps and (wanted_steps & row_steps):
                    return self._reservation_from_row(row)
                if wanted_pids and (not row_pids or (wanted_pids & row_pids)):
                    return self._reservation_from_row(row)
                if not wanted_steps and not wanted_pids:
                    return self._reservation_from_row(row)
            return None

    def list_dispatches(
        self,
        *,
        project_id: str | None = None,
        state: str | None = None,
        include_shared: bool = False,
    ) -> list[DispatchReservation]:
        with self._lock:
            clauses: list[str] = []
            params: list[Any] = []
            if project_id is not None and not include_shared:
                clauses.append("r.project_id = ?")
                params.append(project_id)
            if state is not None:
                clauses.append("r.state = ?")
                params.append(state)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = self._conn().execute(
                f"""
                SELECT r.*, s.target_project_ids_json
                FROM dispatch_reservations r
                LEFT JOIN dispatch_scopes s ON s.reservation_id = r.reservation_id
                {where}
                ORDER BY r.created_at ASC, r.reservation_id ASC
                """,
                params,
            ).fetchall()
            out: list[DispatchReservation] = []
            for r in rows:
                if project_id is not None and include_shared:
                    if r["project_id"] != project_id:
                        if r["project_id"] != "__shared__":
                            continue
                        pids = json.loads(r["target_project_ids_json"]) if r["target_project_ids_json"] else []
                        if pids and project_id not in pids:
                            continue
                out.append(self._reservation_from_row(r))
            return out

    def budget_used(self, project_id: str, budget_bucket: str, *, at_ts: float | None = None) -> int:
        window = self.window_key(budget_bucket, at_ts)
        with self._lock:
            row = self._conn().execute(
                """
                SELECT COALESCE(SUM(
                    CASE
                        WHEN state = 'settled' THEN COALESCE(actual_units, units)
                        WHEN state IN ('reserved', 'dispatched', 'unknown') THEN units
                        ELSE 0
                    END
                ), 0) AS used
                FROM dispatch_reservations
                WHERE project_id = ? AND budget_bucket = ? AND window_key = ?
                """,
                (project_id, budget_bucket, window),
            ).fetchone()
            return int(row["used"])

    @staticmethod
    def _reservation_from_row(row: sqlite3.Row) -> DispatchReservation:
        receipt_raw = row["receipt_json"]
        return DispatchReservation(
            reservation_id=row["reservation_id"],
            idempotency_key=row["idempotency_key"],
            project_id=row["project_id"],
            scope_type=row["scope_type"],
            budget_bucket=row["budget_bucket"],
            units=int(row["units"]),
            window_key=row["window_key"],
            state=row["state"],
            actual_units=row["actual_units"],
            receipt=json.loads(receipt_raw) if receipt_raw else None,
            error=row["error"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            lease_expires_at=float(row["lease_expires_at"]),
        )
