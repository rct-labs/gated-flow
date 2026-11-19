#!/usr/bin/env python3
"""Concise Flow adapter over the standalone learner public API.

No duplicate engine, storage, or skill registry. Read-only resume integration
never issues network or model calls and leaves Flow usable if learner is absent.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import importlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Sequence

HERE = Path(__file__).resolve().parent
REPO_HOME = HERE.parent

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def default_storage_root() -> Path:
    env_root = os.environ.get("FLOW_LEARNER_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".local" / "share")
    return (base / "flow" / "learner").resolve()


def import_learner():
    """Import standalone learner if installed or vendored in checkout; return None when absent."""
    if os.environ.get("FLOW_LEARNER_DISABLED", "").strip() == "1":
        return None
    if str(REPO_HOME) not in sys.path and (REPO_HOME / "learner" / "__init__.py").is_file():
        sys.path.insert(0, str(REPO_HOME))
    try:
        return importlib.import_module("learner")
    except ImportError:
        return None


def is_flow_repo(root: Path) -> bool:
    return all((root / p).is_file() for p in
               ("gate/gate.py", "flow/continuity.py", "skills/flow-run/SKILL.md"))


def project_root(path: Path) -> Path:
    root = path.resolve()
    if not root.is_dir():
        raise ValueError("Project directory does not exist")
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                                capture_output=True, text=True)
    except FileNotFoundError:
        return root
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else root


def generic_profile(learner_mod: Any, root: Path, project_id: str | None = None, *, goals=(), mission_override=None) -> Any:
    pid = project_id or f"{root.name}-{hashlib.sha256(str(root).encode()).hexdigest()[:12]}"
    local = root / ".flow" / "learner-profile.json"
    raw = json.loads(local.read_text()) if local.is_file() else {}
    if raw and Path(raw.get("root_path", "")).resolve() != root:
        raise learner_mod.ValidationError("Local learner profile must bind root_path to this repository")
    if project_id and raw.get("project_id", project_id) != project_id:
        raise learner_mod.ValidationError("Local profile project identity differs from --project-id")
    draft = learner_mod.draft_profile_from_docs(pid, root, name=root.name).profile.to_dict()
    draft["public_concepts"] = []  # Unauthored local docs are private context only.
    return learner_mod.ProjectProfile.from_dict({
        **draft,
        "project_id": pid, "name": root.name, "root_path": str(root),
        "mission": draft["mission"],
        **raw,
        **({"goals": list(goals)} if goals else {}),
        **({"mission": mission_override} if mission_override else {}),
        "root_path": str(root), "adapter": "standalone",
        "learning_enabled": True, "execution_paused": True,
        "export_policy": {"allow_public_concept_queries": False,
                          "allow_shared_public_evidence": False,
                          "private_retention": "purge_private", **raw.get("export_policy", {})},
        "provenance": {**draft["provenance"], **raw.get("provenance", {}), "finite_project_budget": True,
                       "outcome_bridge": True},
    })


def detect_input_language(text: str | None) -> str | None:
    """Lightweight deterministic input-language fallback (zh / es / en)."""
    raw = str(text or "").strip()
    if not raw:
        return None
    if re.search(r"[\u4e00-\u9fff]", raw):
        return "zh"
    low = raw.lower()
    if re.search(r"[áéíóúñ¿¡]|\b(recomendaciones|mejoras|proyecto|pausa|idioma)\b", low):
        return "es"
    if re.search(r"[a-zA-Z]", raw):
        return "en"
    return None


def _inspect_flow_repo_state(repo_root: Path) -> dict[str, Any]:
    """Read bounded maintained capabilities, mission, and queue pause state from target repo only."""
    capabilities: list[str] = []
    if (repo_root / "gate" / "gate.py").is_file():
        capabilities.append("task admission, pre-commit gate, and bounded runner loop")
    if (repo_root / "flow" / "continuity.py").is_file():
        capabilities.append("host session continuity resume checkpoint recall")
    if (repo_root / "flow" / "release.py").is_file():
        capabilities.append("reversible local release stage activate rollback")
    if (repo_root / "gate" / "review_packet.py").is_file():
        capabilities.append("bounded review packet and repair lifecycle")
    if (repo_root / "learner" / "__init__.py").is_file():
        capabilities.append("standalone two-mode directed learner foundation")
    skills_dir = repo_root / "skills"
    if skills_dir.is_dir():
        skill_names = sorted(p.parent.name for p in skills_dir.glob("*/SKILL.md"))
        if skill_names:
            capabilities.append("modular workflow skills: " + ", ".join(skill_names))

    mission = ""
    source_docs: list[str] = []
    ctx_path = repo_root / "CONTEXT.md"
    if ctx_path.is_file():
        source_docs.append("CONTEXT.md")
        ctx_text = ctx_path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^##\s+1\.\s+Goal[^\n]*\n(.*?)(?=^##\s|\Z)", ctx_text, re.S | re.M)
        if m:
            lines = [ln.strip() for ln in m.group(1).splitlines() if ln.strip() and not ln.strip().startswith("<!--")]
            if lines:
                mission = " ".join(lines)[:300]
    if (repo_root / "README.md").is_file():
        source_docs.append("README.md")
    if (repo_root / "docs" / "autonomy.md").is_file():
        source_docs.append("docs/autonomy.md")

    cr1_paused = False
    head_blocked = False
    q_path = repo_root / "TASK_QUEUE.md"
    if q_path.is_file():
        source_docs.append("TASK_QUEUE.md")
        q_text = q_path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\|\s*\d+\s*\|\s*CR-1\s*\|[^|]*\|\s*`BLOCKED`\s*\|", q_text):
            cr1_paused = True
        head_blocked = cr1_paused and not bool(re.search(r"\|\s*`(?:TODO|IN_PROGRESS)`\s*\|", q_text))

    return {
        "capabilities": tuple(capabilities),
        "mission": mission or f"Maintain {repo_root.name} deterministic task gating, continuity, and governed skills.",
        "source_docs": tuple(source_docs),
        "cr1_paused": cr1_paused,
        "execution_paused": head_blocked,
    }


def derive_flow_profile_draft(
    learner_mod: Any,
    repo_root: Path,
    *,
    project_id: str | None = None,
    goals: Sequence[str] = (),
    mission_override: str | None = None,
) -> Any:
    """Derive an unregistered ProjectProfileDraft from target repo's actual maintained state."""
    root = repo_root.resolve()
    if not root.is_dir():
        raise learner_mod.ValidationError(f"Repository directory not found: {root}")
    state = _inspect_flow_repo_state(root)
    base_draft = learner_mod.draft_profile_from_docs(
        project_id or root.name.lower().replace(" ", "-"),
        root,
        name=root.name,
        adapter="flow",
        mission_override=mission_override or state["mission"],
    )
    prof = base_draft.profile
    eff_goals = tuple(g.strip() for g in goals if str(g).strip()) or prof.goals or (
        "improve evaluation harnesses and evidence-driven skill guidance",
    )
    eff_caps = tuple(dict.fromkeys([*state["capabilities"], *prof.existing_capabilities]))[:10]
    eff_concepts = prof.public_concepts or ("evaluation harness", "task scheduling", "workflow skills")
    exclusions = tuple(dict.fromkeys([*prof.exclusions, "resume blocked CR-1", "bypass gate model policy"]))
    merged = prof.to_dict() | {
        "existing_capabilities": list(eff_caps),
        "goals": list(eff_goals),
        "exclusions": list(exclusions),
        "public_concepts": list(eff_concepts),
        "private_terms": list(dict.fromkeys([*prof.private_terms, *(["CR-1"] if state["cr1_paused"] else [])])),
        "learning_enabled": True,
        "execution_paused": bool(state["execution_paused"]),
        "provenance": {
            **prof.provenance,
            "origin": "flow_learn_onboard",
            "source_docs": list(dict.fromkeys([*state["source_docs"], *base_draft.source_docs])),
            "cr1_paused": state["cr1_paused"],
            "eligible_for_host_activation": True,
        },
    }
    return learner_mod.ProjectProfileDraft(
        profile=learner_mod.ProjectProfile.from_dict(merged),
        activated=False,
        source_docs=tuple(merged["provenance"]["source_docs"]),
        warnings=base_draft.warnings,
    )


def _resolve_project_id(store: Any, repo_root: Path, explicit_project_id: str | None) -> str | None:
    if explicit_project_id:
        prof = store.get_profile(explicit_project_id)
        if prof is not None and Path(prof.root_path).resolve() != repo_root.resolve():
            raise ValueError("Project identity belongs to a different repository")
        for other in store.list_profiles():
            if Path(other.root_path).resolve() == repo_root.resolve() and other.project_id != explicit_project_id:
                raise ValueError("Repository already belongs to a different project identity")
        return explicit_project_id if prof is not None else None
    resolved_repo = repo_root.resolve()
    for prof in store.list_profiles():
        try:
            if Path(prof.root_path).resolve() == resolved_repo:
                return prof.project_id
        except OSError:
            continue
    return None


def resume_learning_summary(
    repo_root: Path,
    *,
    storage_root: Path | None = None,
    user_language: str | None = None,
    input_text: str | None = None,
    include_status: bool = False,
) -> dict[str, Any]:
    """Read-only learning summary for `flow resume`: zero network, zero model calls, quiet when empty."""
    learner_mod = import_learner()
    if learner_mod is None:
        return {
            "available": False,
            "quiet": True,
            "recommendations": [],
            "reason": "learner_unavailable",
            "status_requested": include_status,
        }
    root = (storage_root or default_storage_root()).resolve()
    db_file = root / "learner.sqlite3"
    if not db_file.is_file():
        return {
            "available": False,
            "quiet": True,
            "recommendations": [],
            "reason": "learner_store_uninitialized",
            "status_requested": include_status,
        }
    try:
        store = learner_mod.LearnerStore(root)
    except Exception as exc:  # noqa: BLE001 - resume must remain usable if learner store fails
        return {
            "available": False,
            "quiet": True,
            "recommendations": [],
            "reason": f"learner_store_error:{type(exc).__name__}",
            "status_requested": include_status,
        }
    try:
        pid = _resolve_project_id(store, repo_root, None)
        if pid is None:
            return {
                "available": True,
                "registered": False,
                "quiet": True,
                "recommendations": [],
                "reason": "project_not_registered",
                "status_requested": include_status,
            }
        rec_res = learner_mod.get_recommendations(
            store,
            project_id=pid,
            limit=3,
            include_coverage=include_status,
            user_language=user_language,
            input_language=detect_input_language(input_text),
        )
        out: dict[str, Any] = {
            "available": True,
            "registered": True,
            "project_id": pid,
            "quiet": bool(rec_res.get("quiet", True)),
            "total_available": int(rec_res.get("total_available", 0)),
            "user_language": rec_res.get("user_language", "en"),
            "deliverable_language": rec_res.get("deliverable_language", "en"),
            "labels": rec_res.get("labels", {}),
            "recommendations": list(rec_res.get("recommendations") or [])[:3],
            "status_requested": include_status,
        }
        if include_status:
            prof = store.get_profile(pid)
            out["learning_enabled"] = bool(prof.learning_enabled) if prof else False
            out["execution_paused"] = bool(prof.execution_paused) if prof else False
            out["coverage"] = rec_res.get("coverage", [])
            out["incomplete_coverage"] = bool(rec_res.get("incomplete_coverage", False))
            out["unknown_dispatches"] = rec_res.get("unknown_dispatches", [])
            out["shared_budget_status"] = store.get_shared_budget_status()
            out["trial_grant"] = store.get_trial_status()
        return out
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "quiet": True,
            "recommendations": [],
            "reason": f"learner_read_error:{type(exc).__name__}",
            "status_requested": include_status,
        }
    finally:
        store.close()


def _emit(payload: dict[str, Any], *, as_json: bool, summary: str) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    elif summary:
        print(summary)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=argparse.SUPPRESS)
    common.add_argument("--storage-root", default=argparse.SUPPRESS)
    common.add_argument("--project-id", default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    ap = argparse.ArgumentParser(prog="flow learn", description="Goal-level Flow adapter for standalone learner")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--storage-root")
    ap.add_argument("--project-id")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="subcmd")

    p_on = sub.add_parser("onboard", parents=[common], help="Goal-level explicit project onboarding")
    p_on.add_argument("--goal", action="append", default=[])
    p_on.add_argument("--mission")
    p_on.add_argument("--draft-only", action="store_true")

    p_chk = sub.add_parser("check", parents=[common], help="Run bounded learning check (offline fixture or explicit live)")
    p_chk.add_argument("--mode", choices=("project-led", "opportunity-led", "both"), default="project-led")
    p_chk.add_argument("--fixture-map")
    p_chk.add_argument("--model-fixture")
    p_chk.add_argument("--live", action="store_true")
    p_chk.add_argument("--policy-path")
    p_chk.add_argument("--cli-path")
    p_chk.add_argument("--repo-slug", action="append", default=[])

    p_res = sub.add_parser("results", parents=[common], help="Show at most 3 learning recommendations (quiet when empty)")
    p_res.add_argument("--limit", type=int, default=3)
    p_res.add_argument("--coverage", action="store_true")
    p_res.add_argument("--user-language")
    p_res.add_argument("--deliverable-language")
    p_res.add_argument("--input-text")

    p_fb = sub.add_parser("feedback", parents=[common], help="Record natural-language preference, language, or experiment outcome")
    p_fb.add_argument("text", nargs="*")
    p_fb.add_argument("--note")
    p_fb.add_argument("--assessment-id")
    p_fb.add_argument("--experiment-ref")
    p_fb.add_argument("--status", choices=("accepted", "rejected", "inconclusive"))
    p_fb.add_argument("--baseline")
    p_fb.add_argument("--outcome")
    p_fb.add_argument("--evaluator")
    p_fb.add_argument("--release-ref")
    p_fb.add_argument("--user-language")
    p_fb.add_argument("--deliverable-language")

    sub.add_parser("pause", parents=[common], help="Pause learning independently without changing execution state")
    sub.add_parser("resume", parents=[common], help="Resume learning independently without clearing execution pauses")
    sub.add_parser("status", parents=[common], help="Show learning registration, coverage, failures, and trial status")
    p_un = sub.add_parser("unregister", parents=[common], help="Unregister this project using its private retention policy")
    p_un.add_argument("--retention", choices=("purge_private", "retain_redacted"))
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    learner_mod = import_learner()
    if learner_mod is None:
        print("flow learn: learner package is unavailable", file=sys.stderr)
        return 1

    storage_root = Path(args.storage_root).resolve() if args.storage_root else default_storage_root()

    try:
        repo_root = project_root(Path(args.repo))
        if not repo_root.is_dir():
            raise ValueError("Repository directory does not exist")
        if args.subcmd is None:
            store = learner_mod.LearnerStore(storage_root)
            try:
                pid = _resolve_project_id(store, repo_root, args.project_id)
                created = pid is None
                if created:
                    if is_flow_repo(repo_root):
                        profile = derive_flow_profile_draft(learner_mod, repo_root, project_id=args.project_id).profile
                    else:
                        profile = generic_profile(learner_mod, repo_root, args.project_id)
                    try:
                        saved = store.register_profile(profile)
                    except learner_mod.ValidationError:
                        saved = store.get_profile(profile.project_id)
                        if saved is None or Path(saved.root_path).resolve() != repo_root:
                            raise
                        created = False
                    pid = saved.project_id
                prof = store.get_profile(pid)
                res = learner_mod.get_recommendations(store, project_id=pid)
                from learner.feedback import eligible_outcomes
                verified_count = sum(1 for _ in eligible_outcomes(store, pid))
                payload = {"registered": created, "profile": prof.to_dict(), "verified_outcomes": verified_count, **res}
                _emit(payload, as_json=args.json,
                      summary=f"{prof.name}: learning {'enabled' if prof.learning_enabled else 'paused'}; verified outcomes={verified_count}; recommendations={res['total_available']}; {'waiting for feedback' if not verified_count else 'local feedback available'}")
                if not args.json:
                    rendered = learner_mod.format_user_recommendations(res)
                    if rendered:
                        print(rendered)
                return 0
            finally:
                store.close()
        if args.subcmd == "onboard":
            if is_flow_repo(repo_root):
                draft = derive_flow_profile_draft(
                    learner_mod, repo_root, project_id=args.project_id,
                    goals=args.goal, mission_override=args.mission,
                )
            else:
                profile = generic_profile(learner_mod, repo_root, args.project_id, goals=args.goal, mission_override=args.mission)
                draft = learner_mod.ProjectProfileDraft(profile=profile, activated=False, source_docs=(), warnings=())
            if args.draft_only:
                _emit(draft.to_dict(), as_json=args.json, summary=f"draft {draft.profile.project_id}: activated=False")
                return 0
            store = learner_mod.LearnerStore(storage_root)
            try:
                _resolve_project_id(store, repo_root, draft.profile.project_id)
                existing = store.get_profile(draft.profile.project_id)
                if existing is not None:
                    updates: dict[str, Any] = {
                        "existing_capabilities": list(draft.profile.existing_capabilities),
                        "execution_paused": draft.profile.execution_paused,
                    }
                    if args.goal:
                        updates["goals"] = list(draft.profile.goals)
                    if args.mission:
                        updates["mission"] = draft.profile.mission
                    saved, reopened = store.revise_profile(
                        draft.profile.project_id,
                        updates,
                        note="flow_learn_onboard_refresh",
                    )
                    _emit(
                        {"profile": saved.to_dict(), "reopened": reopened, "already_registered": True},
                        as_json=args.json,
                        summary=f"onboarded {saved.project_id} rev={saved.revision} learning={saved.learning_enabled}",
                    )
                else:
                    saved = store.register_profile(draft.profile)
                    _emit(
                        {"profile": saved.to_dict(), "reopened": [], "already_registered": False},
                        as_json=args.json,
                        summary=f"onboarded {saved.project_id} rev={saved.revision} learning={saved.learning_enabled}",
                    )
                return 0
            finally:
                store.close()

        store = learner_mod.LearnerStore(storage_root)
        try:
            pid = _resolve_project_id(store, repo_root, args.project_id)
            if args.subcmd == "unregister":
                if not pid:
                    raise ValueError("Project is not registered")
                result = store.unregister_profile(pid, retention=args.retention)
                _emit(result, as_json=args.json, summary=f"unregistered {pid}: retention={result['retention']} revoked={result['revoked_assessments']}")
                return 0
            if pid is None and args.subcmd != "status":
                raise ValueError("Project is not registered; run flow learn")
            if args.subcmd == "check":
                if not pid:
                    raise learner_mod.ValidationError(f"Project is not registered for {repo_root}; run `flow learn onboard` first")
                from learner.cli import _load_fixture_model_runner, _load_fixture_transport

                if args.fixture_map:
                    if args.cli_path:
                        raise learner_mod.ValidationError("--cli-path cannot be used with --fixture-map")
                    transport = _load_fixture_transport(args.fixture_map)
                    runner = _load_fixture_model_runner(args.model_fixture, policy_path=args.policy_path)
                elif args.live:
                    transport = learner_mod.HttpTransport()
                    runner = learner_mod.AgyCliRunner(policy_path=args.policy_path, cli_path=args.cli_path)
                else:
                    raise learner_mod.LearnerError("Offline by default: pass --fixture-map <file> or explicit --live")
                res = learner_mod.run_review(
                    store,
                    transport,
                    runner,
                    project_id=pid,
                    mode=args.mode,
                    release_repos=args.repo_slug,
                )
                _emit(
                    res,
                    as_json=args.json,
                    summary=f"check {pid}: status={res['status']} proposed={res.get('assessment', {}).get('proposed', 0)}",
                )
                return 0

            if args.subcmd == "results":
                res = learner_mod.get_recommendations(
                    store,
                    project_id=pid,
                    limit=min(max(1, args.limit), 3),
                    include_coverage=args.coverage,
                    user_language=args.user_language,
                    deliverable_language=args.deliverable_language,
                    input_language=detect_input_language(args.input_text),
                )
                if args.json:
                    print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
                else:
                    rendered = learner_mod.format_user_recommendations(res)
                    if rendered:
                        print(rendered)
                return 0

            if args.subcmd == "feedback":
                if args.user_language or args.deliverable_language:
                    langs = learner_mod.set_language_preferences(
                        store,
                        project_id=pid,
                        user_language=args.user_language,
                        deliverable_language=args.deliverable_language,
                    )
                    if not (args.note or args.text or args.assessment_id):
                        _emit(langs, as_json=args.json, summary=f"languages updated: user={langs['user_language']} deliverable={langs['deliverable_language']}")
                        return 0
                if not pid:
                    raise learner_mod.ValidationError(f"Project is not registered for {repo_root}")
                if args.assessment_id:
                    if not (args.status and args.experiment_ref and args.baseline and args.outcome and args.evaluator):
                        raise learner_mod.ValidationError("Outcome feedback requires --status, --experiment-ref, --baseline, --outcome, and --evaluator")
                    out_ref = store.record_outcome_reference(
                        project_id=pid,
                        assessment_id=args.assessment_id,
                        external_experiment_ref=args.experiment_ref,
                        external_release_ref=args.release_ref,
                        status=args.status,
                        baseline_summary=args.baseline,
                        outcome_summary=args.outcome,
                        evaluator=args.evaluator,
                        feedback_note=args.note,
                    )
                    _emit(out_ref.to_dict(), as_json=args.json, summary=f"outcome {out_ref.outcome_id} status={out_ref.status}")
                    return 0
                note = (args.note or " ".join(args.text)).strip()
                if not note:
                    raise learner_mod.ValidationError("Provide natural-language feedback text or --note")
                pref_res = learner_mod.apply_feedback_preference(store, pid, note)
                _emit(pref_res, as_json=args.json, summary=f"feedback recorded ({pref_res['updated']}) for {pid}")
                return 0

            if args.subcmd in ("pause", "resume"):
                if not pid:
                    raise learner_mod.ValidationError(f"Project is not registered for {repo_root}")
                prof = store.set_learning_enabled(pid, args.subcmd == "resume")
                _emit(
                    prof.to_dict(),
                    as_json=args.json,
                    summary=f"{prof.name}: learning {'enabled' if prof.learning_enabled else 'paused'}",
                )
                return 0

            if args.subcmd == "status":
                profiles = [store.get_profile(pid).to_dict()] if pid else []
                coverage = []
                unknowns = [
                    u.to_dict()
                    for u in (store.list_dispatches(project_id=pid, state="unknown", include_shared=is_flow_repo(repo_root)) if pid else [])
                ]
                payload = {
                    "profiles": profiles,
                    "coverage": coverage,
                    "incomplete_coverage": any(c["incomplete"] for c in coverage),
                    "unknown_dispatches": unknowns,
                    "shared_budget_status": store.get_shared_budget_status(),
                    "trial_grant": store.get_trial_status(),
                    "languages": learner_mod.get_language_preferences(store, project_id=pid),
                }
                from learner.feedback import eligible_outcomes
                verified_count = sum(1 for _ in eligible_outcomes(store, pid)) if pid else 0
                recommendation_count = learner_mod.get_recommendations(store, project_id=pid)["total_available"] if pid else 0
                payload.update(verified_outcomes=verified_count, recommendations=recommendation_count)
                _emit(
                    payload,
                    as_json=args.json,
                    summary=(
                        f"{profiles[0]['name']}: learning {'enabled' if profiles[0]['learning_enabled'] else 'paused'}; "
                        f"verified outcomes={verified_count}; recommendations={recommendation_count}"
                        + ("; waiting for feedback" if recommendation_count and not verified_count else "")
                        if profiles else "Project is not registered"
                    ),
                )
                return 0
        finally:
            store.close()
    except (learner_mod.LearnerError, OSError, ValueError) as exc:
        print(f"flow learn: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
