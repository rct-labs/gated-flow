#!/usr/bin/env python3
"""Derived SKILL.md metadata index, progressive selection, task pins, and governed low-impact release path."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable, Sequence
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
REPO_HOME = HERE.parent
DEFAULT_POLICY_PATH = Path.home() / ".config/mini-model-routing/policy.json"
MODEL_POLICY_PY = REPO_HOME / "gate" / "model_policy.py"
ALLOWED_LOW_IMPACT_PATHS = frozenset({"skills/flow-run/references/inspect.md"})

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_FORBIDDEN_CANDIDATE_PATTERNS = (
    re.compile(r"\bskip\s+verify_cmd\b", re.I),
    re.compile(r"\bbypass\s+(?:gate|model_policy|pre-commit)\b", re.I),
    re.compile(r"\bunpause\s+cr-1\b", re.I),
    re.compile(r"\b(?:resume|unblock|clear|ignore)\b[^\n]{0,40}\bcr-1\b", re.I),
    re.compile(r"\bcr-1\b[^\n]{0,40}\b(?:resume|unblock|unpause|clear|todo)\b", re.I),
    re.compile(r"\bauto[-_ ]?resume\s+(?:cr-1|blocked)\b", re.I),
    re.compile(r"\bgrant_execution\b", re.I),
    re.compile(r"\bgit\s+push\b", re.I),
    re.compile(r"\bapprove\s+without\s+review\b", re.I),
    re.compile(r"--no-verify\b", re.I),
    re.compile(r"--dangerously-(?:skip-permissions|bypass-approvals-and-sandbox)\b", re.I),
)
_ADDED_LINE_FORBIDDEN_RE = re.compile(
    r"\b(?:worker_cmds|worker_models|policy\.json|model_policy(?:\.py)?|execution_paused|verify_cmd)\b",
    re.I,
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_frontmatter_metadata(text: str) -> tuple[dict[str, str], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    header, body = m.group(1), m.group(2)
    meta: dict[str, str] = {}
    current_key: str | None = None
    buf: list[str] = []
    for line in header.splitlines():
        kv = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if kv and not line.startswith((" ", "\t")):
            if current_key is not None:
                meta[current_key] = " ".join(buf).strip()
            current_key = kv.group(1).strip()
            val = kv.group(2).strip()
            buf = [] if val in (">", "|", "") else [val]
        elif current_key is not None and line.startswith((" ", "\t")):
            buf.append(line.strip())
    if current_key is not None:
        meta[current_key] = " ".join(buf).strip()
    return meta, body


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".flow-pin-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _atomic_write_text(path: Path, content: str) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"))


def build_skill_index(skills_root: Path | None = None) -> dict[str, Any]:
    """Derive metadata-only index from actual SKILL.md files without loading instructions."""
    root = (skills_root or (REPO_HOME / "skills")).resolve()
    entries: dict[str, dict[str, Any]] = {}
    if root.is_dir():
        for skill_md in sorted(root.glob("*/SKILL.md")):
            raw = skill_md.read_bytes()
            text = raw.decode("utf-8", errors="replace")
            meta, _ = _parse_frontmatter_metadata(text)
            name = meta.get("name") or skill_md.parent.name
            refs = []
            ref_dir = skill_md.parent / "references"
            if ref_dir.is_dir():
                for ref in sorted(ref_dir.glob("*.md")):
                    refs.append(
                        {
                            "rel_path": f"references/{ref.name}",
                            "sha256": _sha256_bytes(ref.read_bytes()),
                            "bytes": ref.stat().st_size,
                        }
                    )
            entries[name] = {
                "name": name,
                "description": meta.get("description", ""),
                "skill_dir": str(skill_md.parent),
                "skill_md_path": str(skill_md),
                "sha256": _sha256_bytes(raw),
                "bytes": len(raw),
                "references": refs,
            }
    return {
        "authority": "derived_index_only",
        "source_authority": "skills/*/SKILL.md",
        "skills": entries,
    }


def resolve_active_task_for_pin(repo: Path, task: str | None = None) -> str | None:
    """Resolve an explicit task ID or unambiguous IN_PROGRESS queue head task."""
    if task is not None and str(task).strip():
        return str(task).strip()
    resolved_repo = Path(repo).resolve()
    cfg_path = resolved_repo / ".gate" / "config.json"
    queue_rel = "TASK_QUEUE.md"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict) and isinstance(cfg.get("queue_file"), str) and cfg["queue_file"].strip():
                queue_rel = cfg["queue_file"].strip()
        except (OSError, ValueError):
            pass
    q_path = (resolved_repo / queue_rel).resolve()
    if not q_path.is_relative_to(resolved_repo) or not q_path.is_file():
        return None
    active: list[str] = []
    for line in q_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\|\s*\d+\s*\|\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*\|[^|]*\|\s*`IN_PROGRESS`\s*\|", line.strip())
        if m:
            active.append(m.group(1))
    if len(active) > 1:
        raise ValueError(f"Ambiguous IN_PROGRESS tasks in {queue_rel}: {', '.join(active)}; pass --task explicitly")
    return active[0] if active else None


def select_skills(
    request_text: str,
    skills_root: Path | None = None,
    *,
    repo: Path | None = None,
    task: str | None = None,
    task_type: str | None = None,
    constraints: dict[str, Any] | None = None,
    load_instructions: bool = False,
    load_references: Sequence[str] = (),
) -> dict[str, Any]:
    """Metadata-first progressive skill selection by task type, goal, and constraints."""
    if task is not None and repo is None:
        raise ValueError("task requires repo to resolve task pins")

    resolved_repo = Path(repo).resolve() if repo is not None else None
    eff_task = resolve_active_task_for_pin(resolved_repo, task=task) if resolved_repo is not None else None
    active_pin = load_task_pin(resolved_repo, eff_task) if (resolved_repo is not None and eff_task) else None

    effective_skills_root = skills_root
    if effective_skills_root is None and resolved_repo is not None and (resolved_repo / "skills").is_dir():
        effective_skills_root = resolved_repo / "skills"

    index = build_skill_index(effective_skills_root)
    skills_meta = index["skills"]
    if active_pin is not None:
        pinned_name = active_pin["skill_name"]
        pinned_skill_bytes = read_pinned_skill(resolved_repo, eff_task, "SKILL.md")
        pinned_meta, _ = _parse_frontmatter_metadata(pinned_skill_bytes.decode("utf-8", errors="replace"))
        base_entry = dict(skills_meta.get(pinned_name) or {})
        base_entry.update(
            {
                "name": pinned_meta.get("name") or pinned_name,
                "description": pinned_meta.get("description", base_entry.get("description", "")),
                "sha256": active_pin["skill_sha256"],
                "bytes": len(pinned_skill_bytes),
                "pinned": True,
            }
        )
        skills_meta[pinned_name] = base_entry

    cons = dict(constraints or {})
    paused = bool(cons.get("paused", False))
    read_only = bool(cons.get("read_only", False))
    norm_task_type = str(task_type or "").strip().lower()
    low = f"{task_type or ''} {request_text or ''}".lower()

    selected: list[str] = []
    recommended_refs: list[str] = []
    intent = "authorized_execution"
    execution_authorized = not (paused or read_only)

    learning_keywords = (
        "improvement opportunit",
        "improve",
        "learning",
        "recommend",
        "learn ",
        "feedback",
        "\u6539\u8fdb\u673a\u4f1a",
        "\u5b66\u4e60\u5efa\u8bae",
        "\u53ef\u6539\u8fdb",
    )
    debate_keywords = ("model-debate", "model debate", "debate", "\u8ba8\u8bba")
    queue_keywords = ("run-queue", "run queue", "admitted queue")
    plan_keywords = ("plan only", "plan-only", "checkpoint", "recall", "tidy", "insert task", "scaffold")

    matched_learning = [k for k in learning_keywords if k in low]
    matched_debate = [k for k in debate_keywords if k in low]
    matched_queue = [k for k in queue_keywords if k in low]
    matched_plan = [k for k in plan_keywords if k in low]

    base_reasons: list[str] = []
    if norm_task_type:
        base_reasons.append(f"task_type:{norm_task_type}")
    if paused:
        base_reasons.append("constraint:paused=true")
    if read_only:
        base_reasons.append("constraint:read_only=true")

    skipped_missing: list[str] = []
    branch_reasons: list[str] = []

    if matched_learning and "flow-run" in skills_meta:
        selected = ["flow-run"]
        intent = "learning_check"
        execution_authorized = False
        recommended_refs = ["references/inspect.md"]
        branch_reasons.extend(f"matched_keyword:{k.strip()}" for k in matched_learning)
        branch_reasons.extend(
            (
                "intent:learning_check",
                "execution_authorized:false(advisory_learning)",
                "recommended_reference:references/inspect.md(learning_inspection)",
            )
        )
    else:
        if matched_learning:
            skipped_missing.append("skipped_missing_skill:flow-run")
        if matched_debate and "model-debate" in skills_meta:
            selected = ["model-debate"]
            intent = "design_debate"
            execution_authorized = False
            branch_reasons.extend(f"matched_keyword:{k}" for k in matched_debate)
            branch_reasons.extend(
                (
                    "intent:design_debate",
                    "execution_authorized:false(design_debate)",
                )
            )
        else:
            if matched_debate:
                skipped_missing.append("skipped_missing_skill:model-debate")
            if matched_queue and "run-queue" in skills_meta:
                selected = ["run-queue"]
                intent = "paused_blocked" if paused else "queue_execution"
                branch_reasons.extend(f"matched_keyword:{k}" for k in matched_queue)
                branch_reasons.append(f"intent:{intent}")
                branch_reasons.append(
                    "execution_authorized:true"
                    if execution_authorized
                    else f"execution_authorized:false({'paused' if paused else 'read_only'})"
                )
            else:
                if matched_queue:
                    skipped_missing.append("skipped_missing_skill:run-queue")
                if matched_plan and "flow" in skills_meta:
                    selected = ["flow"]
                    intent = "planning"
                    execution_authorized = False
                    has_plan_kw = "plan" in low
                    recommended_refs = ["references/planning.md"] if has_plan_kw else ["references/continuity.md"]
                    branch_reasons.extend(f"matched_keyword:{k}" for k in matched_plan)
                    branch_reasons.extend(
                        (
                            "intent:planning",
                            "execution_authorized:false(planning)",
                            f"recommended_reference:{recommended_refs[0]}({'matched_plan' if has_plan_kw else 'continuity_default'})",
                        )
                    )
                else:
                    if matched_plan:
                        skipped_missing.append("skipped_missing_skill:flow")
                    if "flow-run" in skills_meta:
                        selected = ["flow-run"]
                        intent = "paused_blocked" if paused else "authorized_execution"
                        recommended_refs = ["references/inspect.md"]
                        branch_reasons.extend(
                            (
                                "route:default_host_router",
                                f"intent:{intent}",
                                "execution_authorized:true"
                                if execution_authorized
                                else f"execution_authorized:false({'paused' if paused else 'read_only'})",
                                "recommended_reference:references/inspect.md(host_inspect_entry)",
                            )
                        )

    reasons: dict[str, list[str]] = {}
    for name in selected:
        skill_reasons = [*base_reasons, *skipped_missing, *branch_reasons]
        if active_pin is not None and name == active_pin["skill_name"] and eff_task:
            pin_source = "explicit_task" if (task is not None and str(task).strip()) else "in_progress_head"
            skill_reasons.append(f"task_pin:{eff_task}@{active_pin['skill_sha256'][:10]}({pin_source})")
        reasons[name] = list(dict.fromkeys(skill_reasons))

    loaded_instructions: dict[str, str] = {}
    loaded_references: dict[str, str] = {}
    if load_instructions:
        for name in selected:
            if active_pin is not None and name == active_pin["skill_name"] and resolved_repo is not None and eff_task:
                pinned_text = read_pinned_skill(resolved_repo, eff_task, "SKILL.md").decode("utf-8")
                _, body = _parse_frontmatter_metadata(pinned_text)
                loaded_instructions[name] = body.strip()
                for rel_ref in load_references:
                    if rel_ref in (active_pin.get("files") or {}):
                        loaded_references[f"{name}/{rel_ref}"] = read_pinned_skill(
                            resolved_repo, eff_task, rel_ref
                        ).decode("utf-8")
                continue
            entry = skills_meta.get(name)
            if entry and "skill_md_path" in entry:
                _, body = _parse_frontmatter_metadata(Path(entry["skill_md_path"]).read_text(encoding="utf-8"))
                loaded_instructions[name] = body.strip()
            if entry and "skill_dir" in entry:
                skill_dir = Path(entry["skill_dir"])
                for rel_ref in load_references:
                    target = (skill_dir / rel_ref).resolve()
                    if target.is_relative_to(skill_dir) and target.is_file():
                        loaded_references[f"{name}/{rel_ref}"] = target.read_text(encoding="utf-8")

    pin_info = None
    if active_pin is not None and resolved_repo is not None and eff_task:
        pol = active_pin.get("model_policy") or {}
        pin_info = {k: v for k, v in pol.items() if k != "policy_json"}

    return {
        "index_authority": index["authority"],
        "metadata_only_first": True,
        "selected": selected,
        "reasons": reasons,
        "intent": intent,
        "execution_authorized": execution_authorized,
        "recommended_references": recommended_refs,
        "loaded_instructions": loaded_instructions,
        "loaded_references": loaded_references,
        "pinned": active_pin is not None,
        "pin_task": eff_task if active_pin is not None else None,
        "pin_path": str(_pin_file(resolved_repo, eff_task)) if (active_pin is not None and resolved_repo is not None and eff_task) else None,
        "pinned_model_policy": pin_info,
        "available_metadata": {k: {"name": v["name"], "description": v["description"]} for k, v in skills_meta.items()},
    }


def _model_policy_snapshot(policy_path: Path | None = None, model_policy_py: Path | None = None) -> dict[str, Any]:
    p_path = policy_path or DEFAULT_POLICY_PATH
    mp_path = model_policy_py or MODEL_POLICY_PY
    policy_ver: Any = "unknown"
    effective_date = ""
    policy_digest = "missing"
    policy_json: str | None = None
    if p_path.is_file():
        raw = p_path.read_bytes()
        policy_digest = _sha256_bytes(raw)
        policy_json = raw.decode("utf-8", errors="replace")
        try:
            obj = json.loads(policy_json)
            policy_ver = obj.get("version", "unknown")
            effective_date = str(obj.get("effective_date") or "")
        except ValueError:
            pass
    mp_digest = _sha256_bytes(mp_path.read_bytes()) if mp_path.is_file() else "missing"
    return {
        "version": policy_ver,
        "effective_date": effective_date,
        "policy_sha256": policy_digest,
        "guard_sha256": mp_digest,
        "policy_json": policy_json,
    }


def _resolve_skills_root(repo: Path, skill_name: str, explicit_skills_root: Path | None) -> tuple[Path, str]:
    if explicit_skills_root is not None:
        s_root = explicit_skills_root.resolve()
        eff = skill_name if (s_root / skill_name / "SKILL.md").is_file() else "flow-run"
        return s_root, eff
    repo_skills = repo / "skills"
    if (repo_skills / skill_name / "SKILL.md").is_file():
        return repo_skills.resolve(), skill_name
    if (repo_skills / "flow-run" / "SKILL.md").is_file():
        return repo_skills.resolve(), "flow-run"
    home_skills = (REPO_HOME / "skills").resolve()
    eff = skill_name if (home_skills / skill_name / "SKILL.md").is_file() else "flow-run"
    return home_skills, eff


def _pin_dir(repo: Path, task: str) -> Path:
    safe_task = re.sub(r"[^A-Za-z0-9_.-]", "_", task)
    return repo / ".gate" / "pins" / safe_task


def _pin_file(repo: Path, task: str) -> Path:
    safe_task = re.sub(r"[^A-Za-z0-9_.-]", "_", task)
    return repo / ".gate" / "pins" / f"{safe_task}.json"


def _materialize_pin_artifacts(repo: Path, task: str, pin_record: dict[str, Any]) -> None:
    pin_dir = _pin_dir(repo, task)
    pol = pin_record.get("model_policy") or {}
    pol_json = pol.get("policy_json")
    if isinstance(pol_json, str):
        pol_path = pin_dir / "policy.json"
        if not pol_path.exists():
            _atomic_write_bytes(pol_path, pol_json.encode("utf-8"))
    skill_name = str(pin_record.get("skill_name") or "flow-run")
    skill_root = pin_dir / "skills" / skill_name
    for rel_path, entry in (pin_record.get("files") or {}).items():
        dst = (skill_root / rel_path).resolve()
        if not dst.is_relative_to(skill_root.resolve()):
            raise ValueError(f"Unsafe pinned relative path: {rel_path!r}")
        if not dst.exists():
            _atomic_write_bytes(dst, entry["content"].encode("utf-8"))


def preview_task_pin(
    repo: Path,
    *,
    skill_name: str = "flow-run",
    skills_root: Path | None = None,
    policy_path: Path | None = None,
    model_policy_py: Path | None = None,
) -> dict[str, Any]:
    """Read current skill and model-policy metadata without creating an immutable task pin."""
    s_root, eff_name = _resolve_skills_root(repo, skill_name, skills_root)
    skill_md = s_root / eff_name / "SKILL.md"
    skill_sha = _sha256_bytes(skill_md.read_bytes()) if skill_md.is_file() else "missing"
    pol = _model_policy_snapshot(policy_path, model_policy_py)
    return {
        "pinned": False,
        "skill_name": eff_name,
        "skill_sha256": skill_sha,
        "model_policy": pol,
    }


def load_task_pin(repo: Path, task: str) -> dict[str, Any] | None:
    """Load and verify an active task's immutable pin record if present."""
    path = _pin_file(repo, task)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    for rel_path, entry in (data.get("files") or {}).items():
        raw = entry["content"].encode("utf-8")
        if _sha256_bytes(raw) != entry["sha256"]:
            raise ValueError(f"Pinned skill integrity mismatch for {task}:{rel_path}")
    pol = data.get("model_policy") or {}
    pol_json = pol.get("policy_json")
    if isinstance(pol_json, str) and pol.get("policy_sha256") not in (None, "missing"):
        if _sha256_bytes(pol_json.encode("utf-8")) != pol["policy_sha256"]:
            raise ValueError(f"Pinned policy snapshot integrity mismatch for {task}")
    return data


def resolve_pinned_policy_for_spawn(
    repo: Path,
    task: str,
    *,
    model_policy_py: Path | None = None,
) -> tuple[Path | None, str | None]:
    """Return verified pinned policy.json path for an active pinned task, or (None, None) if unpinned."""
    try:
        pin = load_task_pin(repo, task)
    except (OSError, ValueError, KeyError) as exc:
        return None, f"model_policy:corrupt task pin for {task!r}: {exc}; host must reconcile"
    if pin is None:
        return None, None
    pol = pin.get("model_policy") or {}
    mp_path = model_policy_py or MODEL_POLICY_PY
    actual_guard = _sha256_bytes(mp_path.read_bytes()) if mp_path.is_file() else "missing"
    pinned_guard = pol.get("guard_sha256")
    if not pinned_guard or pinned_guard == "missing" or actual_guard != pinned_guard:
        return (
            None,
            f"model_policy:pinned guard_sha256 mismatch for {task!r}; host must reconcile",
        )
    pol_json = pol.get("policy_json")
    pol_sha = pol.get("policy_sha256")
    if not isinstance(pol_json, str) or not pol_sha or pol_sha == "missing":
        return (
            None,
            f"model_policy:pinned policy snapshot missing for {task!r}; host must reconcile",
        )
    if _sha256_bytes(pol_json.encode("utf-8")) != pol_sha:
        return (
            None,
            f"model_policy:pinned policy snapshot integrity mismatch for {task!r}; host must reconcile",
        )
    pol_path = _pin_dir(repo, task) / "policy.json"
    if pol_path.is_file():
        if _sha256_bytes(pol_path.read_bytes()) != pol_sha:
            return (
                None,
                f"model_policy:materialized pinned policy.json tampered for {task!r}; host must reconcile",
            )
    else:
        _atomic_write_bytes(pol_path, pol_json.encode("utf-8"))
    return pol_path, None


def pin_task_start(
    repo: Path,
    task: str,
    *,
    skill_name: str = "flow-run",
    skills_root: Path | None = None,
    policy_path: Path | None = None,
    model_policy_py: Path | None = None,
    reference_rel_paths: Sequence[str] = ("references/inspect.md", "references/outcome.md"),
) -> dict[str, Any]:
    """Snapshot selected skill bytes and model-policy snapshot at task start (immutable once written)."""
    existing = load_task_pin(repo, task)
    if existing is not None:
        _materialize_pin_artifacts(repo, task, existing)
        return existing

    s_root, eff_name = _resolve_skills_root(repo, skill_name, skills_root)
    skill_dir = s_root / eff_name
    rel_candidates = list(reference_rel_paths)
    ref_dir = skill_dir / "references"
    if ref_dir.is_dir():
        for ref_file in sorted(ref_dir.glob("*.md")):
            rel = f"references/{ref_file.name}"
            if rel not in rel_candidates:
                rel_candidates.append(rel)

    files: dict[str, dict[str, Any]] = {}
    for rel_path in ("SKILL.md", *rel_candidates):
        target = (skill_dir / rel_path).resolve()
        if target.is_relative_to(skill_dir.resolve()) and target.is_file():
            raw = target.read_bytes()
            files[rel_path] = {
                "sha256": _sha256_bytes(raw),
                "bytes": len(raw),
                "content": raw.decode("utf-8"),
            }

    skill_sha = files.get("SKILL.md", {}).get("sha256", "missing")
    pin_record = {
        "format": 1,
        "pinned": True,
        "task": task,
        "skill_name": eff_name,
        "skill_sha256": skill_sha,
        "files": files,
        "model_policy": _model_policy_snapshot(policy_path, model_policy_py),
    }
    _atomic_write_text(_pin_file(repo, task), json.dumps(pin_record, indent=2, sort_keys=True) + "\n")
    _materialize_pin_artifacts(repo, task, pin_record)
    return pin_record


def read_pinned_skill(repo: Path, task: str, rel_path: str = "SKILL.md") -> bytes:
    """Read immutable pinned skill bytes for a task (never affected by later candidate activations)."""
    pin = load_task_pin(repo, task)
    if pin is None:
        raise FileNotFoundError(f"No task pin recorded for {task!r}")
    entry = (pin.get("files") or {}).get(rel_path)
    if entry is None:
        raise KeyError(f"Pinned file {rel_path!r} not in pin for {task!r}")
    return entry["content"].encode("utf-8")


def format_pin_line(repo: Path, task: str, *, skill_name: str = "flow-run") -> str:
    """Concise bounded pin line for gate task_packet."""
    pin = load_task_pin(repo, task)
    if pin is not None:
        pol = pin["model_policy"]
        ref_sha = ((pin.get("files") or {}).get("references/inspect.md") or {}).get("sha256", "")
        ref_part = f" inspect={ref_sha[:10]}" if ref_sha else ""
        pin_rel = _pin_file(repo, task).relative_to(repo).as_posix()
        skill_pin_dir = _pin_dir(repo, task) / "skills" / pin["skill_name"]
        skill_part = (
            f" skill_pin={skill_pin_dir.relative_to(repo).as_posix()}"
            if skill_pin_dir.is_dir()
            else ""
        )
        return (
            f"pins: skill={pin['skill_name']}@{pin['skill_sha256'][:10]}{ref_part} "
            f"model_policy=v{pol['version']}:{pol['policy_sha256'][:10]} "
            f"pin={pin_rel}{skill_part} (pinned)"
        )
    live = preview_task_pin(repo, skill_name=skill_name)
    pol = live["model_policy"]
    return (
        f"pins: skill={live['skill_name']}@{live['skill_sha256'][:10]} "
        f"model_policy=v{pol['version']}:{pol['policy_sha256'][:10]} (preview)"
    )


def _check_markdown_links(repo_root: Path, rel_path: str, text: str) -> list[str]:
    errors: list[str] = []
    source_path = (repo_root / rel_path).resolve()
    for target in _MD_LINK_RE.findall(text):
        if "://" in target or target.startswith("#"):
            continue
        name, _, fragment = target.partition("#")
        dest = (source_path.parent / unquote(name)).resolve() if name else source_path
        if not dest.is_file():
            errors.append(f"broken_link:{target}")
            continue
        if fragment:
            headings = re.findall(r"^#+\s+(.+)$", dest.read_text(encoding="utf-8"), re.M)
            anchors = {re.sub(r"[^\w\- ]", "", h.lower()).replace(" ", "-") for h in headings}
            if fragment not in anchors:
                errors.append(f"missing_anchor:{target}")
    return errors


def _score_inspect_cases(text: str) -> dict[str, bool]:
    low = text.lower()
    return {
        "positive_learning_evidence": (
            "flow resume" in low
            and "learning" in low
            and ("three" in low or "3" in low)
        ),
        "nl_improvement_routing": "flow learn" in low,
        "heldout_quiet_no_result": "quiet" in low,
        "heldout_paused_project": ("blocked" in low or "paused" in low) and ("never" in low or "do not" in low),
        "heldout_authorized_execution": "flow doctor" in low and "flow admit" in low,
    }


def _extract_markdown_section(text: str, heading_prefix: str) -> str | None:
    lines = text.splitlines()
    start_idx: int | None = None
    for idx, line in enumerate(lines):
        if line.startswith(heading_prefix):
            start_idx = idx
            break
    if start_idx is None:
        return None
    end_idx = len(lines)
    for idx in range(start_idx + 1, len(lines)):
        if lines[idx].startswith("## "):
            end_idx = idx
            break
    body = "\n".join(lines[start_idx:end_idx]).strip()
    return re.sub(r"\s*<!--.*?-->\s*$", "", body, flags=re.S).strip()


def evaluate_skill_candidate(
    repo_root: Path,
    *,
    rel_path: str,
    candidate_bytes: bytes,
    baseline_bytes: bytes | None = None,
    case_id: str = "SKC-FLOW-INSPECT-01",
    experiment_ref: str = "",
) -> dict[str, Any]:
    """Evaluate a scoped low-impact skill candidate against explicit baseline, held-out cases, and safety invariants.

    Note: static case scoring measures documentation coverage only, never behavioral efficacy.
    """
    errors: list[str] = []
    regressions: list[str] = []
    if rel_path not in ALLOWED_LOW_IMPACT_PATHS:
        errors.append(f"out_of_scope_skill_path:{rel_path}")
    if len(candidate_bytes) > 32_768:
        errors.append("candidate_exceeds_32kb")

    if baseline_bytes is not None:
        eff_baseline_bytes = baseline_bytes
    else:
        baseline_path = repo_root / rel_path
        eff_baseline_bytes = baseline_path.read_bytes() if baseline_path.is_file() else b""
    baseline_sha256 = _sha256_bytes(eff_baseline_bytes)
    candidate_sha256 = _sha256_bytes(candidate_bytes)

    try:
        cand_text = candidate_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "passed": False,
            "coverage_only": True,
            "case_id": case_id,
            "experiment_ref": experiment_ref,
            "baseline_sha256": baseline_sha256,
            "candidate_sha256": candidate_sha256,
            "baseline_score": 0,
            "candidate_score": 0,
            "regressions": ["invalid_utf8"],
            "errors": ["invalid_utf8"],
        }

    for pat in _FORBIDDEN_CANDIDATE_PATTERNS:
        if pat.search(cand_text):
            regressions.append(f"forbidden_authority_escalation:{pat.pattern}")

    for heading in ("## 0.", "## 1.", "## 2."):
        if heading not in cand_text:
            regressions.append(f"missing_required_heading:{heading}")
    for anchor in ("#scope-request-recovery", "#critical-safety-assumptions"):
        if anchor not in cand_text:
            regressions.append(f"missing_required_authority_anchor:{anchor}")
    errors.extend(_check_markdown_links(repo_root, rel_path, cand_text))

    baseline_text = eff_baseline_bytes.decode("utf-8", errors="replace")
    if baseline_text:
        for sec_prefix in ("## 0.", "## 2."):
            base_sec = _extract_markdown_section(baseline_text, sec_prefix)
            cand_sec = _extract_markdown_section(cand_text, sec_prefix)
            if base_sec is not None and cand_sec != base_sec:
                regressions.append(f"modified_non_learning_section:{sec_prefix}")
        for anchor in (
            "#model-routing-authority",
            "#scope-request-recovery",
            "#budget-continuation-decision",
            "#critical-safety-assumptions",
        ):
            if anchor in baseline_text and anchor not in cand_text:
                regressions.append(f"missing_baseline_authority_anchor:{anchor}")
        for diff_line in difflib.ndiff(baseline_text.splitlines(), cand_text.splitlines()):
            if diff_line.startswith("+ "):
                added = diff_line[2:]
                if _ADDED_LINE_FORBIDDEN_RE.search(added):
                    regressions.append(f"forbidden_added_authority_reference:{added.strip()[:80]}")

    base_cases = _score_inspect_cases(baseline_text)
    cand_cases = _score_inspect_cases(cand_text)

    for cname, passed in cand_cases.items():
        if base_cases.get(cname) and not passed:
            regressions.append(f"regression:{cname}")
        elif not passed:
            regressions.append(f"failed_case:{cname}")

    base_score = sum(1 for v in base_cases.values() if v)
    cand_score = sum(1 for v in cand_cases.values() if v)
    passed = not errors and not regressions and cand_score >= base_score and cand_score == len(cand_cases)
    return {
        "passed": passed,
        "coverage_only": True,
        "case_id": case_id,
        "experiment_ref": experiment_ref,
        "baseline_sha256": baseline_sha256,
        "candidate_sha256": candidate_sha256,
        "baseline_score": base_score,
        "candidate_score": cand_score,
        "total_cases": len(cand_cases),
        "baseline_cases": base_cases,
        "candidate_cases": cand_cases,
        "regressions": regressions,
        "errors": errors,
    }


def _load_release_module():
    spec = importlib.util.spec_from_file_location("flow_skills_release", HERE / "release.py")
    if spec is None or spec.loader is None:
        raise ImportError("Cannot load flow/release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _resolve_receipt_json(source_root: Path, ref: str) -> dict[str, Any] | None:
    cleaned = str(ref or "").strip()
    if not cleaned:
        return None
    candidates = [Path(cleaned)]
    if not Path(cleaned).is_absolute():
        candidates.insert(0, source_root / cleaned)
    for cand in candidates:
        if cand.is_file():
            try:
                obj = json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            return obj if isinstance(obj, dict) else None
    return None


def _verify_candidate_evidence(
    source_root: Path,
    *,
    rel_path: str,
    case_id: str,
    baseline_sha256: str,
    candidate_sha256: str,
    experiment_ref: str,
    review_ref: str,
    store: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    exp_clean = str(experiment_ref or "").strip()
    rev_clean = str(review_ref or "").strip()
    if not exp_clean or exp_clean.lower() in ("pending", "todo", "tbd", "none", "model_opinion_only") or "model_opinion_only" in exp_clean.lower():
        raise ValueError("Candidate adoption requires a verifiable project-owned experiment_ref")
    if not rev_clean or rev_clean.lower() in ("pending", "todo", "tbd", "none", "model_opinion_only") or "model_opinion_only" in rev_clean.lower():
        raise ValueError("Candidate adoption requires a verifiable independent review_ref (never model opinion alone)")

    exp_verified: dict[str, Any] | None = None
    if store is not None:
        for outcome in store.list_outcomes():
            if outcome.external_experiment_ref != exp_clean or outcome.status != "accepted":
                continue
            if not str(outcome.evaluator or "").strip() or "model_opinion_only" in outcome.evaluator.lower():
                continue
            combined = f"{outcome.baseline_summary}\n{outcome.outcome_summary}\n{outcome.feedback_note or ''}"
            if (
                rel_path in combined
                and case_id in combined
                and baseline_sha256 in combined
                and candidate_sha256 in combined
            ):
                exp_verified = {
                    "source": "learner_store",
                    "outcome_id": outcome.outcome_id,
                    "experiment_ref": exp_clean,
                    "evaluator": outcome.evaluator,
                }
                break

    if exp_verified is None:
        exp_obj = _resolve_receipt_json(source_root, exp_clean)
        if exp_obj is not None:
            exp_ok = exp_obj.get("status") == "accepted" or exp_obj.get("passed") is True
            evaluator = str(exp_obj.get("evaluator") or "").strip()
            if (
                exp_ok
                and evaluator
                and "model_opinion_only" not in evaluator.lower()
                and exp_obj.get("rel_path") == rel_path
                and exp_obj.get("case_id") == case_id
                and exp_obj.get("baseline_sha256") == baseline_sha256
                and exp_obj.get("candidate_sha256") == candidate_sha256
            ):
                exp_verified = {
                    "source": "receipt_file",
                    "experiment_ref": exp_clean,
                    "evaluator": evaluator,
                }

    if exp_verified is None:
        raise ValueError(
            "Candidate adoption refused: experiment_ref must match an accepted OutcomeReference or structured experiment receipt "
            f"bound to rel_path={rel_path!r}, case_id={case_id!r}, baseline_sha256={baseline_sha256[:12]}, candidate_sha256={candidate_sha256[:12]}"
        )

    rev_obj = _resolve_receipt_json(source_root, rev_clean)
    if rev_obj is None:
        raise ValueError(
            "Candidate adoption refused: review_ref must resolve to a passing independent review verdict JSON receipt"
        )
    rev_verdict = str(rev_obj.get("verdict") or "").lower()
    rev_ok = rev_verdict == "pass" or rev_obj.get("passed") is True
    findings = rev_obj.get("findings") or []
    has_blocking = any(
        isinstance(f, dict)
        and (
            f.get("severity") == "high"
            or f.get("action") == "required"
            or f.get("blocking") in ("stage", "delivery")
        )
        for f in findings
    )
    reviewer = str(rev_obj.get("reviewer") or rev_obj.get("role") or "independent_review").strip().lower()
    if (
        not rev_ok
        or has_blocking
        or "model_opinion_only" in reviewer
        or reviewer == "self"
        or rev_obj.get("rel_path") != rel_path
        or rev_obj.get("candidate_sha256") != candidate_sha256
        or (rev_obj.get("baseline_sha256") is not None and rev_obj.get("baseline_sha256") != baseline_sha256)
        or (rev_obj.get("case_id") is not None and rev_obj.get("case_id") != case_id)
    ):
        raise ValueError(
            "Candidate adoption refused: independent review receipt does not pass for exact rel_path/case_id/baseline/candidate hashes"
        )

    rev_verified = {
        "source": "review_receipt",
        "review_ref": rev_clean,
        "verdict": rev_verdict or "pass",
        "reviewer": reviewer,
    }
    return exp_verified, rev_verified


def activate_governed_skill_candidate(
    source_root: Path,
    home: Path,
    version: str,
    *,
    rel_path: str,
    candidate_bytes: bytes,
    experiment_ref: str,
    review_ref: str,
    case_id: str = "SKC-FLOW-INSPECT-01",
    post_check: Callable[[Path], list[str]] | None = None,
    store: Any | None = None,
    baseline_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Verify candidate-bound evidence, stage, activate, and rollback on regression through existing flow/release.py."""
    release = _load_release_module()
    before = release.inspect_install(home)
    installed_rel = rel_path.removeprefix("skills/")
    installed_path = home / ".agents" / "skills" / installed_rel
    target_file = source_root / rel_path
    prev_source_bytes = target_file.read_bytes() if target_file.is_file() else None
    eff_baseline_bytes = (
        baseline_bytes
        if baseline_bytes is not None
        else (installed_path.read_bytes() if installed_path.is_file() else (prev_source_bytes or b""))
    )
    baseline_sha256 = _sha256_bytes(eff_baseline_bytes)
    candidate_sha256 = _sha256_bytes(candidate_bytes)

    exp_verified, rev_verified = _verify_candidate_evidence(
        source_root,
        rel_path=rel_path,
        case_id=case_id,
        baseline_sha256=baseline_sha256,
        candidate_sha256=candidate_sha256,
        experiment_ref=experiment_ref,
        review_ref=review_ref,
        store=store,
    )

    eval_res = evaluate_skill_candidate(
        source_root,
        rel_path=rel_path,
        candidate_bytes=candidate_bytes,
        baseline_bytes=eff_baseline_bytes,
        case_id=case_id,
        experiment_ref=experiment_ref,
    )
    if not eval_res["passed"]:
        return {
            "activated": False,
            "rolled_back": False,
            "reason": "candidate_evaluation_failed",
            "baseline_sha256": baseline_sha256,
            "candidate_sha256": candidate_sha256,
            "evaluation": eval_res,
            "experiment_ref": experiment_ref,
            "review_ref": review_ref,
        }

    is_git_repo = (source_root / ".git").exists()
    if is_git_repo:
        git_st = subprocess.run(
            ["git", "-C", str(source_root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=False,
        )
        if git_st.returncode != 0 or git_st.stdout.strip():
            raise ValueError("Git source_root must have a clean working tree before candidate activation")
        if prev_source_bytes != candidate_bytes:
            raise ValueError("Git source_root candidate file must already match committed reviewed candidate_bytes")

    references = {
        "experiment_ref": experiment_ref,
        "review_ref": review_ref,
        "rel_path": rel_path,
        "case_id": case_id,
        "baseline_sha256": baseline_sha256,
        "candidate_sha256": candidate_sha256,
        "experiment_verification": exp_verified,
        "review_verification": rev_verified,
    }

    wrote_temp_source = False
    if not is_git_repo and prev_source_bytes != candidate_bytes:
        target_file.write_bytes(candidate_bytes)
        wrote_temp_source = True
    staged_ok = False
    try:
        release.stage(
            source_root,
            home,
            version,
            release.digest(before),
            references=references,
        )
        staged_ok = True
        release.activate(home, version)
        post_eval = evaluate_skill_candidate(
            source_root,
            rel_path=rel_path,
            candidate_bytes=installed_path.read_bytes(),
            baseline_bytes=eff_baseline_bytes,
            case_id=case_id,
            experiment_ref=experiment_ref,
        )
        post_errors = list(post_eval["errors"]) + list(post_eval["regressions"])
        if post_check is not None:
            post_errors.extend(post_check(home))
        if post_errors:
            release.rollback(home, version)
            return {
                "activated": False,
                "rolled_back": True,
                "reason": "post_activation_regression",
                "errors": post_errors,
                "baseline_sha256": baseline_sha256,
                "candidate_sha256": candidate_sha256,
                "evaluation": eval_res,
                "post_evaluation": post_eval,
                "experiment_ref": experiment_ref,
                "review_ref": review_ref,
            }
        return {
            "activated": True,
            "rolled_back": False,
            "version": version,
            "baseline_sha256": baseline_sha256,
            "candidate_sha256": candidate_sha256,
            "evaluation": eval_res,
            "post_evaluation": post_eval,
            "experiment_ref": experiment_ref,
            "review_ref": review_ref,
            "references": references,
        }
    except Exception as exc:
        if staged_ok:
            try:
                release.rollback(home, version)
            except Exception:
                pass
        raise exc
    finally:
        if wrote_temp_source and prev_source_bytes is not None:
            target_file.write_bytes(prev_source_bytes)


def record_governed_skill_outcome(
    store: Any,
    *,
    project_id: str,
    assessment_id: str,
    status: str,
    experiment_ref: str,
    release_ref: str | None,
    baseline_summary: str,
    outcome_summary: str,
    evaluator: str,
    feedback_note: str | None = None,
) -> dict[str, Any]:
    """Record skill experiment outcome in learner and propagate negative/inconclusive feedback to matching."""
    from learner.assessment import apply_feedback_preference

    outcome = store.record_outcome_reference(
        project_id=project_id,
        assessment_id=assessment_id,
        external_experiment_ref=experiment_ref,
        external_release_ref=release_ref,
        status=status,
        baseline_summary=baseline_summary,
        outcome_summary=outcome_summary,
        evaluator=evaluator,
        feedback_note=feedback_note,
    )
    pref_result = None
    if status in ("rejected", "inconclusive") and (feedback_note or outcome_summary):
        note_text = feedback_note or f"less of {outcome_summary}"
        pref_result = apply_feedback_preference(store, project_id, note_text)
    return {"outcome": outcome.to_dict(), "preference_update": pref_result}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="flow skills", description="Derived skill metadata index and selection")
    sub = ap.add_subparsers(dest="subcmd", required=True)
    p_idx = sub.add_parser("index", help="Print derived metadata index from SKILL.md files")
    p_idx.add_argument("--skills-root")

    p_sel = sub.add_parser("select", help="Select relevant skills from metadata by request and constraints")
    p_sel.add_argument("request")
    p_sel.add_argument("--skills-root")
    p_sel.add_argument("--repo")
    p_sel.add_argument("--task")
    p_sel.add_argument("--task-type")
    p_sel.add_argument("--paused", action="store_true")
    p_sel.add_argument("--read-only", action="store_true")
    p_sel.add_argument("--load", action="store_true")
    p_sel.add_argument("--ref", action="append", default=[])

    p_pin = sub.add_parser("read-pin", help="Read immutable pinned skill or policy snapshot for an active task")
    p_pin.add_argument("--repo", default=".")
    p_pin.add_argument("--task")
    p_pin.add_argument("--rel-path", default="SKILL.md")
    p_pin.add_argument("--policy", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.subcmd == "index":
            root = Path(args.skills_root).resolve() if args.skills_root else None
            print(json.dumps(build_skill_index(root), indent=2, sort_keys=True))
        elif args.subcmd == "read-pin":
            repo_path = Path(args.repo).resolve()
            eff_task = resolve_active_task_for_pin(repo_path, task=args.task)
            if not eff_task:
                raise ValueError("No active IN_PROGRESS task found; pass --task explicitly")
            if args.policy:
                pin = load_task_pin(repo_path, eff_task)
                if pin is None:
                    raise FileNotFoundError(f"No task pin recorded for {eff_task!r}")
                pol_json = (pin.get("model_policy") or {}).get("policy_json")
                if not isinstance(pol_json, str):
                    raise ValueError(f"Pinned policy snapshot missing for {eff_task!r}")
                sys.stdout.write(pol_json)
            else:
                sys.stdout.write(read_pinned_skill(repo_path, eff_task, args.rel_path).decode("utf-8"))
        else:
            root = Path(args.skills_root).resolve() if args.skills_root else None
            repo_path = Path(args.repo).resolve() if args.repo else None
            res = select_skills(
                args.request,
                root,
                repo=repo_path,
                task=args.task,
                task_type=args.task_type,
                constraints={"paused": args.paused, "read_only": args.read_only},
                load_instructions=args.load,
                load_references=args.ref,
            )
            print(json.dumps(res, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"flow skills: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
