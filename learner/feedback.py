"""Bounded local owner attestations. Outcome references never authorize execution."""
import json
import os
import stat
from pathlib import Path

from learner.contracts import ValidationError

MAX_OUTCOME_BYTES = 1_048_576
MAX_OUTCOME_RECORDS = 1000
_FIELDS = ("project_id", "assessment_id", "external_experiment_ref", "status",
           "baseline_summary", "outcome_summary", "evaluator")


def consume_project_outcomes(store, profile):
    if not profile.learning_enabled:
        return 0
    root = Path(profile.root_path).resolve()
    path = (root / ".flow" / "verified-outcomes.jsonl").resolve()
    if not path.is_relative_to(root):
        raise ValidationError("Outcome bridge must stay inside project")
    parts = path.relative_to(root).parts
    if not parts:
        raise ValidationError("Outcome bridge must be a regular file")
    try:
        path.stat()
    except FileNotFoundError:
        return 0
    if (not all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
            or os.open not in getattr(os, "supports_dir_fd", ())):
        raise ValidationError("Secure outcome file access is unsupported on this platform")
    # Resolve allowed in-project symlinks, then pin each directory while opening.
    # O_NOFOLLOW rejects a symlink substituted after resolution.
    try:
        dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return 0
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except FileNotFoundError:
                return 0
            os.close(dir_fd)
            dir_fd = next_fd
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=dir_fd)
        except FileNotFoundError:
            return 0
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValidationError("Outcome bridge must be a regular file")
            if info.st_size > MAX_OUTCOME_BYTES:
                raise ValidationError("Outcome file exceeds byte limit")
            raw = stream.read(MAX_OUTCOME_BYTES + 1)
    finally:
        os.close(dir_fd)
    if len(raw) > MAX_OUTCOME_BYTES:
        raise ValidationError("Outcome file exceeds byte limit")
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) > MAX_OUTCOME_RECORDS:
        raise ValidationError("Outcome file exceeds record limit")
    records = [json.loads(line) for line in lines]
    # Preflight the complete batch under the same transaction as its writes.
    with store._tx():
        existing = {}
        for outcome in store.list_outcomes(profile.project_id):
            existing.setdefault(outcome.external_experiment_ref, []).append(outcome.to_dict())
        pending = {}
        for row in records:
            if not isinstance(row, dict) or row.get("project_id") != profile.project_id or row.get("verified") is not True:
                raise ValidationError("Outcome must be owner-attested and project-scoped")
            for key in _FIELDS:
                if not isinstance(row.get(key), str) or not row[key].strip():
                    raise ValidationError(f"Outcome requires {key}")
            asmt = store.get_assessment(row["assessment_id"])
            if not asmt or asmt.project_id != profile.project_id or row["status"] not in ("accepted", "rejected", "inconclusive"):
                raise ValidationError("Invalid outcome assessment or status")
            ref = row["external_experiment_ref"]
            prior = existing.get(ref, []) + ([pending[ref]] if ref in pending else [])
            if any(old[k] != row[k] for old in prior for k in _FIELDS):
                raise ValidationError("Conflicting experiment reference")
            if not prior:
                pending[ref] = row
        for row in pending.values():
            store.record_outcome_reference(**{k: row[k] for k in _FIELDS}, owner_attested=True)
    return len(pending)


def eligible_outcomes(store, project_id):
    """Only project-bound owner attestations of citation-valid proposals affect policy."""
    assessments = {}
    for out in store.list_outcomes(project_id):
        if out.assessment_id not in assessments:
            assessments[out.assessment_id] = store.get_assessment(out.assessment_id)
        a = assessments[out.assessment_id]
        if (not a or a.project_id != project_id or out.project_id != project_id
                or not out.baseline_summary.strip() or not out.outcome_summary.strip()
                or out.baseline_summary == "[redacted]" or out.outcome_summary == "[redacted]"):
            continue
        if not any(o.get("outcome_id") == out.outcome_id and o.get("project_id") == project_id
                   and o.get("verification_provenance") == "owner_attestation"
                   for o in a.verified_observations):
            continue
        if not any(i.get("step") == "verified_proposal" and i.get("citations_valid") for i in a.model_inferences):
            continue
        yield out, a


def project_scores(store, project_id):
    """Two distinct experiments permit a bounded [-1, 1] adjustment per goal/capability."""
    groups = {}
    for out, a in eligible_outcomes(store, project_id):
        if a.goal_ref and out.status in ("accepted", "rejected"):
            groups.setdefault((a.goal_ref, a.capability_ref), {})[out.external_experiment_ref] = 1 if out.status == "accepted" else -1
    return {key: sum(votes.values()) / len(votes) for key, votes in groups.items() if len(votes) >= 2}


def track_record(store, project_id, goal, capability):
    return project_scores(store, project_id).get((goal, capability), 0.0)
