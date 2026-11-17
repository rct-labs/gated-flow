"""Orchestration for explicit profile drafts and bounded two-mode intake."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Sequence
import uuid

from learner.contracts import (
    DispatchBlockedError,
    IngestionCoverage,
    LearnerError,
    ProjectProfile,
    ProjectProfileDraft,
    SourceArtifact,
    ValidationError,
    sha256_json,
)
from learner.privacy import derive_sanitized_concepts
from learner.sources import (
    CollectionResult,
    Transport,
    collect_opportunity_led,
    collect_project_led,
)
from learner.store import LearnerStore

_DEFAULT_DOC_CANDIDATES = ("README.md", "CONTEXT.md", "SPEC.md", "docs/autonomy.md")
_WORD_RE = re.compile(r"[a-z0-9]{4,}")


def _extract_bullets(text: str, heading_keywords: tuple[str, ...], limit: int = 5) -> tuple[str, ...]:
    lines = text.splitlines()
    in_section = False
    found: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            low = stripped.lstrip("#").strip().lower()
            in_section = any(k in low for k in heading_keywords)
            continue
        if in_section and stripped.startswith(("-", "*")):
            item = stripped.lstrip("-* ").strip()
            if item and item not in found:
                found.append(item[:200])
                if len(found) >= limit:
                    break
    return tuple(found)


def draft_profile_from_docs(
    project_id: str,
    root_path: str | Path,
    *,
    name: str | None = None,
    adapter: str = "standalone",
    mission_override: str | None = None,
    doc_paths: Sequence[str] | None = None,
    max_files: int = 5,
    max_bytes_per_file: int = 16_384,
) -> ProjectProfileDraft:
    """Derive an UNREGISTERED profile draft from explicit bounded docs without activating anything."""
    root = Path(root_path).resolve()
    if not root.is_dir():
        raise ValidationError(f"root_path is not an existing directory: {root_path}")

    candidates = list(doc_paths) if doc_paths is not None else list(_DEFAULT_DOC_CANDIDATES)
    read_docs: list[str] = []
    doc_texts: list[str] = []
    warnings: list[str] = []

    for rel in candidates[:max_files]:
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise ValidationError(f"Doc path escapes root_path: {rel!r}")
        if not target.is_file():
            continue
        raw = target.read_bytes()[:max_bytes_per_file]
        text = raw.decode("utf-8", errors="replace")
        read_docs.append(str(target.relative_to(root)))
        doc_texts.append(text)

    combined = "\n".join(doc_texts)
    first_para = ""
    for line in combined.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("<!--"):
            first_para = s[:300]
            break

    project_name = (name or root.name or project_id).strip()
    mission = (mission_override or first_para or f"Draft learning mission for {project_name}").strip()
    goals = _extract_bullets(combined, ("goal", "objective", "acceptance", "roadmap"), limit=5)
    capabilities = _extract_bullets(combined, ("capabilit", "feature", "existing", "implemented"), limit=6)

    concept_lines = [line for line in combined.splitlines() if line.strip() and not line.strip().startswith("<!--")][:30]
    forbidden = (project_id, project_name, root.name)
    public_concepts = derive_sanitized_concepts(concept_lines, forbidden_terms=forbidden, max_concepts=5)
    if not public_concepts:
        warnings.append("No generic public_concepts derived automatically; provide sanitized concepts before public project-led queries")
    if not read_docs:
        warnings.append("No bounded maintained docs found; review mission and goals before registration")

    profile = ProjectProfile(
        project_id=project_id,
        name=project_name,
        root_path=str(root),
        adapter=adapter,
        mission=mission,
        stage="draft",
        existing_capabilities=capabilities,
        goals=goals,
        known_gaps=(),
        desired_outcomes=(),
        exclusions=(),
        public_concepts=public_concepts,
        private_terms=(),
        learning_enabled=True,
        execution_paused=False,
        provenance={
            "origin": "draft_profile_from_docs",
            "source_docs": read_docs,
        },
        revision=1,
        status="draft",
    )
    return ProjectProfileDraft(
        profile=profile,
        activated=False,
        source_docs=tuple(read_docs),
        warnings=tuple(warnings),
    )


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def summarize_intake_fit(profile: ProjectProfile, artifact: SourceArtifact) -> dict[str, Any]:
    """Compute deterministic intake-level lexical cues (never model inference or qualification)."""
    art_text = f"{artifact.title} {artifact.summary}".lower()
    art_tokens = _tokens(art_text)

    exclusions_hit = [ex for ex in profile.exclusions if ex.lower() in art_text]
    capability_overlap = [
        cap for cap in profile.existing_capabilities
        if len(_tokens(cap) & art_tokens) >= 2 or cap.lower() in art_text
    ]

    matched_goal: str | None = None
    best_overlap = 0
    for goal in profile.goals:
        overlap = len(_tokens(goal) & art_tokens)
        if overlap > best_overlap:
            best_overlap = overlap
            matched_goal = goal
    if best_overlap < 1:
        matched_goal = None

    concept_hits = [
        concept for concept in profile.public_concepts
        if _tokens(concept) & art_tokens
    ]
    gap_hits = [
        gap for gap in profile.known_gaps
        if _tokens(gap) & art_tokens
    ]

    if exclusions_hit:
        fit = "excluded_by_profile"
    elif capability_overlap and not gap_hits:
        fit = "existing_capability_overlap"
    elif concept_hits or matched_goal or gap_hits:
        fit = "adjacent_concept_match"
    elif artifact.popularity_signal_only:
        fit = "unmatched_popularity_only"
    else:
        fit = "unmatched_intake"

    return {
        "fit": fit,
        "exclusions_hit": exclusions_hit,
        "capability_overlap": capability_overlap,
        "goal_ref": matched_goal,
        "concept_hits": concept_hits,
        "gap_hits": gap_hits,
        "popularity_signal_only": artifact.popularity_signal_only,
    }


def _load_arxiv_cache(store: LearnerStore) -> dict[str, dict[str, Any]]:
    raw_index = store.get_setting("arxiv_cache:index", {})
    if not isinstance(raw_index, dict):
        return {}
    hydrated: dict[str, dict[str, Any]] = {}
    for qk, meta in raw_index.items():
        if not isinstance(meta, dict):
            continue
        entry: dict[str, Any] = {
            "cached_at": meta.get("cached_at"),
            "retry_after_until": meta.get("retry_after_until"),
        }
        digest = meta.get("digest")
        if isinstance(digest, str) and digest:
            try:
                entry["raw_xml"] = store.get_evidence(digest)
            except LearnerError:
                pass
        hydrated[str(qk)] = entry
    return hydrated


def _save_arxiv_cache_updates(store: LearnerStore, updates: dict[str, Any]) -> None:
    if not updates:
        return
    raw_index = store.get_setting("arxiv_cache:index", {})
    index: dict[str, dict[str, Any]] = dict(raw_index) if isinstance(raw_index, dict) else {}
    for qk, upd in updates.items():
        if not isinstance(upd, dict):
            continue
        cur = dict(index.get(qk) or {})
        raw_xml = upd.get("raw_xml")
        if isinstance(raw_xml, (bytes, bytearray)) and raw_xml:
            digest = store.put_evidence(bytes(raw_xml), visibility="public", content_type="application/atom+xml")
            cur["digest"] = digest
            cur["cached_at"] = upd.get("cached_at")
            cur["retry_after_until"] = upd.get("retry_after_until")
        elif "retry_after_until" in upd:
            cur["retry_after_until"] = upd["retry_after_until"]
        index[str(qk)] = cur
    if len(index) > 10:
        ordered = sorted(
            index.items(),
            key=lambda kv: float((kv[1] or {}).get("cached_at") or 0.0),
            reverse=True,
        )[:10]
        index = dict(ordered)
    store.set_setting("arxiv_cache:index", index)


def run_intake(
    store: LearnerStore,
    mode: str,
    transport: Transport,
    *,
    project_id: str | None = None,
    release_repos: Sequence[str] = (),
    arxiv_query: str = "cat:cs.SE OR cat:cs.AI",
    allow_search_fallback: bool = True,
    extra_concepts: Sequence[str] = (),
    daily_fetches_cap: int | None = None,
    daily_metadata_cap: int | None = None,
    lease_owner: str = "intake-runner",
    ttl_s: float = 300.0,
    run_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Run bounded project-led or opportunity-led intake without executing model inference."""
    if mode not in ("project-led", "opportunity-led"):
        raise ValidationError(f"Unsupported intake mode: {mode!r}")

    if project_id is not None:
        profile = store.get_profile(project_id)
        if profile is None:
            return {
                "status": "blocked",
                "reason": "not_registered",
                "mode": mode,
                "fetched": 0,
                "assessments_created": 0,
                "assessment_status": "unconfigured",
            }
        if not profile.learning_enabled:
            return {
                "status": "blocked",
                "reason": "learning_disabled",
                "mode": mode,
                "project_id": project_id,
                "fetched": 0,
                "queued_preserved": len(store.list_assessments(project_id)),
                "assessment_status": "unconfigured",
            }
        target_profiles = [profile]
    else:
        if mode == "project-led":
            raise ValidationError("project-led intake requires project_id")
        target_profiles = [p for p in store.list_profiles() if p.learning_enabled]
        if not target_profiles:
            return {
                "status": "noop",
                "reason": "no_active_profiles",
                "mode": mode,
                "fetched": 0,
                "assessments_created": 0,
                "assessment_status": "unconfigured",
            }

    effective_run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
    lock_name = f"intake:{mode}:{project_id or 'shared'}"
    store.acquire_lease(lock_name, lease_owner, ttl_s=ttl_s)
    try:
        store.recover_stale_dispatches()
        fetch_step_key = f"shared-fetch:{mode}:{project_id or '__shared__'}"
        target_pids = [p.project_id for p in target_profiles]
        conflict = store.find_unresolved_dispatch(
            scope_types=("shared_fetch",),
            step_keys=(fetch_step_key,),
            project_ids=target_pids,
        )
        if conflict is not None:
            reason = (
                "unknown_pending_reconciliation"
                if conflict.state == "unknown"
                else "duplicate_inflight_dispatch"
            )
            return {
                "status": "blocked",
                "reason": reason,
                "mode": mode,
                "reservation_id": conflict.reservation_id,
                "fetched": 0,
                "queued_preserved": sum(len(store.list_assessments(p.project_id)) for p in target_profiles),
                "assessment_status": reason if conflict.state == "unknown" else "unconfigured",
            }

        if idempotency_key:
            idem_key = idempotency_key
        else:
            existing_keys = {
                d.idempotency_key for d in store.list_dispatches() if d.scope_type == "shared_fetch"
            }
            attempt = 1
            while f"{fetch_step_key}:a{attempt}" in existing_keys:
                attempt += 1
            idem_key = f"{fetch_step_key}:a{attempt}"
        remaining_meta = store.remaining_shared_budget("daily_metadata_items", cap_override=daily_metadata_cap)
        if remaining_meta <= 0:
            return {
                "status": "blocked",
                "reason": "metadata_budget_exhausted",
                "mode": mode,
                "fetched": 0,
                "queued_preserved": sum(len(store.list_assessments(p.project_id)) for p in target_profiles),
                "assessment_status": "unconfigured",
            }

        try:
            reservation, should_dispatch = store.reserve_dispatch(
                idempotency_key=idem_key,
                project_id="__shared__",
                scope_type="shared_fetch",
                budget_bucket="daily_fetches",
                units=1,
                ttl_s=ttl_s,
                cap_override=daily_fetches_cap,
                target_project_ids=target_pids,
                step_keys=(fetch_step_key,),
            )
        except DispatchBlockedError as exc:
            return {
                "status": "blocked",
                "reason": exc.reason,
                "mode": mode,
                "error": str(exc),
                "fetched": 0,
                "queued_preserved": sum(len(store.list_assessments(p.project_id)) for p in target_profiles),
                "assessment_status": "unconfigured",
            }

        if not should_dispatch:
            return {
                "status": "deduplicated",
                "reason": "already_settled",
                "mode": mode,
                "reservation_id": reservation.reservation_id,
                "fetched": 0,
                "assessment_status": "unconfigured",
            }

        store.mark_dispatched(reservation.reservation_id)
        arxiv_cache = _load_arxiv_cache(store)
        if mode == "project-led":
            collect_profile = target_profiles[0]
            if extra_concepts:
                merged_concepts = tuple(dict.fromkeys([*collect_profile.public_concepts, *extra_concepts]))[:5]
                collect_profile = ProjectProfile.from_dict(
                    collect_profile.to_dict() | {"public_concepts": merged_concepts}
                )
            collection: CollectionResult = collect_project_led(
                collect_profile,
                transport,
                clock=store.clock,
                release_repos=release_repos,
                run_id=effective_run_id,
                arxiv_cache=arxiv_cache,
            )
        else:
            collection = collect_opportunity_led(
                transport,
                profiles=target_profiles,
                clock=store.clock,
                max_items_per_sweep=remaining_meta,
                release_repos=release_repos,
                arxiv_query=arxiv_query,
                allow_search_fallback=allow_search_fallback,
                run_id=effective_run_id,
                arxiv_cache=arxiv_cache,
            )

        if isinstance(collection.metadata, dict) and collection.metadata.get("arxiv_cache_updates"):
            _save_arxiv_cache_updates(store, collection.metadata["arxiv_cache_updates"])

        bounded_artifacts = list(collection.artifacts[:remaining_meta])
        metadata_capped = len(collection.artifacts) > remaining_meta
        if bounded_artifacts:
            meta_res, meta_should = store.reserve_dispatch(
                idempotency_key=f"meta:{idem_key}",
                project_id="__shared__",
                scope_type="metadata_items",
                budget_bucket="daily_metadata_items",
                units=len(bounded_artifacts),
                ttl_s=ttl_s,
                cap_override=daily_metadata_cap,
                target_project_ids=target_pids,
                step_keys=(f"meta:{fetch_step_key}",),
            )
            if meta_should:
                store.mark_dispatched(meta_res.reservation_id)
                store.settle_dispatch(
                    meta_res.reservation_id,
                    actual_units=len(bounded_artifacts),
                    receipt={"run_id": effective_run_id, "mode": mode, "items": len(bounded_artifacts)},
                )

        store.settle_dispatch(
            reservation.reservation_id,
            actual_units=1,
            receipt={
                "run_id": effective_run_id,
                "mode": mode,
                "items_found": len(bounded_artifacts),
                "metadata_capped": metadata_capped,
                "incomplete": metadata_capped or any(c.incomplete for c in collection.coverage),
            },
        )

        new_by_class: dict[str, int] = {}
        stored_artifacts: list[SourceArtifact] = []
        for art in bounded_artifacts:
            raw_bytes = collection.raw_evidence.get(art.source_id, b"")
            if raw_bytes:
                store.put_evidence(raw_bytes, visibility="public", content_type="application/json")
            saved_art, is_new = store.record_source_artifact(art)
            stored_artifacts.append(saved_art)
            if is_new:
                new_by_class[saved_art.source_class] = new_by_class.get(saved_art.source_class, 0) + 1

        recorded_cov: list[IngestionCoverage] = []
        for cov in collection.coverage:
            updated_cov = IngestionCoverage(
                run_id=cov.run_id,
                source_class=cov.source_class,
                query_key=cov.query_key,
                mode=cov.mode,
                status=cov.status,
                incomplete=cov.incomplete,
                items_found=cov.items_found,
                items_new=new_by_class.get(cov.source_class, 0) if cov.items_found else 0,
                error=cov.error,
                last_success_at=cov.last_success_at,
                recorded_at=cov.recorded_at,
            )
            recorded_cov.append(store.record_coverage(updated_cov))

        created = 0
        deduped = 0
        for p in target_profiles:
            allowed_classes = set(p.source_policy.allowed_classes)
            for art in stored_artifacts:
                if art.source_class not in allowed_classes:
                    continue
                fit = summarize_intake_fit(p, art)
                reason = f"intake:{fit['fit']}"
                _, was_created = store.ensure_assessment_for_artifact(
                    p,
                    art,
                    goal_ref=fit["goal_ref"],
                    capability_ref=fit["capability_overlap"][0] if fit["capability_overlap"] else None,
                    capability_overlap=fit["capability_overlap"],
                    reason=reason,
                )
                if was_created:
                    created += 1
                else:
                    deduped += 1

        return {
            "status": "degraded" if any(c.incomplete for c in recorded_cov) else "ok",
            "run_id": effective_run_id,
            "mode": mode,
            "reservation_id": reservation.reservation_id,
            "artifacts_found": len(stored_artifacts),
            "artifacts_new": sum(new_by_class.values()),
            "assessments_created": created,
            "assessments_deduplicated": deduped,
            "incomplete": any(c.incomplete for c in recorded_cov),
            "coverage": [c.to_dict() for c in recorded_cov],
            "assessment_status": "unconfigured",
            "assessment_note": "No model assessment route configured in foundation; items queued in seen state.",
        }
    finally:
        store.release_lease(lock_name, lease_owner)
