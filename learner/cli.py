"""Standalone CLI for the independent two-mode learner foundation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from learner.assessment import (
    apply_feedback_preference,
    format_user_recommendations,
    get_language_preferences,
    get_recommendations,
    run_due,
    run_review,
    set_language_preferences,
    summarize_assessment_queue,
)
from learner.contracts import (
    LearnerError,
    OUTCOME_STATUSES,
    ProjectProfile,
    RETENTION_POLICIES,
)
from learner.model_dispatch import AgyCliRunner, FixtureModelRunner
from learner.pipeline import draft_profile_from_docs, run_intake
from learner.sources import FixtureTransport, HttpTransport, Transport, TransportResponse
from learner.store import LearnerStore


def _load_fixture_transport(fixture_map_path: str | Path) -> FixtureTransport:
    path = Path(fixture_map_path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise LearnerError("Fixture map JSON must be an object mapping URLs to responses")
    routes: dict[str, TransportResponse] = {}
    for url, spec in raw.items():
        if not isinstance(spec, dict):
            raise LearnerError(f"Fixture entry for {url!r} must be an object")
        if "body_file" in spec:
            body_bytes = (path.parent / spec["body_file"]).read_bytes()
        else:
            body_text = spec.get("body", "")
            if not isinstance(body_text, str):
                body_text = json.dumps(body_text)
            body_bytes = body_text.encode("utf-8")
        routes[url] = TransportResponse(
            status_code=int(spec.get("status_code", 200)),
            body=body_bytes,
            headers={str(k).lower(): str(v) for k, v in (spec.get("headers") or {}).items()},
            final_url=str(spec.get("final_url", url)),
        )
    return FixtureTransport(routes)


def _load_fixture_model_runner(
    model_fixture_path: str | Path | None,
    *,
    policy_path: str | Path | None = None,
) -> FixtureModelRunner:
    """Build an offline FixtureModelRunner from explicit fixture data or defer without inference."""
    if model_fixture_path is None:
        return FixtureModelRunner(
            available=False,
            unavailable_reason="offline_fixture_no_model_data",
            policy_path=policy_path,
        )
    raw = json.loads(Path(model_fixture_path).resolve().read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise LearnerError("Model fixture JSON must be an object")
    if raw.get("available") is False:
        return FixtureModelRunner(
            available=False,
            unavailable_reason=str(raw.get("reason") or "offline_fixture_model_unavailable"),
            policy_path=policy_path,
        )

    tasks = raw.get("tasks") if isinstance(raw.get("tasks"), dict) else raw

    def _handler(role: str, prompt: str) -> dict[str, Any]:
        req = json.loads(prompt)
        task = req.get("task")
        spec = tasks.get(task) if isinstance(tasks, dict) else None
        if isinstance(spec, dict) and role in spec and isinstance(spec[role], dict):
            spec = spec[role]
        if task == "batch_triage" and isinstance(spec, dict) and "decisions" not in spec:
            qualify_terms = [str(t).lower() for t in (spec.get("qualify_contains") or [])]
            existing_terms = [str(t).lower() for t in (spec.get("already_implemented_contains") or [])]
            alias = req["projects"][0]["project_alias"]
            decisions = []
            for cand in req.get("untrusted_source_data") or []:
                hay = f"{cand.get('title', '')} {cand.get('summary', '')}".lower()
                if any(t in hay for t in existing_terms):
                    decisions.append({
                        "source_id": cand["source_id"],
                        "project_alias": alias,
                        "decision": "reject",
                        "goal_ref": "",
                        "capability_ref": str(spec.get("capability_ref") or "existing"),
                        "rationale": "Matches existing project capability.",
                        "uncertainty": "Low.",
                        "already_implemented": True,
                        "reopen_triggers": ["profile_capabilities_changed"],
                    })
                elif any(t in hay for t in qualify_terms):
                    decisions.append({
                        "source_id": cand["source_id"],
                        "project_alias": alias,
                        "decision": "qualify",
                        "goal_ref": str(spec.get("goal_ref") or "improve project workflow"),
                        "capability_ref": str(spec.get("capability_ref") or "adjacent capability"),
                        "rationale": str(spec.get("qualify_rationale") or "Adjacent fixture capability matches project goal."),
                        "uncertainty": str(spec.get("uncertainty") or "Requires primary evidence check."),
                        "already_implemented": False,
                        "reopen_triggers": ["new_source_version", "profile_goal_changed"],
                    })
                else:
                    decisions.append({
                        "source_id": cand["source_id"],
                        "project_alias": alias,
                        "decision": "reject",
                        "goal_ref": "",
                        "capability_ref": "",
                        "rationale": "Unrelated fixture item.",
                        "uncertainty": "Low.",
                        "already_implemented": False,
                        "reopen_triggers": ["profile_goal_changed"],
                    })
            return {"decisions": decisions}
        if isinstance(spec, dict) and task in ("independent_blind_assessment", "bounded_proposal_synthesis"):
            out = dict(spec)
            if not out.get("citations"):
                ev_list = (
                    req.get("retrieved_evidence")
                    if task == "bounded_proposal_synthesis"
                    else (req.get("untrusted_source_data") or {}).get("retrieved_evidence")
                ) or []
                if ev_list:
                    ev = next((e for e in ev_list if e.get("kind") != "github_repo_metadata"), ev_list[0])
                    out["citations"] = [{
                        "evidence_id": ev["evidence_id"],
                        "url": ev["url"],
                        "digest": ev["digest"],
                        "claim": "Fixture primary evidence citation.",
                    }]
            return out
        if isinstance(spec, dict):
            return spec
        raise LearnerError(f"Model fixture missing entry for task {task!r}")

    return FixtureModelRunner(handler=_handler, policy_path=policy_path)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--storage-root", default=argparse.SUPPRESS, help="Explicit SQLite/evidence storage root")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit JSON output")

    parser = argparse.ArgumentParser(prog="project-learner", description="Standalone directed learner CLI")
    parser.add_argument("--storage-root", default=".learner-data", help="Explicit SQLite/evidence storage root")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    p_draft = sub.add_parser("draft", parents=[common], help="Build an unregistered profile draft from bounded docs")
    p_draft.add_argument("--project-id", required=True)
    p_draft.add_argument("--root-path", required=True)
    p_draft.add_argument("--name")
    p_draft.add_argument("--adapter", default="standalone")
    p_draft.add_argument("--mission")
    p_draft.add_argument("--out")

    p_reg = sub.add_parser("register", parents=[common], help="Explicitly register and activate a project profile")
    p_reg.add_argument("--profile", help="Path to ProjectProfile JSON file")
    p_reg.add_argument("--project-id")
    p_reg.add_argument("--name")
    p_reg.add_argument("--root-path")
    p_reg.add_argument("--adapter", default="standalone")
    p_reg.add_argument("--mission")
    p_reg.add_argument("--public-concepts", help="Comma-separated generic public concepts")
    p_reg.add_argument("--goals", help="Comma-separated goals")
    p_reg.add_argument("--capabilities", help="Comma-separated existing capabilities")

    p_rev = sub.add_parser("revise", parents=[common], help="Revise an active project profile")
    p_rev.add_argument("--project-id", required=True)
    p_rev.add_argument("--updates", help="Path to JSON updates object")
    p_rev.add_argument("--goals", help="Comma-separated replacement goals")
    p_rev.add_argument("--capabilities", help="Comma-separated replacement capabilities")
    p_rev.add_argument("--public-concepts", help="Comma-separated replacement public concepts")
    p_rev.add_argument("--note", default="")

    for action in ("pause", "resume"):
        p_act = sub.add_parser(action, parents=[common], help=f"{action.capitalize()} learning or execution")
        p_act.add_argument("--project-id", required=True)
        p_act.add_argument("--learning", action="store_true", help="Target learning_enabled")
        p_act.add_argument("--execution", action="store_true", help="Target execution_paused")

    p_unreg = sub.add_parser("unregister", parents=[common], help="Unregister profile and apply private retention")
    p_unreg.add_argument("--project-id", required=True)
    p_unreg.add_argument("--retention", choices=RETENTION_POLICIES)

    p_intake = sub.add_parser("intake", parents=[common], help="Run bounded project-led or opportunity-led intake")
    p_intake.add_argument("--mode", choices=("project-led", "opportunity-led"), required=True)
    p_intake.add_argument("--project-id")
    p_intake.add_argument("--fixture-map", help="Path to offline fixture URL map JSON")
    p_intake.add_argument("--live", action="store_true", help="Allow live HTTPS fetches (disabled by default)")
    p_intake.add_argument("--repo", action="append", default=[], help="Public owner/repo for official releases")
    p_intake.add_argument("--arxiv-query", default="cat:cs.SE OR cat:cs.AI")

    p_status = sub.add_parser("status", parents=[common], help="Show profiles, coverage, queued assessments, and outcomes")
    p_status.add_argument("--project-id")

    p_fb = sub.add_parser("feedback", parents=[common], help="Record an external project outcome reference")
    p_fb.add_argument("--project-id", required=True)
    p_fb.add_argument("--assessment-id", required=True)
    p_fb.add_argument("--experiment-ref", required=True)
    p_fb.add_argument("--status", choices=OUTCOME_STATUSES, required=True)
    p_fb.add_argument("--baseline", required=True)
    p_fb.add_argument("--outcome", required=True)
    p_fb.add_argument("--evaluator", required=True)
    p_fb.add_argument("--release-ref")
    p_fb.add_argument("--note")

    p_rec = sub.add_parser("reconcile", parents=[common], help="Reconcile an unknown dispatch reservation")
    p_rec.add_argument("--reservation-id", required=True)
    p_rec.add_argument("--outcome", choices=("settled", "failed"), required=True)
    p_rec.add_argument("--actual-units", type=int, default=0)
    p_rec.add_argument("--note", default="")

    p_due = sub.add_parser("due", parents=[common], help="Run scheduled daily opportunity-led and weekly project-led learning")
    p_due.add_argument("--fixture-map", help="Path to offline fixture URL map JSON")
    p_due.add_argument("--model-fixture", help="Path to explicit offline fixture model responses JSON")
    p_due.add_argument("--live", action="store_true", help="Allow live HTTPS fetches (disabled by default)")
    p_due.add_argument("--repo", action="append", default=[], help="Public owner/repo for official releases")
    p_due.add_argument("--arxiv-query", default="cat:cs.SE OR cat:cs.AI")
    p_due.add_argument("--plan-queries", action="store_true", help="Use bounded Hydrogen query planning")
    p_due.add_argument("--refresh-deferred", action="store_true", help="Refresh deferred primary evidence and reassess changed candidates")
    p_due.add_argument("--policy-path", help="Override model policy.json path")
    p_due.add_argument("--cli-path", help="Override agy-flow/agy executable path")

    p_rev_run = sub.add_parser("review", parents=[common], help="Run on-demand learning review using the shared envelope")
    p_rev_run.add_argument("--project-id")
    p_rev_run.add_argument("--mode", choices=("project-led", "opportunity-led", "both"), default="project-led")
    p_rev_run.add_argument("--fixture-map", help="Path to offline fixture URL map JSON")
    p_rev_run.add_argument("--model-fixture", help="Path to explicit offline fixture model responses JSON")
    p_rev_run.add_argument("--live", action="store_true", help="Allow live HTTPS fetches (disabled by default)")
    p_rev_run.add_argument("--repo", action="append", default=[], help="Public owner/repo for official releases")
    p_rev_run.add_argument("--arxiv-query", default="cat:cs.SE OR cat:cs.AI")
    p_rev_run.add_argument("--plan-queries", action="store_true", help="Use bounded Hydrogen query planning")
    p_rev_run.add_argument("--refresh-deferred", action="store_true", help="Refresh deferred primary evidence and reassess changed candidates without feed sweeps")
    p_rev_run.add_argument("--policy-path", help="Override model policy.json path")
    p_rev_run.add_argument("--cli-path", help="Override agy-flow/agy executable path")

    p_res = sub.add_parser("results", parents=[common], help="Show top actionable recommendations (quiet by default when empty)")
    p_res.add_argument("--project-id")
    p_res.add_argument("--limit", type=int, default=3)
    p_res.add_argument("--coverage", action="store_true", help="Include coverage summary even when quiet")
    p_res.add_argument("--user-language")
    p_res.add_argument("--deliverable-language")
    p_res.add_argument("--input-language")

    p_pref = sub.add_parser("prefer", parents=[common], help="Update bounded matching preferences from natural-language feedback")
    p_pref.add_argument("--project-id", required=True)
    p_pref.add_argument("--note", required=True)

    p_lang = sub.add_parser("language", parents=[common], help="Configure user output language separate from deliverable language")
    p_lang.add_argument("--project-id")
    p_lang.add_argument("--user-language")
    p_lang.add_argument("--deliverable-language")

    p_trial = sub.add_parser("trial", parents=[common], help="Inspect or configure finite or uncapped local model authorization")
    p_trial.add_argument("--grant", action="store_true", help="Activate an explicit finite trial grant")
    p_trial.add_argument("--unlimited", action="store_true", help="Explicitly remove local model-call and daily/weekly model/deep-candidate quotas for this store")
    p_trial.add_argument("--revoke", action="store_true", help="Deactivate active trial or unlimited authorization")
    p_trial.add_argument("--max-calls", type=int, help="Cumulative model-call ceiling for a finite trial")
    p_trial.add_argument("--expires-at", help="ISO-8601 UTC/tz timestamp or epoch seconds for a finite trial")
    p_trial.add_argument("--grant-id", help="Explicit distinct trial grant identifier")
    p_trial.add_argument("--scope", default="flow_pilot", help="Trial scope label")
    p_trial.add_argument("--authority", help="Owner authorization note (required with --unlimited)")
    p_trial.add_argument("--granted-by", help="Authority actor label (defaults to 'owner' for --unlimited, 'host' for --grant)")

    return parser


def _emit(payload: dict[str, Any], *, as_json: bool, summary: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(summary)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "draft":
            draft = draft_profile_from_docs(
                args.project_id,
                args.root_path,
                name=args.name,
                adapter=args.adapter,
                mission_override=args.mission,
            )
            payload = draft.to_dict()
            if args.out:
                Path(args.out).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            _emit(
                payload,
                as_json=args.json,
                summary=f"draft {draft.profile.project_id}: activated={draft.activated} docs={len(draft.source_docs)}",
            )
            return 0

        store = LearnerStore(args.storage_root)
        try:
            if args.command == "register":
                if args.profile:
                    raw = json.loads(Path(args.profile).read_text(encoding="utf-8"))
                    prof_dict = raw["profile"] if "profile" in raw and "activated" in raw else raw
                    profile = ProjectProfile.from_dict(prof_dict | {"status": "active"})
                else:
                    if not (args.project_id and args.name and args.root_path and args.mission):
                        raise LearnerError("Provide --profile or --project-id/--name/--root-path/--mission")
                    profile = ProjectProfile(
                        project_id=args.project_id,
                        name=args.name,
                        root_path=args.root_path,
                        adapter=args.adapter,
                        mission=args.mission,
                        existing_capabilities=tuple(_split_csv(args.capabilities)),
                        goals=tuple(_split_csv(args.goals)),
                        public_concepts=tuple(_split_csv(args.public_concepts)),
                    )
                saved = store.register_profile(profile)
                _emit(
                    saved.to_dict(),
                    as_json=args.json,
                    summary=f"registered {saved.project_id} rev={saved.revision} learning={saved.learning_enabled}",
                )
                return 0

            if args.command == "revise":
                updates: dict[str, Any] = {}
                if args.updates:
                    updates.update(json.loads(Path(args.updates).read_text(encoding="utf-8")))
                if args.goals is not None:
                    updates["goals"] = _split_csv(args.goals)
                if args.capabilities is not None:
                    updates["existing_capabilities"] = _split_csv(args.capabilities)
                if args.public_concepts is not None:
                    updates["public_concepts"] = _split_csv(args.public_concepts)
                revised, reopened = store.revise_profile(args.project_id, updates, note=args.note)
                payload = {"profile": revised.to_dict(), "reopened_assessments": reopened}
                _emit(
                    payload,
                    as_json=args.json,
                    summary=f"revised {revised.project_id} rev={revised.revision} reopened={len(reopened)}",
                )
                return 0

            if args.command in ("pause", "resume"):
                is_pause = args.command == "pause"
                target_learning = args.learning or not args.execution
                target_exec = args.execution
                prof: ProjectProfile | None = None
                if target_learning:
                    prof = store.set_learning_enabled(args.project_id, not is_pause)
                if target_exec:
                    prof = store.set_execution_paused(args.project_id, is_pause)
                assert prof is not None
                _emit(
                    prof.to_dict(),
                    as_json=args.json,
                    summary=f"{args.command} {prof.project_id}: learning_enabled={prof.learning_enabled} execution_paused={prof.execution_paused}",
                )
                return 0

            if args.command == "unregister":
                result = store.unregister_profile(args.project_id, retention=args.retention)
                _emit(
                    result,
                    as_json=args.json,
                    summary=f"unregistered {args.project_id} retention={result['retention']} revoked={result['revoked_assessments']}",
                )
                return 0

            if args.command == "intake":
                transport: Transport
                if args.fixture_map:
                    transport = _load_fixture_transport(args.fixture_map)
                elif args.live:
                    transport = HttpTransport()
                else:
                    raise LearnerError("Offline by default: pass --fixture-map <file> or explicit --live")
                result = run_intake(
                    store,
                    args.mode,
                    transport,
                    project_id=args.project_id,
                    release_repos=args.repo,
                    arxiv_query=args.arxiv_query,
                )
                _emit(
                    result,
                    as_json=args.json,
                    summary=(
                        f"intake {result['mode']} status={result['status']} "
                        f"found={result.get('artifacts_found', 0)} created={result.get('assessments_created', 0)} "
                        f"assessment_status={result.get('assessment_status', 'unconfigured')}"
                    ),
                )
                return 0

            if args.command == "status":
                store.recover_stale_dispatches()
                profiles = (
                    [p for p in [store.get_profile(args.project_id)] if p is not None]
                    if args.project_id
                    else store.list_profiles()
                )
                assessments = store.list_assessments(args.project_id)
                coverage = store.list_coverage()
                outcomes = store.list_outcomes(args.project_id)
                unknowns = store.list_dispatches(
                    project_id=args.project_id, state="unknown", include_shared=True
                )
                model_dispatches = [
                    d
                    for d in store.list_dispatches(project_id=args.project_id, include_shared=True)
                    if d.scope_type in ("shared_triage", "query_planning", "deep_hydrogen", "deep_carbon", "deep_synthesis")
                ]
                has_assessed = any(a.assessment_status != "unconfigured" for a in assessments) or bool(model_dispatches)
                if unknowns:
                    status_label = "unknown_pending_reconciliation"
                    status_note = "Unknown dispatch pending reconciliation."
                elif has_assessed:
                    status_label = "assessed"
                    status_note = "Model assessment route active via central policy."
                else:
                    status_label = "unconfigured"
                    status_note = "No model assessment route configured in foundation."
                assessment_summary = summarize_assessment_queue(
                    store,
                    project_ids=[args.project_id] if args.project_id else None,
                )
                payload = {
                    "assessment_status": status_label,
                    "assessment_note": status_note,
                    "assessment_summary": assessment_summary,
                    "profiles": [p.to_dict() for p in profiles],
                    "assessments": [a.to_dict() for a in assessments],
                    "coverage": [c.to_dict() for c in coverage],
                    "outcomes": [o.to_dict() for o in outcomes],
                    "unknown_dispatches": [u.to_dict() for u in unknowns],
                    "shared_budget_used": {
                        "daily_calls": store.budget_used("__shared__", "daily_calls"),
                        "weekly_deep_calls": store.budget_used("__shared__", "weekly_deep_calls"),
                        "weekly_carbon_calls": store.budget_used("__shared__", "weekly_carbon_calls"),
                        "daily_metadata_items": store.budget_used("__shared__", "daily_metadata_items"),
                        "daily_fetches": store.budget_used("__shared__", "daily_fetches"),
                    },
                    "shared_budget_status": store.get_shared_budget_status(),
                    "trial_grant": store.get_trial_status(),
                    "languages": get_language_preferences(store, project_id=args.project_id),
                }
                _emit(
                    payload,
                    as_json=args.json,
                    summary=(
                        f"profiles={len(profiles)} assessments={len(assessments)} "
                        f"seen_pending={assessment_summary['seen_pending']} "
                        f"evidence_deferred={assessment_summary['evidence_deferred']} "
                        f"coverage={len(coverage)} outcomes={len(outcomes)} "
                        f"unknown_dispatches={len(unknowns)} assessment_status={status_label}"
                    ),
                )
                return 0

            if args.command in ("due", "review"):
                active_profiles = [p for p in store.list_profiles() if p.learning_enabled]
                if not active_profiles:
                    payload = {"status": "noop", "reason": "no_active_profiles", "modes_run": [], "coverage": []}
                    _emit(payload, as_json=args.json, summary="noop: no active learning-enabled profiles")
                    return 0
                if args.fixture_map:
                    if args.cli_path:
                        raise LearnerError(
                            "--cli-path cannot be used with --fixture-map; offline fixture mode never starts CLI inference"
                        )
                    transport = _load_fixture_transport(args.fixture_map)
                    runner = _load_fixture_model_runner(args.model_fixture, policy_path=args.policy_path)
                elif args.live:
                    transport = HttpTransport()
                    runner = AgyCliRunner(policy_path=args.policy_path, cli_path=args.cli_path)
                else:
                    raise LearnerError("Offline by default: pass --fixture-map <file> or explicit --live")
                if args.command == "due":
                    result = run_due(
                        store,
                        transport,
                        runner,
                        release_repos=args.repo,
                        arxiv_query=args.arxiv_query,
                        plan_queries=args.plan_queries,
                        refresh_deferred=args.refresh_deferred,
                    )
                else:
                    result = run_review(
                        store,
                        transport,
                        runner,
                        project_id=args.project_id,
                        mode=args.mode,
                        release_repos=args.repo,
                        arxiv_query=args.arxiv_query,
                        plan_queries=args.plan_queries,
                        refresh_deferred=args.refresh_deferred,
                    )
                _emit(
                    result,
                    as_json=args.json,
                    summary=(
                        f"{args.command} status={result['status']} "
                        f"created={result.get('assessments_created', 0)} "
                        f"proposed={result.get('assessment', {}).get('proposed', 0)}"
                    ),
                )
                return 0

            if args.command == "results":
                res = get_recommendations(
                    store,
                    project_id=args.project_id,
                    limit=args.limit,
                    include_coverage=args.coverage,
                    user_language=args.user_language,
                    deliverable_language=args.deliverable_language,
                    input_language=args.input_language,
                )
                if args.json:
                    print(json.dumps(res, indent=2, sort_keys=True))
                else:
                    rendered = format_user_recommendations(res)
                    if rendered:
                        print(rendered)
                return 0

            if args.command == "trial":
                if sum(bool(flag) for flag in (args.grant, args.unlimited, args.revoke)) > 1:
                    raise LearnerError("Specify --grant, --unlimited, or --revoke, not conflicting actions")
                if args.unlimited:
                    if args.max_calls is not None or args.expires_at is not None:
                        raise LearnerError("--unlimited cannot be combined with finite --max-calls or --expires-at")
                    if not (args.authority and args.authority.strip()):
                        raise LearnerError("--unlimited requires non-empty --authority recording owner authorization")
                    t_status = store.set_trial_grant(
                        unlimited=True,
                        grant_id=args.grant_id,
                        scope=args.scope,
                        granted_by=args.granted_by or "owner",
                        authority=args.authority,
                    )
                elif args.grant:
                    if args.max_calls is None or not args.expires_at:
                        raise LearnerError("--grant requires --max-calls and --expires-at")
                    exp_val: float | str = args.expires_at
                    try:
                        exp_val = float(args.expires_at)
                    except ValueError:
                        exp_val = args.expires_at
                    t_status = store.set_trial_grant(
                        max_model_calls=args.max_calls,
                        expires_at=exp_val,
                        grant_id=args.grant_id,
                        scope=args.scope,
                        granted_by=args.granted_by or "host",
                        authority=args.authority,
                    )
                elif args.revoke:
                    t_status = store.revoke_trial_grant()
                else:
                    t_status = store.get_trial_status()
                max_label = "unlimited" if t_status.get("mode") == "unlimited" else t_status["max_model_calls"]
                _emit(
                    t_status,
                    as_json=args.json,
                    summary=(
                        f"trial state={t_status['state']} mode={t_status.get('mode', 'inactive')} "
                        f"used={t_status['used']} cumulative={t_status.get('cumulative_model_calls', t_status['used'])} "
                        f"max={max_label}"
                    ),
                )
                return 0

            if args.command == "prefer":
                res = apply_feedback_preference(store, args.project_id, args.note)
                _emit(
                    res,
                    as_json=args.json,
                    summary=f"preferences updated ({res['updated']}) for {args.project_id}",
                )
                return 0

            if args.command == "language":
                langs = set_language_preferences(
                    store,
                    project_id=args.project_id,
                    user_language=args.user_language,
                    deliverable_language=args.deliverable_language,
                )
                _emit(
                    langs,
                    as_json=args.json,
                    summary=f"languages: user={langs['user_language']} deliverable={langs['deliverable_language']} internal={langs['internal_language']}",
                )
                return 0

            if args.command == "feedback":
                outcome = store.record_outcome_reference(
                    project_id=args.project_id,
                    assessment_id=args.assessment_id,
                    external_experiment_ref=args.experiment_ref,
                    external_release_ref=args.release_ref,
                    status=args.status,
                    baseline_summary=args.baseline,
                    outcome_summary=args.outcome,
                    evaluator=args.evaluator,
                    feedback_note=args.note,
                )
                _emit(
                    outcome.to_dict(),
                    as_json=args.json,
                    summary=f"outcome {outcome.outcome_id} status={outcome.status} ref={outcome.external_experiment_ref}",
                )
                return 0

            if args.command == "reconcile":
                store.recover_stale_dispatches()
                rec = store.reconcile_dispatch(
                    args.reservation_id,
                    outcome=args.outcome,
                    actual_units=args.actual_units,
                    note=args.note,
                )
                _emit(
                    rec.to_dict(),
                    as_json=args.json,
                    summary=f"reconciled {rec.reservation_id} state={rec.state} actual_units={rec.actual_units}",
                )
                return 0
        finally:
            store.close()
    except (LearnerError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
