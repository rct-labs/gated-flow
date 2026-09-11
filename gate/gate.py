#!/usr/bin/env python3
"""
gate.py — the acceptance gate that a lying commit cannot pass.

Closes one specific hole observed in an earlier unattended orchestrator:
a commit that flips a task's status to DONE while touching only the queue
and handoff documents. Measured fake-completion rate before the fix: ~60%.

Design rules (see docs/design.md):
  - The verdict comes from a command's exit code, never from a model's claim.
  - The structural check is derived from the diff itself, so it cannot be
    forged: there is no field to lie in.
  - Everything else is auditable after the fact with one command.

Stdlib only. Python 3.10+. Windows-safe (no python3 assumption, UTF-8 I/O).

Subcommands
  init          write .gate/config.json into a repo, with sane defaults
  install-hook  install .git/hooks/pre-commit that calls `check-commit`
  check-commit  fast structural gate, run by the pre-commit hook
  verify        slow gate: run the acceptance command, record a verdict
  audit         re-derive the structural verdict over the last N commits
  admit         sweet-spot admission report for every TODO task in the queue
  recover-task  revalidate a stopped claim's implementation, return its row to
                TODO, and record the receipt Gate 1 needs (docs/recovery.md)
  review-task   independently review a recovered task with persistent budgets
  usage         probe every worker / judge CLI now; bench the quota-dead ones
  doctor        report what is configured and whether it is usable
  run           unattended loop: probe → dispatch → verify → judge → revise
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import execution
import recovery

GATE_DIR = ".gate"
CONFIG_NAME = "config.json"
VERDICT_NAME = "verdict.json"

# Windows consoles default to a legacy code page; without this the report is
# mojibake and a blocked commit's reason becomes unreadable.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DEFAULT_CONFIG = {
    # Repo-relative directory this project owns. Empty means the whole repo.
    # When set, a commit that touches the queue may not touch anything outside
    # it — the "don't drag my other projects into this history" gate.
    "project_prefix": "",
    "queue_file": "TASK_QUEUE.md",
    "context_file": "CONTEXT.md",
    "external_context": False,
    "handoff_globs": [],
    # A commit that only touches these is documentation. Flipping a task to
    # DONE while touching nothing else is the fake-completion pattern.
    "doc_only_globs": [
        "TASK_QUEUE.md",
        "NEXT_SESSION.md",
        "NEXT_SESSION.*.archive.md",
        "SESSION.md",
        "docs/handoff/*",
    ],
    # The acceptance oracle. Must exit non-zero when the requirement is violated.
    "verify_cmd": "uv run pytest tests -q",
    "verify_timeout_s": 1800,
    # How the oracle reports its count, for the non-vacuity check.
    "count_regex": r"(\d+)\s+passed",
    # Paths whose git diff must be empty during a refactor package.
    # Empty list disables the check.
    "frozen_globs": [],
    # A verdict older than this is not accepted by check-commit.
    "verdict_max_age_s": 5400,
    # Set false to let check-commit warn instead of block on a missing verdict.
    "require_verdict_on_done": True,
    "done_markers": ["DONE"],
    "todo_markers": ["TODO"],
    "blocked_markers": ["BLOCKED"],
    "in_progress_markers": ["IN_PROGRESS"],
    # ---- unattended run -------------------------------------------------
    # Writers are tried in order; a quota-dead one is skipped for a cooldown.
    "workers": ["codex", "claude", "grok"],
    # Filled while queueing, never inferred from a later host session.
    "execution": {"defaults": {"workers": {}, "judges": {}}},
    # What one worker is asked to do. It should execute exactly one task and
    # stop — the loop belongs to this script, not to the model.
    "worker_prompt": (
        "Invoke the {skill} skill. Execute exactly task {task} from the queue "
        "— claim {task}, implement it, verify it, commit it — then stop. "
        "Never return while a test, verification, build, or child process is "
        "still running. Run acceptance commands in the foreground with an "
        "adequate timeout; if any command is backgrounded, wait for it and "
        "check its exit code before responding. "
        "Never select a different TODO task, even if {task} is no longer TODO. "
        "Do not start a second task. Do not push. If {task} is BLOCKED, stop "
        "immediately and report why instead of guessing. Tool, sandbox, Git, "
        "quota, and filesystem failures are environment failures, not task "
        "blockers: do not mark the task BLOCKED for them."
    ),
    "worker_skill": "",
    "max_tasks": 3,
    "max_attempts_per_task": 2,
    "task_timeout_s": 5400,
    "run_timeout_s": 28800,
    "quota_cooldown_s": 21600,
    # Shell command run on every milestone (task start / task end / run end).
    # {title} and {body} are filled in. Empty disables notification.
    "notify_cmd": "",
    # How often a still-running worker writes a heartbeat, so a long task and a
    # hung one look different from the outside.
    "heartbeat_s": 300,
    # Opt-in, explicit queue groups only. Checkpoints never survive a run.
    "session_reuse": {
        "enabled": False,
        "max_tasks": 3,
        "max_input_tokens": 1000000,
        "context_files": ["AGENTS.md", "CLAUDE.md"],
    },
    # ---- admission ------------------------------------------------------
    # The third leg of the design: exit-code verdicts and the diff
    # guard catch a bad attempt, admission keeps out-of-sweet-spot tasks
    # (small / isolated / implicitly verifiable) out of the loop entirely.
    # Scope is declared per task in the queue file, on its own line:
    #   <!-- task:WP-7 files: src/a.py, tests/test_a.py -->
    "max_task_files": 6,
    # A queue row matching this is a decision, not a task; it needs a human.
    # The first three alternatives are the Chinese words for design / decide /
    # review, written as escapes so the source stays ASCII.
    "human_decision_regex": "\u8bbe\u8ba1|\u51b3\u5b9a|\u8bc4\u5ba1|design|decide|choose",
    # False: a refused head task is a warning. True: it stops the run.
    # Flip to true only after one real project's advisory report has been
    # human-checked for false refusals.
    "strict_admit": False,
    # ---- autonomy (docs/autonomy.md) ---------------------------------------
    # Probe every candidate CLI once per run, before the first dispatch, with a
    # tiny real request. A quota-shaped answer benches it for quota_cooldown_s;
    # a probe that never reached the provider (timeout, not installed, other
    # error) benches it for retry_s only — it must not occupy the quota window.
    "probe": {"enabled": True, "timeout_s": 90, "retry_s": 600},
    # Judge every closed task with a read-only headless model, then iterate at
    # most max_revisions times (initial + 2 = 3 rounds: the point where the
    # per-round gain of self-correction drops under 2%). The judge never edits
    # the queue: DONE is earned by the exit-code gate and stays.
    "judge": {
        "enabled": False,
        "chain": ["fable", "opus", "codex"],
        "pass_score": 85,
        "max_revisions": 2,
        "min_gain": 5,
        "timeout_s": 1200,
    },
    "judge_prompt": (
        "You are the acceptance judge for ONE finished task in this repository. "
        "Task {task} was just closed by commit {commit}. Declared scope: {files}. "
        "Commits of this task since {base}: {commits}. Last acceptance run tail:\n"
        "{verify_tail}\n\n"
        "Read CONTEXT.md and TASK_QUEUE.md (repo root); locate the work package under "
        "docs/work/ whose PLAN.md or spec.md names {task} and read its acceptance for "
        "this task. Inspect the diff (`git show <sha>`) and the tests it added or "
        "changed. Do not modify anything.\n\n"
        "Score 0-100 with this rubric: acceptance criteria met 40; tests are real and "
        "would fail without the change 20; scope discipline (only declared files, no "
        "unrelated edits, queue row untouched apart from status/counts) 15; project "
        "conventions and code quality 15; regression risk 10.\n"
        "verdict: 'pass' when score >= {pass_score} and no high-severity finding; "
        "'revise' when the shortfall is fixable inside the declared scope by one worker "
        "session; 'escalate' when the spec is ambiguous, the fix needs a human decision, "
        "or the scope must grow. findings: concrete, file-anchored, each with a fix. "
        "revision_brief: the exact instructions a worker needs, or empty on pass.\n"
        "Return ONLY JSON matching the schema."
    ),
    "revision_prompt": (
        "You are revising ONE task in this repository after an acceptance review. "
        "Task {task} is already DONE in the queue and must stay DONE: do not edit its "
        "queue row or any other row. Fix exactly the findings below, inside the declared "
        "scope {files}; do not widen scope, do not weaken or delete tests. Run the "
        "project's acceptance command in the foreground and make it pass, then commit "
        "only the paths you changed (never `git add -A`, never `--no-verify`, never "
        "push). If a finding cannot be fixed within scope, leave it and say so.\n\n"
        "Reviewer brief: {brief}\n\nFindings:\n{findings}\n"
    ),
    # Declared files matching these are irreversible for this project: the
    # task needs an explicit `<!-- task:ID approved: <who/date> -->` line or the
    # run stops with needs_approval, even in advisory admission mode.
    "irreversible_globs": [
        "drizzle/**",
        "data/**",
        ".git/hooks/**",
        ".claude/settings*",
        "scripts/apply-*",
    ],
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "verdict": {"type": "string", "enum": ["pass", "revise", "escalate"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "file": {"type": "string"},
                    "issue": {"type": "string"},
                    "fix": {"type": "string"},
                },
                # Codex strict structured output requires every property here.
                # Empty strings represent findings without a location or fix.
                "required": ["severity", "file", "issue", "fix"],
                "additionalProperties": False,
            },
        },
        "revision_brief": {"type": "string"},
    },
    "required": ["score", "verdict", "findings", "revision_brief"],
    "additionalProperties": False,
}

# Read-only judge contracts with explicit model pins, independent of workers.
# {schema} is the inline JSON schema,
# {schema_file} / {out_file} are paths for CLIs that take files.
JUDGE_CMDS: dict[str, list[str]] = {
    "fable": [
        "claude", "-p", "{prompt}",
        "--model", "claude-fable-5-1",
        "--output-format", "json", "--json-schema", "{schema}",
        "--allowedTools",
        "Read,Grep,Glob,Bash(git diff:*),Bash(git show:*),Bash(git log:*),Bash(git status:*)",
        "--max-budget-usd", "5",
    ],
    "opus": [
        "claude", "-p", "{prompt}",
        "--model", "opus",
        "--output-format", "json", "--json-schema", "{schema}",
        "--allowedTools",
        "Read,Grep,Glob,Bash(git diff:*),Bash(git show:*),Bash(git log:*),Bash(git status:*)",
        "--max-budget-usd", "5",
    ],
    "codex": [
        "codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
        "-C", "{projdir}", "--output-schema", "{schema_file}", "-o", "{out_file}", "-",
    ],
}

# Which installed CLI a judge chain member runs on, for probing.
JUDGE_TOOL: dict[str, str] = {"fable": "claude", "opus": "claude", "codex": "codex"}

# A worker that cannot be flag-restricted to read-only is still safe to use as
# a writer here: every commit it makes passes through the pre-commit gate, and
# the scope gate keeps it inside the project prefix.
WORKER_CMDS: dict[str, list[str]] = {
    # The gate contract requires every worker to make queue-only claim and
    # completion commits. Codex workspace-write deliberately protects .git as
    # read-only, so it cannot satisfy that contract. The hook and scope gate
    # remain the enforcement boundary; Codex therefore needs full filesystem
    # access here, just like the other writer CLIs.
    "codex": [
        "codex", "exec", "--sandbox", "danger-full-access",
        "--skip-git-repo-check", "-C", "{projdir}", "-",
    ],
    # Inherit the effective CLI model unless the project explicitly pins one.
    "claude": [
        "claude", "-p", "{prompt}",
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Read,Edit,Write,Bash,Glob,Grep,TaskOutput",
        "--output-format", "text",
    ],
    "grok": [
        "grok", "--prompt-file", "{prompt_file}",
        "--permission-mode", "auto", "--max-turns", "120",
        "--output-format", "plain", "--cwd", "{projdir}",
    ],
    "kimi": ["kimi", "-p", "{prompt}", "--output-format", "text"],
}

# Signatures that mean "this tool is unusable right now", not "this task
# failed". Both classes get the same treatment — bench the tool, do not spend
# the task's attempt budget, fall through to the next worker — because both are
# outages of the tool rather than verdicts on the work.
#
# The overload half was added after a real run burned both attempts on
# `API Error: 529 Overloaded.` and reported not_done for work that was in
# fact complete. A server-side 5xx is not a rejected commit.
# Patterns are matched case-insensitively as substrings, so they are kept
# distinctive: a bare "529" would false-match an incidental test count.
QUOTA_PATTERNS = [
    "usage limit",
    "billing cycle",
    "quota",
    "rate_limit",
    "rate limit",
    "429",
    "insufficient_quota",
    # prepaid balance / billing exhaustion: a 402 is the same outage as a 429,
    # not a rejected commit. Observed once: grok returned
    # `API error (status 402 Payment Required): Grok Build usage balance
    # exhausted`, matched nothing here, burned a task's second attempt and
    # ended the run as not_done. A bare "402" would false-match a test count.
    "status 402",
    "error: 402",
    "error 402",
    "402 payment required",
    "balance exhausted",
    "insufficient balance",
    "insufficient credit",
    "credit balance is too low",
    "out of credits",
    # transient server-side outages
    "overloaded",
    "error: 529",
    "error 529",
    "status 529",
    "error: 503",
    "error 503",
    "service unavailable",
    "internal server error",
    "bad gateway",
    "gateway timeout",
]

# Codex can exit zero even when a model-issued Git command failed. Match the
# actual tool transcript, not only the CLI process return code. Deliberately do
# not match "File exists": a stale index.lock is repository state and must be
# inspected, never deleted automatically.
GIT_WRITE_DENIAL_RE = re.compile(
    r"fatal:\s+unable to create ['\"][^'\"]*[/\\]\.git[/\\]index\.lock['\"]:\s*"
    r"(?:permission denied|access is denied|read-only file system)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- utilities


def die(msg: str, code: int = 1) -> None:
    print(f"GATE: {msg}", file=sys.stderr)
    raise SystemExit(code)


def ok(msg: str) -> None:
    print(f"GATE PASS: {msg}")


def fail(msg: str, detail: str = "") -> None:
    print(f"GATE BLOCK: {msg}", file=sys.stderr)
    if detail:
        print(detail, file=sys.stderr)
    raise SystemExit(1)


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        die(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(start: Path) -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(start),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if out.returncode != 0:
        die(f"{start} is not inside a git repository")
    return Path(out.stdout.strip())


def load_config(repo: Path) -> dict:
    # GATE_CONFIG lets you point the gate at a repo without writing into it —
    # useful for auditing a project before you decide to install the hook.
    env = os.environ.get("GATE_CONFIG")
    path = Path(env) if env else repo / GATE_DIR / CONFIG_NAME
    if not path.exists():
        die(f"no config at {path}. Run: gate.py init --repo {repo}")
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


def continuous_quality_script() -> Path | None:
    """Locate the optional provider-neutral continuous-quality runner."""
    explicit = os.environ.get("CONTINUOUS_QUALITY_SCRIPT")
    candidates = [Path(explicit)] if explicit else []
    home = Path.home()
    candidates.extend(
        [
            home / ".agents" / "skills" / "continuous-quality" / "scripts" / "continuous_quality.py",
            home / ".codex" / "skills" / "continuous-quality" / "scripts" / "continuous_quality.py",
            home / ".claude" / "skills" / "continuous-quality" / "scripts" / "continuous_quality.py",
            home / ".grok" / "skills" / "continuous-quality" / "scripts" / "continuous_quality.py",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def quality_phase(repo: Path, cfg: dict, phase: str) -> dict | None:
    """Run the optional quality hook and return only process-level evidence.

    The shared runner owns policy and project semantics. Gate knows the phase,
    command exit code, and a bounded output tail only.
    """
    quality = cfg.get("quality")
    if quality is None:
        return None
    if not isinstance(quality, dict):
        return {
            "result": "FAIL",
            "exit_code": 1,
            "tail": "quality must be an object",
        }
    if quality.get("enabled") is False:
        return None
    script = continuous_quality_script()
    if script is None:
        return {
            "result": "FAIL",
            "exit_code": 127,
            "tail": "continuous-quality runner not found under a shared skill directory",
        }
    proc = subprocess.run(
        [sys.executable, str(script), "check", "--repo", str(repo), "--phase", phase],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "result": "PASS" if proc.returncode == 0 else "FAIL",
        "exit_code": proc.returncode,
        "tail": "\n".join(output.strip().splitlines()[-20:])[-4000:],
    }


def matches_any(path: str, globs: list[str]) -> bool:
    norm = path.replace("\\", "/")
    return any(fnmatch.fnmatch(norm, g) for g in globs)


def prefixed(cfg: dict, rel: str) -> str:
    """Resolve a project-relative path to a repo-relative one."""
    pre = (cfg.get("project_prefix") or "").strip("/")
    return f"{pre}/{rel}" if pre else rel


def in_project(cfg: dict, path: str) -> bool:
    pre = (cfg.get("project_prefix") or "").strip("/")
    if not pre:
        return True
    return path.replace("\\", "/").startswith(pre + "/")


def project_rel(cfg: dict, path: str) -> str:
    pre = (cfg.get("project_prefix") or "").strip("/")
    norm = path.replace("\\", "/")
    return norm[len(pre) + 1 :] if pre and norm.startswith(pre + "/") else norm


# ------------------------------------------------------------ queue parsing

# | 5 | WP-3a | name | `DONE` | **484 passed** | **507 passed**（...） | ... |
ROW_RE = re.compile(r"^\s*\|\s*\d+\s*\|\s*([^|]+?)\s*\|.*$")
STATUS_RE = re.compile(r"`([A-Z_]+)`")


def parse_queue(text: str) -> dict[str, dict]:
    """Map task id -> {status, cells}. Tolerates prose around the table."""
    tasks: dict[str, dict] = {}
    for line in text.splitlines():
        m = ROW_RE.match(line)
        if not m:
            continue
        task_id = m.group(1).strip().strip("*` ")
        if not task_id or task_id in ("ID", "---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        status = ""
        for cell in cells:
            sm = STATUS_RE.search(cell)
            if sm:
                status = sm.group(1)
                break
        if not status:
            continue
        tasks[task_id] = {"status": status, "cells": cells}
    return tasks


# <!-- task:WP-7 files: src/foo.py, tests/test_foo.py -->
TASK_FILES_RE = re.compile(r"<!--\s*task:(\S+)\s+files:\s*(.*?)\s*-->")


def parse_task_files(text: str) -> dict[str, list[str]]:
    """Map task id -> declared files, from scope-declaration comment lines."""
    out: dict[str, list[str]] = {}
    for m in TASK_FILES_RE.finditer(text):
        out[m.group(1)] = [f.strip() for f in m.group(2).split(",") if f.strip()]
    return out


# <!-- task:WP-8 after: WP-7 -->
TASK_AFTER_RE = re.compile(r"<!--\s*task:(\S+)\s+after:\s*(.*?)\s*-->")


def parse_task_after(text: str) -> dict[str, list[str]]:
    """Map task id -> ids it declares it runs after. A declared order lets two
    strictly sequential slices share a file (the second extends what the first
    created) without tripping the overlap refusal, which exists to stop
    *independent* tasks from stepping on each other. Added 2026-09-02."""
    out: dict[str, list[str]] = {}
    for m in TASK_AFTER_RE.finditer(text):
        out[m.group(1)] = [f.strip() for f in m.group(2).split(",") if f.strip()]
    return out


# <!-- task:WP-9 approved: 2026-09-03 user -->
TASK_APPROVED_RE = re.compile(r"<!--\s*task:(\S+)\s+approved:\s*(.*?)\s*-->")


def parse_task_approved(text: str) -> dict[str, str]:
    """Map task id -> approval note. An irreversible task (declared files match
    irreversible_globs) needs one of these before the runner will dispatch it.
    Per task id, so one approval never carries over to the next task."""
    out: dict[str, str] = {}
    for m in TASK_APPROVED_RE.finditer(text):
        note = m.group(2).strip()
        if note:
            out[m.group(1)] = note
    return out


def sub_cfg(cfg: dict, key: str) -> dict:
    """Nested config block with defaults filled in. load_config() replaces a
    top-level key wholesale, so a project that sets only judge.enabled must
    still get the default chain and thresholds."""
    merged = dict(DEFAULT_CONFIG.get(key) or {})
    value = cfg.get(key)
    if isinstance(value, dict):
        merged.update(value)
    return merged


def irreversible_files(cfg: dict, files: list[str]) -> list[str]:
    globs = cfg.get("irreversible_globs") or []
    return [f for f in files if matches_any(f, globs)]


WORKER_COLUMN = "worker"
NO_PIN_CELLS = {"", "-", "—", "–"}


def _is_separator_row(cells: list[str]) -> bool:
    return all(set(c) <= set("-: ") for c in cells)


def parse_task_workers(text: str) -> dict[str, str]:
    """Map task id -> pinned worker id, from an optional `worker` column.

    The column index is per table, not per file: a header row that names the
    column sets it, a header row without it clears it, so the many older tables
    in a long queue file stay unpinned. An empty / dash cell means no pin.
    Added 2026-09-02 so one run can route each task to the CLI that suits it
    (docs to kimi, mechanical implementation to codex, cross-package design to
    claude) instead of one run per worker."""
    out: dict[str, str] = {}
    idx: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if _is_separator_row(cells):
            continue
        m = ROW_RE.match(line)
        if not m:
            # A header row. It either declares the column or resets it.
            lowered = [c.lower().strip("*` ") for c in cells]
            idx = lowered.index(WORKER_COLUMN) if WORKER_COLUMN in lowered else None
            continue
        if idx is None or idx >= len(cells):
            continue
        task_id = m.group(1).strip().strip("*` ")
        value = cells[idx].strip("*` ").lower()
        if not task_id or value in NO_PIN_CELLS:
            continue
        out[task_id] = value
    return out


def known_workers(cfg: dict) -> set[str]:
    return set(WORKER_CMDS) | set(cfg.get("worker_cmds") or {})


def blob(repo: Path, ref: str, path: str) -> str | None:
    """Content of `path` at `ref`. Use ':' for the staged (index) version."""
    spec = f":{path}" if ref == ":" else f"{ref}:{path}"
    proc = subprocess.run(
        ["git", "show", spec],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def newly_done(repo: Path, cfg: dict, before_ref: str, after_ref: str) -> list[str]:
    """Task ids whose status became a done-marker between two refs."""
    qf = prefixed(cfg, cfg["queue_file"])
    before = blob(repo, before_ref, qf)
    after = blob(repo, after_ref, qf)
    if after is None:
        return []
    old = parse_queue(before) if before else {}
    new = parse_queue(after)
    markers = set(cfg["done_markers"])
    flipped = []
    for tid, row in new.items():
        was = old.get(tid, {}).get("status", "")
        if row["status"] in markers and was not in markers:
            flipped.append(tid)
    return flipped


def end_baseline_for(repo: Path, cfg: dict, ref: str, task_id: str) -> int | None:
    text = blob(repo, ref, prefixed(cfg, cfg["queue_file"]))
    if not text:
        return None
    row = parse_queue(text).get(task_id)
    if not row:
        return None
    # The end baseline is the last cell containing a "<n> passed"-shaped number.
    found = None
    for cell in row["cells"]:
        m = re.search(cfg["count_regex"], cell)
        if m:
            found = int(m.group(1))
    return found


# ------------------------------------------------------------ verdict store


def verdict_path(repo: Path) -> Path:
    return repo / GATE_DIR / VERDICT_NAME


def write_verdict(repo: Path, payload: dict) -> None:
    p = verdict_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    os.replace(tmp, p)


def read_verdict(repo: Path) -> dict | None:
    p = verdict_path(repo)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def worktree_fingerprint(repo: Path) -> str:
    """Hash of the staged+tracked tree, so a verdict cannot outlive its code."""
    out = git(repo, "status", "--porcelain=v1", "-uall")
    head = git(repo, "rev-parse", "HEAD", check=False).strip()
    import hashlib

    h = hashlib.sha256()
    h.update(head.encode())
    h.update(out.encode())
    return h.hexdigest()[:16]


# -------------------------------------------------------------- subcommands


def cmd_init(args) -> None:
    repo = repo_root(Path(args.repo).resolve())
    d = repo / GATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    cfgp = d / CONFIG_NAME
    if cfgp.exists() and not args.force:
        die(f"{cfgp} exists. Use --force to overwrite.")
    cfgp.write_text(
        json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    gi = d / ".gitignore"
    gi.write_text("verdict.json\n*.tmp\n", encoding="utf-8")
    print(f"wrote {cfgp}")
    print(f"wrote {gi}  (the verdict is local evidence, not repo content)")
    print("Review verify_cmd and frozen_globs before installing the hook.")


# The gate needs only the standard library, so it deliberately runs on a plain
# interpreter rather than the project's uv-managed venv — a venv that is being
# rebuilt must never be able to block a commit. Interpreters are tried in order;
# if none exists the hook warns and allows the commit, because a missing
# interpreter is not evidence of a fake completion, and bricking every commit in
# a repo that hosts several projects is the worse failure. `gate.py audit` still
# sees anything that slipped through.
HOOK_TEMPLATE = """#!/bin/sh
# installed by gate.py — blocks the fake-completion pattern.
# Bypass in an emergency with: git commit --no-verify   (and say so in the message)
GATE="{gate}"
for PY in "{python}" python py python3; do
    if [ "$PY" = "py" ]; then
        command -v py >/dev/null 2>&1 && exec py -3 "$GATE" check-commit --repo "{repo}"
    elif command -v "$PY" >/dev/null 2>&1 || [ -x "$PY" ]; then
        exec "$PY" "$GATE" check-commit --repo "{repo}"
    fi
done
echo "GATE WARNING: no Python interpreter found; the acceptance gate did not run." >&2
echo "GATE WARNING: re-check this commit later with: gate.py audit" >&2
exit 0
"""


def cmd_install_hook(args) -> None:
    repo = repo_root(Path(args.repo).resolve())
    hooks = Path(git(repo, "rev-parse", "--git-path", "hooks").strip())
    if not hooks.is_absolute():
        hooks = repo / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    if hook.exists() and not args.force:
        existing = hook.read_text(encoding="utf-8", errors="replace")
        if "gate.py" in existing and "check-commit" in existing:
            print(f"already installed {hook}")
            return
        die(
            f"{hook} exists and is not a direct gate hook; refusing to overwrite it. "
            "If it is managed by pre-commit, add a local gate-check-commit entry "
            "to .pre-commit-config.yaml and re-run doctor. Use --force only when "
            "you explicitly intend to replace the existing hook."
        )
    body = HOOK_TEMPLATE.format(
        python=sys.executable.replace("\\", "/"),
        gate=str(Path(__file__).resolve()).replace("\\", "/"),
        repo=str(repo).replace("\\", "/"),
    )
    hook.write_text(body, encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    print(f"installed {hook}")


def hook_installation(repo: Path) -> tuple[bool, str]:
    """Return whether gate is enforced directly or through pre-commit.

    A pre-commit-generated shell hook intentionally does not mention gate.py;
    the project config is the auditable chain in that setup.  Requiring both a
    known local hook id and the gate command avoids treating an arbitrary
    pre-commit installation as gate enforcement.
    """
    hooks = Path(git(repo, "rev-parse", "--git-path", "hooks").strip())
    if not hooks.is_absolute():
        hooks = repo / hooks
    hook = hooks / "pre-commit"
    if not hook.exists():
        return False, f"{hook} missing"
    body = hook.read_text(encoding="utf-8", errors="replace")
    if "gate.py" in body and "check-commit" in body:
        return True, "direct"
    pcfg = repo / ".pre-commit-config.yaml"
    if "pre-commit" in body and pcfg.exists():
        cfg_text = pcfg.read_text(encoding="utf-8", errors="replace")
        chained = (
            "id: gate-check-commit" in cfg_text
            and "gate.py" in cfg_text
            and "check-commit" in cfg_text
        )
        if chained:
            return True, "pre-commit-chain"
        return False, "pre-commit installed, gate-check-commit chain missing"
    return False, "existing hook is not gate-managed"


def cmd_check_commit(args) -> None:
    """Fast, structural, and unforgeable: derived from the staged diff itself."""
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)

    staged = recovery.git_paths(repo, "diff", "--cached", "--name-only", "-z")
    if not staged:
        ok("no staged changes")
        return

    quality = quality_phase(repo, cfg, "commit")
    if quality is not None and quality["result"] != "PASS":
        fail(
            "required continuous-quality commit checks failed.",
            quality["tail"],
        )

    has_head = git(repo, "rev-parse", "--verify", "HEAD", check=False).strip() != ""
    before = "HEAD" if has_head else None
    flipped = newly_done(repo, cfg, before or "HEAD", ":") if before else []

    queue_staged = prefixed(cfg, cfg["queue_file"]) in [
        p.replace("\\", "/") for p in staged
    ]

    # --- Gate 0: scope. A commit that touches this project's queue may not
    # carry paths from elsewhere in the repo. This is the "don't drag my other
    # projects into this history" rule, enforced instead of merely written down.
    if queue_staged and (cfg.get("project_prefix") or "").strip("/"):
        outside = [p for p in staged if not in_project(cfg, p)]
        if outside:
            fail(
                "commit touches the task queue and paths outside "
                f"'{cfg['project_prefix']}'.",
                "Outside paths:\n  "
                + "\n  ".join(outside)
                + "\n\nStage this project's paths only. Never `git add -A` in a "
                "repo that hosts several projects.",
            )

    if not flipped:
        ok(f"{len(staged)} staged path(s); no task flipped to DONE")
        return

    # --- Gate 1: fake completion. A DONE flip needs code, not just documents.
    code_paths = [
        p for p in staged if not matches_any(project_rel(cfg, p), cfg["doc_only_globs"])
    ]
    recovered = None
    recovery_history = any(e.get("task") in flipped for e in
                           recovery._read_ndjson(recovery.index_path(repo), "recovery index"))
    if not code_paths or recovery_history:
        # One exception, and it carries its own proof: a task whose
        # implementation is already in history, recovered by `recover-task`,
        # whose receipt is bound to those declared blobs and to a fresh PASS
        # from the unchanged canonical oracle (docs/recovery.md). Everything
        # below still runs — this replaces no other gate.
        allowance = recovery.done_allowance(repo, cfg, flipped, staged)
        if not allowance["allowed"]:
            fail(
                f"fake completion — task(s) {', '.join(flipped)} flipped to DONE "
                f"but the commit touches only documentation.",
                "Staged paths:\n  "
                + "\n  ".join(staged)
                + "\n\nThis is the fake-completion pattern (measured at ~60% of tasks in the\n"
                "orchestrator this gate replaced).\n"
                "A task is done when the code changed, not when the queue says so.\n"
                f"\nNo supported recovery covers this commit: {allowance['detail']}",
            )
        recovered = allowance
        # Record what this commit is allowed on, bound to the commit it is
        # about to become. Nothing later may call a queue-only DONE verified
        # on the strength of a recovery receipt alone.
        recovery.record_completion(repo, cfg, flipped[0], allowance)
        print(f"GATE: {allowance['detail']}")

    # --- Gate 2: frozen paths must not move.
    frozen = cfg.get("frozen_globs") or []
    if frozen:
        violating = [p for p in staged if matches_any(project_rel(cfg, p), frozen)]
        if violating:
            fail(
                "frozen path modified in a DONE commit: " + ", ".join(violating),
                "frozen_globs in .gate/config.json forbids changing these while "
                "closing a task. Weakening the oracle is how a loop converges on "
                "rewriting its own tests.",
            )

    # --- Gate 3: a fresh, matching verdict from the real acceptance command.
    if cfg.get("require_verdict_on_done", True):
        v = read_verdict(repo)
        if not v:
            fail(
                f"no verdict recorded for {', '.join(flipped)}.",
                "Run the acceptance command through the gate first:\n"
                f"  python {Path(__file__).name} verify --repo {repo}",
            )
        age = time.time() - float(v.get("at_epoch", 0))
        if age > float(cfg["verdict_max_age_s"]):
            fail(
                f"verdict is {int(age)}s old (limit {cfg['verdict_max_age_s']}s).",
                "Re-run: gate.py verify",
            )
        if v.get("result") != "PASS":
            fail(
                f"last verdict is {v.get('result')}, not PASS.",
                json.dumps(v, indent=2, ensure_ascii=False),
            )
        expected = end_baseline_for(repo, cfg, ":", flipped[0])
        if expected is not None and v.get("count") is not None:
            if int(v["count"]) != int(expected):
                fail(
                    f"queue declares end baseline {expected} for {flipped[0]} "
                    f"but the acceptance run measured {v['count']}.",
                    "The number in the queue is a claim; the number from the "
                    "command is the fact. Fix whichever is wrong.",
                )

    if recovered:
        ok(
            f"task(s) {', '.join(flipped)} → DONE on recovery receipt "
            f"{recovered['receipt'][:10]} and a fresh PASS verdict"
        )
        return
    ok(
        f"task(s) {', '.join(flipped)} → DONE with {len(code_paths)} code path(s) "
        f"and a fresh PASS verdict"
    )


def cmd_verify(args) -> None:
    """Run the real acceptance command and record what it actually said."""
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)
    payload = run_acceptance(repo, cfg, args.cmd or cfg["verify_cmd"])
    print(
        f"GATE: {payload['result']} exit={payload['exit_code']} "
        f"count={payload['count']} in {payload['elapsed_s']}s"
    )
    if payload["result"] != "PASS":
        print(payload["tail"], file=sys.stderr)
        raise SystemExit(2)


def run_acceptance(repo: Path, cfg: dict, cmd: str | None = None) -> dict:
    # Invalidate even if monitor startup, configuration or initial capture fails.
    write_verdict(repo, {"result": "RUNNING", "cmd": cmd or cfg["verify_cmd"], "at_epoch": time.time()})
    monitor = recovery.InputMonitor(repo, cfg).start()
    try:
        return _run_acceptance_monitored(repo, cfg, cmd, monitor)
    finally:
        monitor.finish()


def _run_acceptance_monitored(repo: Path, cfg: dict, cmd, monitor) -> dict:
    """Run the acceptance command, write the verdict, return it. Shared by
    `verify` and by the runner's own re-check after a judge revision."""
    cmd = cmd or cfg["verify_cmd"]
    # The oracle runs where the project lives, not at the repo root — a repo
    # can host several projects.
    workdir = repo / (cfg.get("project_prefix") or "")

    print(f"GATE: running acceptance command in {workdir}: {cmd}")
    started = time.time()
    # What the oracle is about to run over, captured before it starts. The
    # capture doubles as the invalidation of the previous verdict: from here
    # on there is no PASS on record, so a run that times out, is interrupted,
    # or dies cannot leave an older success standing as evidence.
    write_verdict(repo, {"result": "RUNNING", "cmd": cmd, "at_epoch": started})
    inputs_before = recovery.acceptance_inputs(repo, cfg)

    def record(payload: dict) -> dict:
        """Write the verdict and say so in the append-only journal.

        verdict.json is local, unsigned and writable; on its own it is a claim.
        The journal line is what the gate cross-checks before it accepts a
        completion that carries no code of its own, so editing one file is no
        longer enough to invent an acceptance run."""
        write_verdict(repo, payload)
        journal(repo, {"event": "acceptance", "result": payload["result"],
                       "cmd": payload["cmd"], "count": payload.get("count"),
                       "at_epoch": payload["at_epoch"], "head": payload.get("head"),
                       "verdict_digest": recovery.digest(payload), "pid": os.getpid()})
        return payload

    unfinished = {
        "cmd": cmd, "count": None, "exit_code": None, "elapsed_s": None,
        "at_epoch": time.time(), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "head": git(repo, "rev-parse", "HEAD", check=False).strip(),
        "inputs_before": inputs_before, "inputs_after": None, "recovery": {},
        "tail": "", "quality": None,
    }
    record({**unfinished, "result": "RUNNING"})
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workdir),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=cfg["verify_timeout_s"],
        )
    except subprocess.TimeoutExpired:
        record({**unfinished, "result": "TIMEOUT",
                "elapsed_s": round(time.time() - started, 1),
                "tail": f"acceptance command exceeded {cfg['verify_timeout_s']}s"})
        raise
    except BaseException as exc:
        record({**unfinished, "result": "INTERRUPTED",
                "elapsed_s": round(time.time() - started, 1),
                "tail": f"acceptance command did not finish: {exc!r}"})
        raise
    elapsed = round(time.time() - started, 1)
    output = (proc.stdout or "") + (proc.stderr or "")
    quality = quality_phase(repo, cfg, "task")
    if quality is not None:
        output += "\n" + quality["tail"]
    tail = "\n".join(output.strip().splitlines()[-15:])

    count = None
    m = None
    for m in re.finditer(cfg["count_regex"], output):
        pass
    if m:
        count = int(m.group(1))

    result = "PASS" if proc.returncode == 0 else "FAIL"
    if quality is not None and quality["result"] != "PASS":
        result = "FAIL"

    # Non-vacuity: an oracle that asserts nothing is not an oracle.
    if result == "PASS" and count == 0:
        result = "VACUOUS"

    # The same capture again: a completion is bound to inputs that did not
    # move while they were being judged.
    inputs_after = recovery.acceptance_inputs(repo, cfg)
    drift = sorted(set(recovery.inputs_differences(inputs_before, inputs_after) + monitor.finish()))
    if drift:
        result = "INPUTS_CHANGED"

    payload = {
        "result": result,
        "exit_code": proc.returncode,
        "count": count,
        "cmd": cmd,
        "elapsed_s": elapsed,
        "at_epoch": time.time(),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "head": git(repo, "rev-parse", "HEAD", check=False).strip(),
        "fingerprint": worktree_fingerprint(repo),
        "tail": tail,
        "quality": quality,
        # The fingerprint above hashes `git status` output — which paths were
        # dirty, not what was in them. These two capture the content the oracle
        # actually ran over: HEAD, every changed or untracked path, and the
        # configuration. A recovered task's completion commit carries no code
        # of its own, so it is accepted against these instead.
        "inputs_before": inputs_before,
        "inputs_after": inputs_after,
        "inputs_drift": drift,
        "recovery": recovery.verdict_binding(repo, cfg),
    }
    record(payload)
    return payload


def cmd_audit(args) -> None:
    """Re-derive the structural verdict over history. Forgery shows up here."""
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)
    n = args.last
    shas = [s for s in git(repo, "log", f"-{n}", "--format=%H").splitlines() if s]

    findings = []
    for sha in shas:
        parent = git(repo, "rev-parse", f"{sha}^", check=False).strip()
        if not parent:
            continue
        flipped = newly_done(repo, cfg, parent, sha)
        if not flipped:
            continue
        names = [
            p
            for p in git(repo, "show", "--name-only", "--format=", sha).splitlines()
            if p.strip()
        ]
        code = [
            p
            for p in names
            if not matches_any(project_rel(cfg, p), cfg["doc_only_globs"])
        ]
        subject = git(repo, "log", "-1", "--format=%s", sha).strip()
        if not code:
            # A queue-only DONE is the fake-completion shape. It is legitimate
            # only when a receipt that still revalidates, an acceptance run
            # recorded against this very commit, and a cleared review barrier
            # all line up; anything less stays a finding.
            explained = recovery.audit_recovered(repo, cfg, sha, flipped)
            label, clean = explained or ("FAKE_COMPLETION", False)
            findings.append((sha[:10], ", ".join(flipped), subject, label, clean))
        else:
            findings.append(
                (sha[:10], ", ".join(flipped), subject, f"ok ({len(code)} code paths)", True)
            )

    if not findings:
        print(f"GATE audit: no DONE transitions in the last {n} commits.")
        return

    print(f"GATE audit — DONE transitions in the last {n} commits:\n")
    bad = 0
    for sha, tasks, subject, verdict, clean in findings:
        flag = "  " if clean else "!!"
        if not clean:
            bad += 1
        print(f"{flag} {sha}  {tasks:<12}  {verdict}")
        print(f"     {subject}")
    print(f"\n{bad} unproven completion(s) out of {len(findings)} DONE transition(s).")
    if bad:
        raise SystemExit(1)


# ------------------------------------------------------------- the run loop


def journal(repo: Path, event: dict) -> None:
    p = repo / GATE_DIR / "journal.ndjson"
    p.parent.mkdir(parents=True, exist_ok=True)
    event = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def tool_state(repo: Path) -> dict:
    p = repo / GATE_DIR / "tool-status.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def set_tool_state(repo: Path, state: dict) -> None:
    p = repo / GATE_DIR / "tool-status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def head_task(cfg: dict, tasks: dict[str, dict]) -> tuple[str | None, str]:
    """Return (task_id, reason). Mirrors the queue's own selection rule."""
    for tid, row in tasks.items():
        st = row["status"]
        if st in cfg["in_progress_markers"]:
            return tid, "in_progress"
        if st in cfg["blocked_markers"]:
            return tid, "blocked"
        if st in cfg["todo_markers"]:
            return tid, "todo"
        # DONE and anything else: keep scanning.
    return None, "empty"


def admit_verdicts(
    cfg: dict,
    repo: Path,
    tasks: dict[str, dict],
    files_map: dict[str, list[str]],
    workers_map: dict[str, str] | None = None,
    after_map: dict[str, list[str]] | None = None,
    approved_map: dict[str, str] | None = None,
) -> dict[str, dict]:
    """Sweet-spot admission for every TODO task: small, isolated, implicitly
    verifiable. Deterministic — a refusal carries its reasons as data, there
    is no judgement call to argue with."""
    out: dict[str, dict] = {}
    todo = [t for t, r in tasks.items() if r["status"] in cfg["todo_markers"]]
    human_re = re.compile(cfg["human_decision_regex"], re.IGNORECASE)
    verdict = read_verdict(repo)
    workers_map = workers_map or {}
    after_map = after_map or {}
    approved_map = approved_map or {}
    known = known_workers(cfg)
    order = list(tasks)

    def ordered_pair(a: str, b: str) -> bool:
        return b in after_map.get(a, []) or a in after_map.get(b, [])

    for tid in todo:
        files = files_map.get(tid)
        if files is None:
            out[tid] = {
                "verdict": "undeclared-scope",
                "reason": f"no '<!-- task:{tid} files: … -->' line in the queue",
            }
            continue
        reasons = []
        if len(files) > cfg["max_task_files"]:
            reasons.append(
                f"too-broad: {len(files)} files > max_task_files={cfg['max_task_files']}"
            )
        overlaps = [
            f"{other} ({', '.join(sorted(set(files) & set(files_map.get(other) or [])))})"
            for other in todo
            if other != tid
            and set(files) & set(files_map.get(other) or [])
            and not ordered_pair(tid, other)
        ]
        if overlaps:
            reasons.append("overlaps: " + "; ".join(overlaps))
        for dep in after_map.get(tid, []):
            if dep not in tasks:
                reasons.append(f"unknown-dependency: {dep} is not in the queue")
            elif tasks[dep]["status"] not in cfg["done_markers"] and order.index(dep) > order.index(tid):
                reasons.append(f"out-of-order: {dep} must come before {tid} in the queue")
        if human_re.search(" ".join(tasks[tid]["cells"])):
            reasons.append("needs-human: row matches human_decision_regex")
        pin = workers_map.get(tid)
        if pin and pin not in known:
            reasons.append(
                f"unknown-worker: '{pin}' is not one of {', '.join(sorted(known))}"
            )
        if not cfg.get("verify_cmd"):
            reasons.append("no-oracle: verify_cmd is empty")
        elif verdict and verdict.get("result") == "VACUOUS":
            reasons.append("no-oracle: last verify was VACUOUS")
        # Reversibility tier: an irreversible task is never a judgement call
        # for the runner. Without an explicit per-task approval it is refused,
        # and cmd_run stops on it regardless of advisory mode.
        needs_approval = False
        irreversible = irreversible_files(cfg, files)
        if irreversible and tid not in approved_map:
            needs_approval = True
            reasons.append(
                "needs-approval: touches irreversible path(s) "
                + ", ".join(irreversible)
                + f"; add `<!-- task:{tid} approved: <who/date> -->`"
            )
        out[tid] = {
            "verdict": "refuse" if reasons else "admit",
            "reason": "; ".join(reasons),
            "needs_approval": needs_approval,
        }
    return out


def cmd_admit(args) -> None:
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)
    qpath = repo / prefixed(cfg, cfg["queue_file"])
    if not qpath.exists():
        die(f"queue not found: {qpath}")
    text = qpath.read_text(encoding="utf-8")
    pins = parse_task_workers(text)
    verdicts = admit_verdicts(
        cfg, repo, parse_queue(text), parse_task_files(text), pins,
        parse_task_after(text), parse_task_approved(text),
    )
    if not verdicts:
        print("admit: no TODO tasks in the queue.")
        return
    print(
        f"admission — sweet spot: small (≤{cfg['max_task_files']} files), "
        "isolated, verifiable, reversible-or-approved"
    )
    bad = 0
    for tid, v in verdicts.items():
        mark = {"admit": "ok", "refuse": "REFUSE", "undeclared-scope": "UNDECLARED"}[
            v["verdict"]
        ]
        if v.get("needs_approval"):
            mark = "APPROVAL"
        pin = f" [worker: {pins[tid]}]" if tid in pins else ""
        print(f"  {mark:10} {tid}{pin}" + (f" — {v['reason']}" if v["reason"] else ""))
        bad += v["verdict"] != "admit"
    mode = "strict" if cfg.get("strict_admit") else "advisory"
    print(f"{len(verdicts)} TODO task(s), {bad} not admissible. Mode: {mode}.")
    try:
        policies = execution.resolve(cfg, text, parse_queue(text), pins)
        print(f"execution       : {len(policies)} explicit task profiles OK")
    except execution.PolicyError as exc:
        die(f"execution admission refused: {exc}")


def cmd_lock_execution(args) -> None:
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)
    text = (repo / prefixed(cfg, cfg["queue_file"])).read_text(encoding="utf-8")
    tasks = parse_queue(text)
    pending = {tid for tid, row in tasks.items() if row["status"] in cfg["todo_markers"]}
    # The worker may not have written its claim yet. Dispatch already counts
    # as started, including a DONE row undergoing review or revision.
    active = set()
    jp = repo / GATE_DIR / "journal.ndjson"
    if jp.exists():
        for line in jp.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "attempt_start":
                active.add(event.get("task"))
            elif event.get("event") == "task_end":
                active.discard(event.get("task"))
            elif event.get("event") == "run_end":
                active.clear()
    try:
        policies = execution.resolve(cfg, text, tasks, parse_task_workers(text))
        lock = execution.freeze(repo, policies, getattr(args, "reason", None), pending - active)
    except execution.PolicyError as exc:
        die(str(exc))
    print(f"execution locked: {len(policies)} tasks; sha256={lock['sha256']}")


def reject_run_preflight(repo: Path, cfg: dict, run_id: str, stop: str, message: str) -> None:
    report = render_report(repo, cfg, run_id, [], stop, 0) + f"\nPreflight: {message}\n"
    (repo / GATE_DIR / "RUN-REPORT.md").write_text(report, encoding="utf-8")
    (repo / GATE_DIR / f"RUN-REPORT-{run_id}.md").write_text(report, encoding="utf-8")
    journal(repo, {"event": "admit_refused" if stop.startswith("admit_refused:") else "execution_refused",
                   "run": run_id, "reason": message})
    journal(repo, {"event": "run_end", "run": run_id, "stop": stop,
                   "pid": os.getpid(), "host": platform.node()})
    die(message, code=3)


def is_quota(text: str) -> bool:
    low = text.lower()
    return any(pat in low for pat in QUOTA_PATTERNS)


def tool_outage_reason(text: str, returncode: int = 1) -> str | None:
    """Classify worker/tool outages that are not verdicts on task work."""
    if GIT_WRITE_DENIAL_RE.search(text):
        return "git_write_denied"
    if returncode != 0 and is_quota(text):
        return "quota"
    return None


def disable_worker(repo: Path, cfg: dict, worker: str, reason: str) -> None:
    """Bench an unusable worker so retries rotate without burning attempts."""
    until = time.time() + cfg["quota_cooldown_s"]
    st = tool_state(repo)
    st[worker] = {
        "disabled_until": until,
        "reason": reason,
        "seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    set_tool_state(repo, st)


def _task_id_from_row(line: str) -> str | None:
    match = ROW_RE.match(line)
    if not match:
        return None
    task_id = match.group(1).strip().strip("*` ")
    return task_id if task_id and task_id not in ("ID", "---") else None


def restore_task_row_text(before: str, current: str, task_id: str) -> str:
    """Restore only one queue row, preserving concurrent edits to other rows."""
    before_line = next(
        (line for line in before.splitlines(keepends=True) if _task_id_from_row(line) == task_id),
        None,
    )
    if before_line is None:
        raise ValueError(f"task {task_id} was absent before worker dispatch")

    lines = current.splitlines(keepends=True)
    indexes = [i for i, line in enumerate(lines) if _task_id_from_row(line) == task_id]
    if len(indexes) != 1:
        raise ValueError(f"task {task_id} has {len(indexes)} queue rows after worker dispatch")
    lines[indexes[0]] = before_line
    return "".join(lines)


def queue_status_changes(before: str, after: str) -> dict[str, tuple[str, str]]:
    """Return every task whose parsed queue status changed."""
    old = parse_queue(before)
    new = parse_queue(after)
    changes: dict[str, tuple[str, str]] = {}
    for task_id in old.keys() | new.keys():
        prior = old.get(task_id, {}).get("status", "<missing>")
        current = new.get(task_id, {}).get("status", "<missing>")
        if prior != current:
            changes[task_id] = (prior, current)
    return changes


def non_gate_worktree_state(repo: Path) -> tuple[str, ...]:
    """Stable dirty-state snapshot excluding runner-owned .gate artifacts."""
    lines = git(repo, "status", "--porcelain=v1", "-uall").splitlines()
    return tuple(sorted(line for line in lines if line.strip() and GATE_DIR not in line))


INCOMPLETE_WORK_PATTERNS = (
    re.compile(r"\bstill running in (?:the )?background\b", re.IGNORECASE),
    re.compile(
        r"\brunning\s*[;,.:-]\s*waiting on (?:it|the (?:test|verification|build|acceptance))\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bwaiting on (?:it|them) before .*\b(?:commit|verdict)\b", re.IGNORECASE),
)


def worker_incomplete_reason(output: str) -> str | None:
    """Detect a one-shot worker returning while its acceptance work is pending."""
    if any(pattern.search(output) for pattern in INCOMPLETE_WORK_PATTERNS):
        return "background_work_pending"
    return None


def available_workers(repo: Path, cfg: dict, prefer: str | None = None) -> list[str]:
    """Usable workers in dispatch order. A per-task pin goes first even when it
    is not in the run-wide list; benched or uninstalled tools drop out, so a
    pinned task still falls through to the run-wide list on retry."""
    state = tool_state(repo)
    now = time.time()
    out = []
    candidates = ([prefer] if prefer else []) + list(cfg["workers"])
    seen: set[str] = set()
    for w in candidates:
        if w in seen:
            continue
        seen.add(w)
        info = state.get(w, {})
        if info.get("disabled_until", 0) > now:
            continue
        if not resolve_tool(w):
            continue
        out.append(w)
    return out


def resolve_tool(name: str) -> str | None:
    """Find the executable, including Windows shims a POSIX PATH lookup misses."""
    import shutil

    for candidate in (name, f"{name}.cmd", f"{name}.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    for extra in (
        Path.home() / f".{name}" / "bin" / f"{name}.exe",
        Path.home() / f".{name}-code" / "bin" / f"{name}.exe",
        Path.home() / "AppData/Local/pnpm" / f"{name}.cmd",
        Path.home() / ".local/bin" / f"{name}.exe",
    ):
        if extra.exists():
            return str(extra)
    return None


def notify(cfg: dict, repo: Path, title: str, body: str = "") -> None:
    """Milestone notification. It must never be able to fail a run."""
    cmd = cfg.get("notify_cmd")
    if not cmd:
        return
    try:
        subprocess.run(
            cmd.replace("{title}", title).replace("{body}", body[:1500]),
            shell=True,
            cwd=str(repo),
            timeout=60,
            capture_output=True,
        )
    except Exception as exc:
        print(f"GATE: notification failed: {exc}", file=sys.stderr)


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the worker and everything it started. A surviving grandchild would
    keep writing into the worktree after we declared the attempt over."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            proc.kill()


class WorkerResult:
    """Just enough of CompletedProcess for the caller, plus what we streamed."""

    def __init__(self, returncode: int, output: str, timed_out: bool = False):
        self.returncode = returncode
        self.stdout = output
        self.stderr = ""
        self.timed_out = timed_out


def spawn_worker(
    repo: Path,
    cfg: dict,
    worker: str,
    prompt: str,
    prompt_file: Path,
    log_path: Path | None = None,
    on_beat=None,
    template: list[str] | None = None,
    timeout_s: float | None = None,
    extra: dict[str, str] | None = None,
):
    """Spawn one headless CLI process and stream it. `template` overrides the
    worker contract (used by the read-only judge and the usage probe, which
    use their own invocation contracts); `timeout_s`
    overrides task_timeout_s; `extra` adds placeholder substitutions."""
    projdir = repo / (cfg.get("project_prefix") or "")
    exe = resolve_tool(worker)
    if not exe:
        return None, f"{worker} not found on PATH"
    if template is None:
        # worker_cmds in the config overrides the built-in contracts, so flags
        # can be tuned without editing this file.
        template = (cfg.get("worker_cmds") or {}).get(worker) or WORKER_CMDS.get(worker)
        if not template:
            return None, f"no invocation contract for {worker}"

    argv = []
    for part in template:
        part = (
            part.replace("{projdir}", str(projdir))
            .replace("{prompt_file}", str(prompt_file))
            .replace("{prompt}", prompt)
        )
        for key, value in (extra or {}).items():
            part = part.replace("{" + key + "}", value)
        argv.append(part)
    argv[0] = exe

    try:
        requested = execution.selected(cfg, worker)
        if requested:
            argv = execution.arguments(argv, worker, requested)
            journal(repo, {"event": "execution_requested", "task": cfg.get("_execution_task"),
                           "role": cfg.get("_execution_role", "workers"), "worker": worker,
                           "profile": requested, "profile_sha256": execution.digest(requested)})
    except execution.PolicyError as exc:
        return None, f"execution_policy:{exc}"

    # Codex reads its prompt from stdin: the Windows .cmd shim truncates a
    # multi-line argument at the first newline.
    stdin_text = prompt if worker == "codex" else None
    limit = float(timeout_s) if timeout_s else float(cfg["task_timeout_s"])

    # Stream rather than capture. A 56-minute task that writes nothing until it
    # exits is indistinguishable from a hung one, which is the whole complaint.
    log = open(log_path, "w", encoding="utf-8", buffering=1) if log_path else None
    proc = subprocess.Popen(
        argv,
        cwd=str(projdir),
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=(os.name != "nt"),
    )
    if stdin_text is not None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except Exception:
            pass

    import threading

    state = {"last_line": "", "lines": 0}
    chunks: list[str] = []

    def pump() -> None:
        for raw in proc.stdout:
            chunks.append(raw)
            state["lines"] += 1
            stripped = raw.strip()
            if stripped:
                state["last_line"] = stripped[:200]
            if log:
                log.write(raw)

    t = threading.Thread(target=pump, daemon=True)
    t.start()

    started = time.time()
    last_beat = started
    timed_out = False
    beat_every = max(30, int(cfg.get("heartbeat_s", 300)))

    while proc.poll() is None:
        time.sleep(2)
        now = time.time()
        if now - started > limit:
            kill_tree(proc)
            timed_out = True
            break
        if on_beat and now - last_beat >= beat_every:
            last_beat = now
            on_beat(int(now - started), state["lines"], state["last_line"])

    t.join(timeout=10)
    if not t.is_alive() and callable(getattr(proc.stdout, "close", None)):
        proc.stdout.close()
    if log:
        log.close()
    rc = proc.returncode if proc.returncode is not None else -1
    result = WorkerResult(rc, "".join(chunks), timed_out)
    if requested:
        observation = execution.observed(result.stdout, requested)
        journal(repo, {"event": "execution_observed", "task": cfg.get("_execution_task"),
                       "role": cfg.get("_execution_role", "workers"), "worker": worker,
                       "requested": requested, "observed": observation})
        result.execution_mismatch = observation["mismatch"]
        if result.execution_mismatch:
            result.returncode = 1
    return result, None


# ------------------------------------------------------------ usage probe

PROBE_PROMPT = "Reply with the single word OK and nothing else. Do not use any tool."


def probe_worker(repo: Path, cfg: dict, worker: str, run_dir: Path | None = None) -> tuple[bool, str]:
    """One tiny real request through the worker's own contract. Returns
    (usable, reason). Only a quota-shaped answer is a quota outage; a probe
    that never reached the provider is reported as such so the caller can
    bench it briefly instead of for the whole quota cooldown."""
    if not resolve_tool(worker):
        return False, "not_installed"
    pcfg = sub_cfg(cfg, "probe")
    pf = (run_dir or repo / GATE_DIR / "runs" / "probe") / f"probe-{worker}.prompt.md"
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(PROBE_PROMPT, encoding="utf-8")
    try:
        proc, err = spawn_worker(
            repo, cfg, worker, PROBE_PROMPT, pf,
            log_path=pf.with_suffix(".log"),
            timeout_s=pcfg.get("timeout_s", 90),
        )
    except Exception as exc:  # a probe must never take the run down
        return False, f"error:{type(exc).__name__}"
    if proc is None:
        return False, f"error:{err}"
    out = proc.stdout or ""
    if is_quota(out):
        return False, "quota"
    if getattr(proc, "timed_out", False):
        return False, "timeout"
    if proc.returncode != 0:
        return False, f"exit:{proc.returncode}"
    return True, "ok"


def bench_from_probe(repo: Path, cfg: dict, worker: str, reason: str, run_id: str) -> None:
    pcfg = sub_cfg(cfg, "probe")
    st = tool_state(repo)
    if reason == "ok":
        st[worker] = {"probed_run": run_id, "reason": "ok",
                      "seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    else:
        # Quota occupies the real cooldown; anything that did not reach the
        # provider gets the short retry window only.
        secs = cfg["quota_cooldown_s"] if reason == "quota" else pcfg.get("retry_s", 600)
        st[worker] = {
            "disabled_until": time.time() + float(secs),
            "reason": f"probe:{reason}",
            "probed_run": run_id,
            "seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    requested = execution.selected(cfg, worker)
    if requested:
        st[worker]["probed_profile"] = execution.digest(requested)
    set_tool_state(repo, st)


def probe_candidates(cfg: dict, pins: dict[str, str]) -> list[str]:
    jcfg = sub_cfg(cfg, "judge")
    judge_tools = [JUDGE_TOOL.get(m, m) for m in jcfg.get("chain", [])] if jcfg.get("enabled") else []
    seen: list[str] = []
    for w in list(cfg["workers"]) + list(pins.values()) + judge_tools:
        if w and w not in seen:
            seen.append(w)
    return seen


def ensure_probed(repo: Path, cfg: dict, worker: str, run_id: str, run_dir: Path) -> bool:
    """Probe `worker` once per run (lazily, so a cooldown that expires mid-run
    is re-checked before the first dispatch that would use it). Returns
    whether it is usable now."""
    info = tool_state(repo).get(worker, {})
    if info.get("disabled_until", 0) > time.time():
        return False
    if not sub_cfg(cfg, "probe").get("enabled", True):
        return True
    requested = execution.selected(cfg, worker)
    profile_ok = not requested or info.get("probed_profile") == execution.digest(requested)
    if info.get("probed_run") == run_id and profile_ok:
        return True
    usable, reason = probe_worker(repo, cfg, worker, run_dir)
    bench_from_probe(repo, cfg, worker, reason, run_id)
    journal(repo, {"event": "probe", "run": run_id, "worker": worker,
                   "usable": usable, "reason": reason})
    print(f"  probe {worker}: {'ok' if usable else reason}")
    return usable


def cmd_usage(args) -> None:
    """Probe every configured worker and judge CLI now and print the table."""
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)
    qpath = repo / prefixed(cfg, cfg["queue_file"])
    pins = parse_task_workers(qpath.read_text(encoding="utf-8")) if qpath.exists() else {}
    text = qpath.read_text(encoding="utf-8") if qpath.exists() else ""
    try:
        policies = execution.resolve(cfg, text, parse_queue(text), pins)
        if not policies:
            policies = execution.resolve(cfg, "", {"usage": {"status": cfg["todo_markers"][0]}}, {})
    except execution.PolicyError as exc:
        die(f"usage needs explicit execution profiles before probing: {exc}")
    run_id = "usage-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    print("usage — one tiny request per CLI; quota benches for the cooldown, "
          "a probe that never reached the provider benches briefly")
    seen_profiles = set()
    for task, policy in policies.items():
        for role, members in policy["profiles"].items():
            for member, requested in members.items():
                w = JUDGE_TOOL.get(member, member) if role == "judges" else member
                key = (w, execution.digest(requested))
                if key in seen_profiles:
                    continue
                seen_profiles.add(key)
                probe_cfg = execution.bind(cfg, policy, task)
                probe_cfg.update(_execution_role=role, _execution_member=member)
                usable = ensure_probed(repo, probe_cfg, w, run_id, repo / GATE_DIR / "runs" / run_id)
                print(f"  {w:8} {'ok' if usable else 'BENCHED'}  {requested['model']} / {requested['reasoning_effort']}")
    st = tool_state(repo)
    now = time.time()
    for w, info in st.items():
        if info.get("disabled_until", 0) > now:
            mins = int((info["disabled_until"] - now) // 60)
            print(f"  {w:8} benched {mins}m more ({info.get('reason')})")


# ------------------------------------------------------------------ judge


def _last_json_object(text: str) -> dict | None:
    """Best-effort: the last balanced {...} in a transcript, parsed."""
    end = text.rfind("}")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            ch = text[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[i:end + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        end = text.rfind("}", 0, end)
    return None


def parse_judge_output(output: str, out_file: Path) -> dict | None:
    """Pull the verdict out of whatever the CLI produced: codex writes the
    schema-shaped answer to out_file; claude wraps it in a result envelope
    under structured_output; anything else must end in a JSON object."""
    candidates: list[dict] = []
    if out_file.exists():
        try:
            obj = json.loads(out_file.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                candidates.append(obj)
        except (json.JSONDecodeError, OSError):
            pass
    env = _last_json_object(output)
    if env:
        if isinstance(env.get("structured_output"), dict):
            candidates.append(env["structured_output"])
        elif isinstance(env.get("result"), str):
            inner = _last_json_object(env["result"])
            if inner:
                candidates.append(inner)
        candidates.append(env)
    for obj in candidates:
        if validate_judge_verdict(obj):
            return normalize_judge_verdict(obj)
    return None


def validate_judge_verdict(obj: dict) -> bool:
    score = obj.get("score")
    verdict = obj.get("verdict")
    return (
        isinstance(score, int) and 0 <= score <= 100
        and verdict in ("pass", "revise", "escalate")
    )


def normalize_judge_verdict(obj: dict) -> dict:
    findings = []
    for f in obj.get("findings") or []:
        if isinstance(f, dict) and f.get("issue"):
            findings.append({
                "severity": f.get("severity") or "medium",
                "file": f.get("file") or "",
                "issue": str(f["issue"]),
                "fix": f.get("fix") or "",
            })
    return {
        "score": int(obj["score"]),
        "verdict": obj["verdict"],
        "findings": findings,
        "revision_brief": str(obj.get("revision_brief") or ""),
    }


def judge_once(
    repo: Path, cfg: dict, prompt: str, run_dir: Path, tag: str, run_id: str
) -> tuple[dict | None, str]:
    """Walk the judge chain. Returns (verdict, member) or (None, why)."""
    jcfg = sub_cfg(cfg, "judge")
    cmds = dict(JUDGE_CMDS)
    cmds.update(cfg.get("judge_cmds") or {})
    schema_file = run_dir / f"{tag}.schema.json"
    schema_file.write_text(json.dumps(JUDGE_SCHEMA), encoding="utf-8")
    failures: list[str] = []
    for member in jcfg.get("chain", []):
        template = cmds.get(member)
        tool = JUDGE_TOOL.get(member, member)
        member_cfg = {**cfg, "_execution_role": "judges", "_execution_member": member}
        if not template:
            failures.append(f"{member}:no_contract")
            continue
        if not ensure_probed(repo, member_cfg, tool, run_id, run_dir):
            failures.append(f"{member}:benched")
            continue
        out_file = run_dir / f"{tag}.{member}.out.json"
        if out_file.exists():
            out_file.unlink()
        pf = run_dir / f"{tag}.{member}.prompt.md"
        pf.write_text(prompt, encoding="utf-8")
        proc, err = spawn_worker(
            repo, member_cfg, tool, prompt, pf,
            log_path=pf.with_suffix(".log"),
            template=template,
            timeout_s=jcfg.get("timeout_s", 1200),
            extra={
                "schema": json.dumps(JUDGE_SCHEMA),
                "schema_file": str(schema_file),
                "out_file": str(out_file),
            },
        )
        if proc is not None and getattr(proc, "execution_mismatch", False):
            return None, "execution_mismatch"
        if proc is None:
            failures.append(f"{member}:{err}")
            continue
        out = proc.stdout or ""
        if getattr(proc, "timed_out", False):
            failures.append(f"{member}:timeout")
            continue
        outage = tool_outage_reason(out, proc.returncode)
        if outage:
            disable_worker(repo, cfg, tool, outage)
            failures.append(f"{member}:{outage}")
            continue
        verdict = parse_judge_output(out, out_file)
        if verdict is None:
            failures.append(f"{member}:unparsable")
            continue
        return verdict, member
    return None, "; ".join(failures) or "empty chain"


def format_findings(findings: list[dict]) -> str:
    if not findings:
        return "(none)"
    return "\n".join(
        f"- [{f['severity']}] {f['file'] + ': ' if f['file'] else ''}{f['issue']}"
        + (f" → fix: {f['fix']}" if f["fix"] else "")
        for f in findings
    )


def judge_task(
    repo: Path,
    cfg: dict,
    run_id: str,
    run_dir: Path,
    tid: str,
    base_head: str,
    files: list[str],
    last_worker: str,
    pin: str | None,
    extra_commits: list[str] | None = None,
) -> dict:
    """Judge a closed task and iterate at most judge.max_revisions times.

    Returns {"final": pass|escalated|skipped|broke_verify, "reason", "rounds":
    [{"round","score","verdict","member","worker","findings"}]}. The queue row
    is never touched here; a revision is an ordinary code commit that the
    runner re-verifies itself, because check-commit only engages on DONE flips.
    """
    jcfg = sub_cfg(cfg, "judge")
    rounds: list[dict] = []
    history = [e for e in recovery.journal_events(repo) if e.get("task") == tid] if extra_commits else []
    revisions = sum(e.get("event") == "revision_start" for e in history)
    worker_of_last_commit = last_worker
    scores: list[int] = [e["score"] for e in history if e.get("event") == "judge_verdict"]
    exhausted = any(e.get("event") == "judge_escalated" and
                    ("no progress" in e.get("reason", "") or "revision cap" in e.get("reason", ""))
                    for e in history)
    while True:
        head = git(repo, "rev-parse", "HEAD").strip()
        commits = [
            s[:10] for s in git(repo, "rev-list", f"{base_head}..{head}", check=False).split()
        ]
        # A recovered task closes an implementation that landed before this
        # run started, so it is not in the range above. Name it explicitly:
        # the reviewer must read the code, not only the commit that closed it.
        commits = [s[:10] for s in (extra_commits or []) if s[:10] not in commits] + commits
        v = read_verdict(repo) or {}
        prompt = cfg["judge_prompt"].format(
            task=tid,
            commit=head[:10],
            base=base_head[:10],
            commits=", ".join(commits) or head[:10],
            files=", ".join(files) or "(undeclared)",
            verify_tail=(v.get("tail") or "")[-1500:],
            pass_score=jcfg["pass_score"],
        )
        tag = f"{tid}-judge{len(rounds) + 1}"
        journal(repo, {"event": "judge_start", "task": tid, "round": len(rounds) + 1,
                       "commit": head})
        print(f"  judge round {len(rounds) + 1} for {tid} …")
        verdict, member = judge_once(repo, cfg, prompt, run_dir, tag, run_id)
        if verdict is None:
            if member == "execution_mismatch":
                return _escalate(repo, tid, rounds, member)
            journal(repo, {"event": "judge_skipped", "task": tid, "reason": member})
            print(f"  judge unavailable ({member}) — task stays DONE, unreviewed")
            return {"final": "skipped", "reason": member, "rounds": rounds}
        rounds.append({
            "round": len(rounds) + 1,
            "score": verdict["score"],
            "verdict": verdict["verdict"],
            "member": member,
            "worker": worker_of_last_commit,
            "findings": verdict["findings"],
            "brief": verdict["revision_brief"],
        })
        scores.append(verdict["score"])
        journal(repo, {"event": "judge_verdict", "task": tid, "round": len(rounds),
                       "score": verdict["score"], "verdict": verdict["verdict"],
                       "member": member, "findings": len(verdict["findings"]),
                       "commit": head,
                       "receipt": recovery.open_barriers(repo).get(tid, {}).get("receipt"),
                       "passed": verdict["verdict"] == "pass" or verdict["score"] >= jcfg["pass_score"]})
        print(f"  judge ({member}): score {verdict['score']}, {verdict['verdict']}")
        notify(cfg, repo, f"{tid} judged {verdict['score']} ({verdict['verdict']}, {member})",
               format_findings(verdict["findings"]))

        if verdict["verdict"] == "pass" or verdict["score"] >= jcfg["pass_score"]:
            return {"final": "pass", "reason": "", "rounds": rounds}
        if verdict["verdict"] == "escalate":
            return _escalate(repo, tid, rounds, "judge asked for a human")
        if exhausted or revisions >= jcfg["max_revisions"]:
            return _escalate(repo, tid, rounds, f"revision cap {jcfg['max_revisions']} reached")
        if len(scores) >= 2 and scores[-1] - scores[-2] < jcfg["min_gain"]:
            return _escalate(
                repo, tid, rounds,
                f"no progress: {scores[-2]} → {scores[-1]} (< min_gain {jcfg['min_gain']})",
            )

        # Pick a usable worker other than the one that made the last commit.
        revision_cfg = {**cfg, "_execution_role": "revisions"}
        pool = [w for w in available_workers(repo, revision_cfg, prefer=pin)
                if ensure_probed(repo, revision_cfg, w, run_id, run_dir)]
        others = [w for w in pool if w != worker_of_last_commit]
        if not pool:
            return _escalate(repo, tid, rounds, "no_workers_for_revision")
        # Rotation: a second pass by the same model repeats its own blind
        # spot. Fall back to the same worker only when it is the only usable
        # one, and say so in the journal and the report.
        rotated = bool(others)
        worker = (others or pool)[0]
        rounds[-1]["rotated"] = rotated
        revisions += 1
        rprompt = cfg["revision_prompt"].format(
            task=tid,
            files=", ".join(files) or "(undeclared)",
            brief=verdict["revision_brief"] or "(none)",
            findings=format_findings(verdict["findings"]),
        )
        pf = run_dir / f"{tid}-rev{revisions}.prompt.md"
        pf.write_text(rprompt, encoding="utf-8")
        queue_path = repo / prefixed(cfg, cfg["queue_file"])
        queue_before = queue_path.read_text(encoding="utf-8")
        tree_before = non_gate_worktree_state(repo)
        head_before = head
        journal(repo, {"event": "revision_start", "task": tid, "revision": revisions,
                       "worker": worker, "rotated": rotated,
                       "log": str(pf.with_suffix(".log"))})
        print(f"  revision {revisions} via {worker}{'' if rotated else ' (same worker: no other usable)'} …")

        def beat(secs: int, lines: int, last: str, _tid=tid, _w=worker) -> None:
            print(f"    … {_tid} revising {secs // 60}m, {lines} lines | {last[:80]}")
            journal(repo, {"event": "heartbeat", "task": _tid, "worker": _w,
                           "seconds": secs, "lines": lines, "last_line": last,
                           "phase": "revision"})

        proc, err = spawn_worker(repo, revision_cfg, worker, rprompt, pf,
                                 log_path=pf.with_suffix(".log"), on_beat=beat)
        if proc is not None and getattr(proc, "execution_mismatch", False):
            return _escalate(repo, tid, rounds, "execution_mismatch", final="broke_verify")
        head_after = git(repo, "rev-parse", "HEAD").strip()
        queue_after = queue_path.read_text(encoding="utf-8")
        changed_rows = queue_status_changes(queue_before, queue_after)
        if changed_rows:
            return _escalate(repo, tid, rounds, f"revision changed queue rows {changed_rows}",
                             final="broke_verify")
        if proc is None or getattr(proc, "timed_out", False):
            journal(repo, {"event": "revision_end", "task": tid, "revision": revisions,
                           "worker": worker, "outcome": "failed", "err": err or "timeout"})
            worker_of_last_commit = worker
            if non_gate_worktree_state(repo) != tree_before:
                return _escalate(repo, tid, rounds, "revision left uncommitted changes",
                                 final="broke_verify")
            continue
        if head_after == head_before:
            journal(repo, {"event": "revision_end", "task": tid, "revision": revisions,
                           "worker": worker, "outcome": "no_commit"})
            if non_gate_worktree_state(repo) != tree_before:
                return _escalate(repo, tid, rounds, "revision left uncommitted changes",
                                 final="broke_verify")
            worker_of_last_commit = worker
            print(f"  revision {revisions} made no commit")
            continue
        worker_of_last_commit = worker
        # The verdict is ours: re-run the acceptance on the revised tree.
        payload = run_acceptance(repo, cfg)
        journal(repo, {"event": "revision_end", "task": tid, "revision": revisions,
                       "worker": worker, "outcome": payload["result"],
                       "commit": head_after})
        if payload["result"] != "PASS":
            return _escalate(
                repo, tid, rounds,
                f"revision commit {head_after[:10]} fails acceptance ({payload['result']})",
                final="broke_verify",
            )


def _escalate(repo: Path, tid: str, rounds: list[dict], reason: str, final: str = "escalated") -> dict:
    journal(repo, {"event": "judge_escalated", "task": tid, "reason": reason, "final": final})
    print(f"  {tid} needs you: {reason}")
    return {"final": final, "reason": reason, "rounds": rounds}


class CodexTaskSession:
    """Reuse only the immediately preceding clean, reviewed Codex attempt.

    Persist metadata for audit, never load it as authority after a restart.
    The gate still dispatches exactly one task and owns all stop decisions.
    """

    def __init__(self, repo: Path, cfg: dict, run_dir: Path):
        self.repo, self.cfg, self.run_dir = repo, cfg, run_dir
        self.options = sub_cfg(cfg, "session_reuse")
        self.previous = None

    def snapshot(self) -> dict:
        import hashlib

        # Unlike worktree_fingerprint(), detect a second edit to an already
        # dirty file. Unrelated pre-existing changes may remain, unchanged.
        digest = hashlib.sha256()
        for flags in (("--cached", "HEAD"), ()):
            digest.update(git(self.repo, "diff", "--binary", *flags,
                              "--", ".", ":(exclude).gate").encode())
        for name in sorted(git(self.repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")):
            if not name or name.startswith(".gate/"):
                continue
            path = self.repo / name
            digest.update(name.encode())
            digest.update(path.read_bytes() if path.is_file() else b"<not-file>")
        return {"head": git(self.repo, "rev-parse", "HEAD").strip(),
                "dirty": digest.hexdigest()}

    def background_hash(self, group: str) -> str:
        import hashlib

        root = (self.repo / (self.cfg.get("project_prefix") or "")).resolve()
        paths = [root / p for p in self.options.get("context_files", [])]
        paths += [root / group / "spec.md", root / self.cfg.get("context_file", "CONTEXT.md"),
                  self.repo / GATE_DIR / CONFIG_NAME, root / ".codex/config.toml"]
        codex_dir = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        paths += [codex_dir / "config.toml", codex_dir / "AGENTS.md"]
        digest = hashlib.sha256()
        digest.update(execution.digest(self.cfg.get("_execution_policy")).encode())
        for path in paths:
            digest.update(str(path).encode())
            content = path.read_bytes() if path.is_file() else b"<missing>"
            if path.name == "config.toml" and path.is_file():
                try:
                    import tomllib
                    config = tomllib.loads(content.decode("utf-8"))
                    # Codex registers a newly visited trusted repo at startup.
                    # Ignore only that bookkeeping, retaining explicit untrusted
                    # entries and every other setting. Raw bytes are the safe
                    # fallback on Python 3.10 or malformed TOML.
                    projects = config.get("projects", {})
                    for name, settings in list(projects.items()):
                        if isinstance(settings, dict) and settings.get("trust_level") == "trusted":
                            settings.pop("trust_level")
                            if not settings:
                                projects.pop(name)
                    if not projects:
                        config.pop("projects", None)
                    content = json.dumps(config, sort_keys=True, default=str).encode()
                except (ImportError, ValueError, AttributeError):
                    pass
            digest.update(content)
        return digest.hexdigest()

    def select(self, task: str, worker: str, queue: str, dispatch: int, files: list[str]) -> dict:
        decision = {"mode": "off", "reason": "disabled", "task": task}
        if not self.options.get("enabled"):
            return decision
        groups = re.findall(r"<!--\s*task:" + re.escape(task) + r"\s+session:\s*(\S+)\s*-->", queue)
        if worker != "codex" or (self.cfg.get("worker_cmds") or {}).get("codex"):
            decision["reason"] = "unsupported_worker_or_override"
            return decision
        root = (self.repo / (self.cfg.get("project_prefix") or "")).resolve()
        if (len(groups) != 1 or not re.fullmatch(r"docs/work/[A-Za-z0-9_/-]+", groups[0])
                or not (root / groups[0]).resolve().is_relative_to(root)
                or not (root / groups[0] / "spec.md").is_file()):
            decision["reason"] = "no_valid_group"
            return decision
        if irreversible_files(self.cfg, files):
            decision["reason"] = "irreversible_task"
            return decision
        decision.update(mode="fresh", reason="no_checkpoint", group=groups[0],
                        background=self.background_hash(groups[0]), before=self.snapshot())
        previous = self.previous
        if dispatch != 1:
            decision["reason"] = "retry"
        elif previous:
            if previous["group"] != decision["group"]:
                decision["reason"] = "group_changed"
            elif previous["background"] != decision["background"]:
                decision["reason"] = "background_changed"
            elif previous["after"] != decision["before"]:
                decision["reason"] = "workspace_changed"
            elif previous["tasks"] >= int(self.options["max_tasks"]):
                decision["reason"] = "task_budget"
            elif previous["input_tokens"] >= int(self.options["max_input_tokens"]):
                decision["reason"] = "input_budget"
            else:
                decision.update(mode="resume", reason="eligible", session_id=previous["session_id"],
                                previous_task=previous["task"], tasks=previous["tasks"],
                                input_tokens=previous["input_tokens"])
        return decision

    @staticmethod
    def template(decision: dict) -> list[str] | None:
        if decision["mode"] == "off":
            return None
        # Keep the existing exec sandbox and cwd flags, including on resume.
        argv = WORKER_CMDS["codex"][:-1] + ["--json"]
        if decision["mode"] == "resume":
            argv += ["resume", decision["session_id"]]
        return argv + ["-"]

    def prompt(self, decision: dict, original: str, files: list[str]) -> str:
        if decision["mode"] == "off":
            return original
        task = decision["task"]
        brief = (
            f"This invocation authorizes ONLY task {task}. Stop after that task; "
            "the gate dispatches the next task after acceptance and independent review. "
            "Never choose a different TODO or alter another task's status. "
            f"Scope: {', '.join(files)}. Work package: {decision['group']}.\n"
        )
        if decision["mode"] == "resume":
            brief += (
                f"Previous task {decision['previous_task']} passed the gate and judge without revision. "
                f"Current HEAD: {decision['before']['head']}. Shared background files and workspace "
                "still match the recorded checkpoint. Reuse the common instructions and code "
                "understanding already in this conversation. Read the CURRENT queue row and "
                "this task's spec/acceptance; read changed or newly relevant source as needed. "
                "Claim, implement, verify in the foreground, and commit this task using the "
                "same project procedure. Previous test results do not validate this task. "
                "Do not reread unchanged general background just to reconstruct it.\n"
            )
        else:
            brief += original + "\n"
        return brief + (
            "Keep large command/test output in a task-specific .gate log file. Report the "
            "exit code, measured summary, and log path; inspect relevant failure lines before "
            "deciding what to do. Never discard errors or return with pending child processes. "
            "Return a concise factual result, not a transcript. Do not push or open a PR.\n"
        )

    @staticmethod
    def read_events(output: str) -> dict:
        evidence = {"session_id": None, "completed": False, "usage": {}}
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "thread.started":
                sid = event.get("thread_id", "")
                if isinstance(sid, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", sid):
                    evidence["session_id"] = sid
            elif event.get("type") == "turn.completed":
                evidence["completed"] = True
                usage = event.get("usage") or {}
                if isinstance(usage, dict):
                    evidence["usage"] = {k: v for k, v in usage.items()
                                         if isinstance(v, int) and not isinstance(v, bool) and v >= 0}
            elif event.get("type") in ("turn.failed", "error"):
                evidence["completed"] = False
        return evidence

    def finish(self, decision: dict, evidence: dict, outcome: str, judge: dict | None,
               dispatches: int, returncode: int, started: float, worker_head: str) -> dict:
        self.previous = None
        summary = {k: decision[k] for k in ("mode", "reason", "group") if k in decision}
        summary.update(session_id=evidence.get("session_id"), usage=evidence.get("usage", {}))
        if decision["mode"] == "off":
            return summary
        verdict = read_verdict(self.repo) or {}
        after = self.snapshot()
        checks = {
            "task_incomplete": outcome == "done",
            "retried": dispatches == 1,
            "worker_exit": returncode == 0,
            "missing_events": evidence.get("completed") and evidence.get("session_id"),
            "wrong_thread": decision["mode"] != "resume" or evidence.get("session_id") == decision["session_id"],
            "not_reviewed_without_revision": judge and judge.get("final") == "pass" and len(judge.get("rounds", [])) == 1,
            "head_changed_after_worker": after["head"] == worker_head,
            "left_changes": after["dirty"] == decision["before"]["dirty"],
            "background_changed": self.background_hash(decision["group"]) == decision["background"],
            "missing_fresh_pass": verdict.get("result") == "PASS" and verdict.get("at_epoch", 0) >= started,
            "missing_usage": "input_tokens" in summary["usage"],
        }
        reusable = all(checks.values())
        summary["checkpoint_reason"] = next((key for key, passed in checks.items() if not passed), "eligible")
        if reusable:
            self.previous = dict(decision, after=after, session_id=evidence["session_id"],
                                 tasks=decision.get("tasks", 0) + 1,
                                 input_tokens=decision.get("input_tokens", 0) + summary["usage"]["input_tokens"])
        summary["reusable"] = bool(
            self.previous and self.previous["tasks"] < int(self.options["max_tasks"])
            and self.previous["input_tokens"] < int(self.options["max_input_tokens"])
        )
        record = {"task": decision["task"], **summary, "checkpoint": self.previous}
        (self.run_dir / "session.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return summary


def cmd_run(args) -> None:
    repo = repo_root(Path(args.repo).resolve())
    try:
        lock = recovery.EngineLock(repo, "gate run").acquire()
    except recovery.RecoveryError as exc:
        die(str(exc), code=3)
    try:
        if recovery.pending_transactions(repo):
            die("incomplete recovery transaction; retry the original recover-task command", code=3)
        _cmd_run_locked(args)
    finally:
        lock.release()


def _cmd_run_locked(args) -> None:
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)
    base_cfg = cfg
    projdir = repo / (cfg.get("project_prefix") or "")
    qpath = repo / prefixed(cfg, cfg["queue_file"])

    run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    started = time.time()
    max_tasks = args.max_tasks or cfg["max_tasks"]

    # ---- preflight: refuse to start blind ------------------------------
    hook_ok, hook_mode = hook_installation(repo)
    if not hook_ok and not args.force:
        die(
            "the pre-commit gate is not installed; an unattended run without it "
            "is exactly the failure this gate exists to prevent. Run install-hook or configure the "
            f"pre-commit chain, then run doctor. Detected: {hook_mode}."
        )
    if not qpath.exists():
        die(f"queue not found: {qpath}")
    # A recovered task owes an independent review, and a DONE row is not
    # evidence that it got one. The barrier outlives this process.
    barriers = recovery.open_barriers(repo)
    if barriers:
        first = sorted(barriers)[0]
        journal(repo, {"event": "review_barrier", "run": run_id, "task": first,
                       "reason": barriers[first].get("reason")})
        die(
            f"{first} is closed but unreviewed ({barriers[first].get('reason')}). "
            f"Run: gate.py review-task --repo {repo} --task {first}",
            code=3,
        )

    initial_text = qpath.read_text(encoding="utf-8")
    initial_tasks = parse_queue(initial_text)
    initial_pins = parse_task_workers(initial_text)
    first, first_state = head_task(cfg, initial_tasks)
    if first and first_state == "todo" and (args.strict_admit or cfg.get("strict_admit")):
        admission = admit_verdicts(cfg, repo, initial_tasks, parse_task_files(initial_text),
                                  initial_pins, parse_task_after(initial_text), parse_task_approved(initial_text))[first]
        if admission["verdict"] != "admit" and not admission.get("needs_approval"):
            reject_run_preflight(repo, cfg, run_id, f"admit_refused:{first}",
                                 f"task {first} refused admission: {admission['reason']}")
    try:
        policies = execution.resolve(cfg, initial_text, parse_queue(initial_text), parse_task_workers(initial_text))
        locked = execution.freeze(repo, policies)
    except execution.PolicyError as exc:
        reject_run_preflight(repo, cfg, run_id, "execution_policy",
                             f"execution preflight refused before any inference: {exc}")

    workers = available_workers(repo, cfg)
    pins = parse_task_workers(qpath.read_text(encoding="utf-8"))
    pinned_available = sorted(
        {w for w in pins.values() if available_workers(repo, cfg, prefer=w)}
    )
    if not workers and not pinned_available:
        die(
            "no usable worker CLI found (checked: "
            + ", ".join(sorted(set(cfg["workers"]) | set(pins.values())))
            + ")"
        )

    print(f"GATE run {run_id}")
    print(f"  project : {projdir}")
    print(f"  queue   : {qpath}")
    print(f"  workers : {', '.join(workers) or '(none run-wide)'}")
    if pins:
        print("  pinned  : " + ", ".join(f"{t}={w}" for t, w in pins.items()))
    print(f"  budget  : {max_tasks} task(s), {cfg['run_timeout_s']}s")
    jcfg = sub_cfg(cfg, "judge")
    print(f"  judge   : {'on — ' + ' → '.join(jcfg['chain']) if jcfg.get('enabled') else 'off'}")
    journal(
        repo,
        {"event": "run_start", "run": run_id, "workers": workers, "pins": pins,
         "judge": bool(jcfg.get("enabled")),
         # Who is running. A later recovery has to name a stopped owner, and
         # an unrecorded owner is one the operator has to observe by hand.
         "pid": os.getpid(), "host": platform.node()},
    )

    # Probe before spending: every candidate CLI once, so a quota-dead worker
    # is benched before it costs a dispatch and a queue-row restore.
    run_dir = repo / GATE_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "execution.json").write_text(json.dumps(locked, indent=2) + "\n", encoding="utf-8")
    results: list[dict] = []
    stop_reason = "budget"
    sessions = CodexTaskSession(repo, cfg, run_dir)

    for slot in range(max_tasks):
        if time.time() - started > cfg["run_timeout_s"]:
            stop_reason = "run_timeout"
            break

        qtext = qpath.read_text(encoding="utf-8")
        tasks = parse_queue(qtext)
        tid, why = head_task(cfg, tasks)
        if tid is None:
            stop_reason = "queue_empty"
            break
        if why == "blocked":
            stop_reason = f"blocked:{tid}"
            print(f"GATE: head task {tid} is BLOCKED — stopping, this needs you.")
            break
        if why == "in_progress":
            stop_reason = f"in_progress:{tid}"
            print(
                f"GATE: head task {tid} is already IN_PROGRESS — another runner may "
                "be active. Stopping instead of taking it over."
            )
            break

        strict = args.strict_admit or cfg.get("strict_admit", False)
        pins = parse_task_workers(qtext)
        pin = pins.get(tid)
        try:
            task_lock = execution.read_lock(repo)
            policy = task_lock["tasks"][tid]
            if policy["pin"] != pin:
                raise execution.PolicyError(f"queue worker changed without an execution lock update: {tid}")
            cfg = execution.bind(base_cfg, policy, tid)
            sessions.cfg = cfg
        except (execution.PolicyError, KeyError) as exc:
            stop_reason = f"execution_policy:{tid}"
            journal(repo, {"event": "execution_refused", "task": tid, "reason": str(exc)})
            break
        (run_dir / f"{tid}.execution.json").write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        journal(repo, {"event": "execution_locked", "task": tid, "sha256": execution.digest(policy)})
        files_map = parse_task_files(qtext)
        task_files = files_map.get(tid) or []
        adm = admit_verdicts(
            cfg, repo, tasks, files_map, pins, parse_task_after(qtext),
            parse_task_approved(qtext),
        ).get(tid, {"verdict": "admit", "reason": ""})
        if adm.get("needs_approval"):
            # Irreversible tier: never a judgement call for the runner, and
            # never downgraded to a warning by advisory mode.
            journal(repo, {"event": "needs_approval", "task": tid, "reason": adm["reason"]})
            stop_reason = f"needs_approval:{tid}"
            print(f"GATE: task {tid} needs your approval ({adm['reason']}) — stopping.")
            break
        if adm["verdict"] != "admit":
            journal(
                repo,
                {"event": "admit_refused", "task": tid, "strict": strict, **adm},
            )
            detail = f"{adm['verdict']}: {adm['reason']}"
            if strict:
                stop_reason = f"admit_refused:{tid}"
                print(
                    f"GATE: task {tid} refused admission ({detail}) — stopping. "
                    "Re-shape the task, or run without strict admission."
                )
                break
            print(
                f"GATE: WARNING task {tid} is outside the sweet spot ({detail}) "
                "— continuing (advisory mode)."
            )

        print(f"\nGATE: task {tid} ({slot + 1}/{max_tasks})")
        head_before = git(repo, "rev-parse", "HEAD").strip()
        recovered = recovery.receipt_for(repo, cfg, tid)
        if recovered is None and any(e.get("task") == tid for e in
                                     recovery._read_ndjson(recovery.index_path(repo), "recovery index")):
            stop_reason = f"recovery_invalid:{tid}"
            print(f"GATE: {tid} has recovery history but no valid receipt; refusing ordinary dispatch")
            break
        attempt_cap = cfg["max_attempts_per_task"]
        budget = None
        if recovered:
            # A recovery does not hand back a fresh attempt budget. It grants
            # exactly one recorded revalidation dispatch on top of the attempts
            # the task already spent, and only once per receipt.
            budget = recovery.revalidation_budget(repo, cfg, tid, recovered)
            if budget["used"] >= budget["allowed"]:
                stop_reason = f"revalidation_exhausted:{tid}"
                journal(repo, {"event": "revalidation_refused", "task": tid, **budget})
                print(
                    f"GATE: {tid} already spent its one revalidation dispatch "
                    f"(receipt {recovered['sha256'][:10]}) — stopping."
                )
                break
            attempt_cap = budget["prior_attempts"] + budget["allowed"]
            print(
                f"  {tid} carries recovery receipt {recovered['sha256'][:10]} for "
                f"implementation {recovered['implementation']['commit'][:10]}; "
                f"{budget['prior_attempts']} prior attempt(s) kept, "
                f"{budget['allowed']} revalidation dispatch allowed"
            )
        # A recovered task carries its spent attempts and its last failure
        # signature forward: the original limit still bounds it, and a repeat
        # of the same failure is still no progress.
        attempts = budget["prior_attempts"] if budget else 0
        dispatches = 0
        outcome = "unknown"
        last_sig = budget["prior_signature"] if budget else None
        last_worker = ""
        session_decision = {"mode": "off", "reason": "not_dispatched", "task": tid}
        session_evidence: dict = {}
        session_returncode, session_started, session_head = -1, time.time(), ""

        while attempts < attempt_cap:
            attempt_queue_text = qpath.read_text(encoding="utf-8")
            attempt_tasks = parse_queue(attempt_queue_text)
            attempt_status = attempt_tasks.get(tid, {}).get("status", "<missing>")
            if attempt_status in cfg["blocked_markers"]:
                outcome = "blocked"
                print(f"  {tid} became BLOCKED — stopping instead of dispatching another task")
                break
            allowed_statuses = set(cfg["todo_markers"] + cfg["in_progress_markers"])
            if attempt_status not in allowed_statuses:
                outcome = "state_changed"
                print(
                    f"  {tid} changed to {attempt_status} before retry — stopping "
                    "instead of selecting another task"
                )
                break

            pool = [
                w for w in available_workers(repo, cfg, prefer=pin)
                if ensure_probed(repo, cfg, w, run_id, run_dir)
            ]
            if not pool:
                outcome = "no_workers"
                break
            # Rotate on retry. A second attempt through the same CLI repeats
            # whatever was wrong with that CLI: a real run once spent both of
            # its attempts on claude while it was returning 529, with codex,
            # grok and kimi installed and idle. Quota benching already rotates; this covers every other
            # tool-shaped failure, which is most of them.
            # A recovered task's one revalidation dispatch goes to the preferred
            # worker: the attempts it carries were not that CLI's failures, so
            # rotating on them would only swap the pinned model for a fallback.
            worker = pool[0] if recovered else pool[attempts % len(pool)]
            last_worker = worker
            dispatches += 1
            attempt_head = git(repo, "rev-parse", "HEAD").strip()
            attempt_worktree = non_gate_worktree_state(repo)

            prompt = cfg["worker_prompt"].format(
                skill=cfg.get("worker_skill") or "the project's next-session",
                task=tid,
            )
            session_decision = sessions.select(tid, worker, attempt_queue_text, dispatches, task_files)
            session_evidence = {}
            session_returncode = -1
            prompt = sessions.prompt(session_decision, prompt, task_files)
            pf = repo / GATE_DIR / "runs" / run_id / f"{tid}-a{dispatches}.prompt.md"
            pf.parent.mkdir(parents=True, exist_ok=True)
            pf.write_text(prompt, encoding="utf-8")

            logp = pf.with_suffix(".log")
            how = (
                "pinned" if pin == worker
                else (f"fallback from {pin}" if pin else "run-wide")
            )
            print(f"  attempt {attempts + 1} via {worker} [{how}] … (live log: {logp})")
            if recovered:
                budget = recovery.revalidation_budget(repo, cfg, tid, recovered)
                if budget["used"] >= budget["allowed"]:
                    outcome = "revalidation_exhausted"
                    break
                # Spend the one revalidation dispatch before the worker starts,
                # and raise the review barrier before it can close anything: a
                # crash between here and the judge must not read as approval.
                recovery.record_revalidation_dispatch(repo, tid, recovered, budget)
                recovery.raise_barrier(
                    repo, tid, recovered["sha256"],
                    f"recovered implementation {recovered['implementation']['commit'][:10]} "
                    "closed without a recorded judge pass",
                )
            journal(
                repo,
                {
                    "event": "attempt_start",
                    "task": tid,
                    "worker": worker,
                    "pid": os.getpid(),
                    "pin": pin,
                    "pinned": bool(pin) and worker == pin,
                    "attempt": attempts + 1,
                    "dispatch": dispatches,
                    "log": str(logp),
                    "session_mode": session_decision["mode"],
                    "session_reason": session_decision["reason"],
                    "session_before": session_decision.get("before"),
                    "session_background": session_decision.get("background"),
                },
            )
            notify(cfg, repo, f"{tid} started (attempt {attempts + 1}, {worker})")

            def beat(secs: int, lines: int, last: str, _tid=tid, _w=worker) -> None:
                mins = secs // 60
                print(f"    … {_tid} running {mins}m, {lines} lines | {last[:80]}")
                journal(
                    repo,
                    {
                        "event": "heartbeat",
                        "task": _tid,
                        "worker": _w,
                        "seconds": secs,
                        "lines": lines,
                        "last_line": last,
                    },
                )

            t0 = time.time()
            session_started = t0
            try:
                session_options = {}
                if session_decision["mode"] != "off":
                    session_options["template"] = sessions.template(session_decision)
                proc, err = spawn_worker(
                    repo, cfg, worker, prompt, pf, log_path=logp, on_beat=beat, **session_options
                )
            except subprocess.TimeoutExpired:
                proc, err = None, "timeout"
            took = round(time.time() - t0, 1)
            if proc is not None and getattr(proc, "timed_out", False):
                print(f"  timed out after {took}s — worker tree killed")
                journal(repo, {"event": "attempt_timeout", "task": tid, "worker": worker})
                notify(cfg, repo, f"{tid} timed out after {int(took // 60)}m")
                attempts += 1
                outcome = "timeout"
                continue

            if proc is None:
                print(f"  worker unusable: {err}")
                journal(
                    repo,
                    {"event": "worker_error", "task": tid, "worker": worker, "err": err},
                )
                if err == "timeout":
                    attempts += 1
                    outcome = "timeout"
                    continue
                outcome = "worker_error"
                break

            out = (proc.stdout or "") + (proc.stderr or "")  # already streamed to logp
            if getattr(proc, "execution_mismatch", False):
                outcome = "execution_mismatch"
                break
            session_returncode = proc.returncode
            session_evidence = sessions.read_events(out) if session_decision["mode"] != "off" else {}
            session_head = git(repo, "rev-parse", "HEAD").strip()

            # Tool outages are not task failures. A model CLI may itself exit
            # zero after a nested Git command fails, so classify its transcript
            # regardless of return code. Restore only the dispatched task row;
            # never overwrite concurrent changes elsewhere in the queue.
            outage = tool_outage_reason(out, proc.returncode)
            if outage:
                outage_head = git(repo, "rev-parse", "HEAD").strip()
                if outage_head != attempt_head:
                    outcome = "tool_state_conflict"
                    print(
                        f"  {worker} reported {outage} after moving HEAD — "
                        "stopping for inspection"
                    )
                    break
                try:
                    restored = restore_task_row_text(
                        attempt_queue_text,
                        qpath.read_text(encoding="utf-8"),
                        tid,
                    )
                except ValueError as exc:
                    outcome = "task_mismatch"
                    print(f"  cannot restore {tid} after {outage}: {exc}")
                    break
                qpath.write_text(restored, encoding="utf-8")

                unexpected = {
                    task_id: change
                    for task_id, change in queue_status_changes(
                        attempt_queue_text, restored
                    ).items()
                    if task_id != tid
                }
                dirty_after = non_gate_worktree_state(repo)
                if unexpected:
                    outcome = "task_mismatch"
                    print(
                        f"  {worker} changed other task statuses during {tid}: "
                        f"{unexpected}"
                    )
                    break
                if dirty_after != attempt_worktree:
                    outcome = "tool_left_changes"
                    print(
                        f"  {worker} left non-queue worktree changes after {outage}; "
                        "stopping instead of handing a dirty attempt to another worker"
                    )
                    break

                disable_worker(repo, cfg, worker, outage)
                print(
                    f"  {worker} unavailable ({outage}) — restored {tid}, "
                    "disabled worker, trying the next tool"
                )
                journal(
                    repo,
                    {
                        "event": "tool_disabled",
                        "task": tid,
                        "worker": worker,
                        "reason": outage,
                        "restored_task": True,
                    },
                )
                continue

            attempts += 1

            # ---- the verdict is ours, not the worker's --------------------
            head_after = git(repo, "rev-parse", "HEAD").strip()
            queue_after_text = qpath.read_text(encoding="utf-8")
            tasks_after = parse_queue(queue_after_text)
            task_status = tasks_after.get(tid, {}).get("status", "<missing>")
            now_done = task_status in cfg["done_markers"]
            advanced = head_after != head_before
            completed = newly_done(repo, cfg, head_before, head_after) if advanced else []
            unexpected_done = [task_id for task_id in completed if task_id != tid]
            unexpected_status = {
                task_id: change
                for task_id, change in queue_status_changes(
                    attempt_queue_text, queue_after_text
                ).items()
                if task_id != tid
            }

            if unexpected_done or unexpected_status:
                outcome = "task_mismatch"
                print(
                    f"  dispatched {tid}, but other tasks changed "
                    f"(DONE={unexpected_done}, statuses={unexpected_status}) — stopping"
                )
                journal(
                    repo,
                    {
                        "event": "task_mismatch",
                        "task": tid,
                        "worker": worker,
                        "unexpected_done": unexpected_done,
                        "unexpected_status": unexpected_status,
                    },
                )
                break

            if now_done and completed == [tid]:
                outcome = "done"
                print(f"  {tid} DONE in {took}s via {worker} ({head_after[:10]})")
                journal(
                    repo,
                    {
                        "event": "task_done",
                        "task": tid,
                        "worker": worker,
                        "commit": head_after,
                        "seconds": took,
                    },
                )
                vv = read_verdict(repo)
                notify(
                    cfg,
                    repo,
                    f"{tid} DONE in {int(took // 60)}m ({worker})",
                    f"commit {head_after[:10]}, acceptance count "
                    f"{vv.get('count') if vv else '?'}",
                )
                break

            if task_status in cfg["blocked_markers"]:
                outcome = "blocked"
                print(f"  {tid} reported a real blocker — stopping without retry")
                break

            if task_status not in allowed_statuses:
                outcome = "state_changed"
                print(f"  {tid} ended attempt in unexpected state {task_status} — stopping")
                break

            incomplete = worker_incomplete_reason(out)
            if incomplete:
                outcome = "worker_incomplete"
                print(
                    f"  {worker} returned while task work was still pending "
                    f"({incomplete}) — stopping without a concurrent retry"
                )
                journal(
                    repo,
                    {
                        "event": "worker_incomplete",
                        "task": tid,
                        "worker": worker,
                        "reason": incomplete,
                    },
                )
                break

            dirty_after = non_gate_worktree_state(repo)
            if dirty_after != attempt_worktree:
                outcome = "worker_left_changes"
                print(
                    f"  {worker} exited without closing {tid} and changed the worktree — "
                    "stopping instead of dispatching another worker into partial work"
                )
                break

            sig = f"{proc.returncode}:{out.strip().splitlines()[-1][:120] if out.strip() else ''}"
            if sig == last_sig:
                outcome = "no_progress"
                print("  same failure twice — stopping this task rather than repeating")
                break
            last_sig = sig
            outcome = "not_done"
            print(
                f"  attempt {attempts} did not close {tid} "
                f"(exit={proc.returncode}, commit {'moved' if advanced else 'unchanged'})"
            )

        judge_info: dict | None = None
        if outcome == "done" and jcfg.get("enabled"):
            # Named only when there is something to add, so an ordinary task's
            # review is the same call it has always been.
            extra = recovery.judge_extra_commits(
                repo, recovered, git(repo, "rev-parse", "HEAD").strip()
            )
            judge_info = judge_task(
                repo, cfg, run_id, run_dir, tid, head_before, task_files,
                last_worker, pin, **({"extra_commits": extra} if extra else {}),
            )
        # Resumed code is still unreviewed code. A recovered task closes on an
        # implementation no judge has seen, so a disabled, benched or
        # unparsable judge is escalated here instead of passing silently.
        if outcome == "done" and recovered and (
            judge_info is None or judge_info["final"] == "skipped"
        ):
            judge_info = _escalate(
                repo, tid, (judge_info or {}).get("rounds", []),
                "recovered task needs an independent review: "
                + ("judge disabled" if judge_info is None else judge_info["reason"]),
            )
        if recovered and outcome != "done":
            # The dispatch closed nothing: retire the barrier it raised, or no
            # later run could ever start. A DONE row is never retired here —
            # open_barriers reconstructs that obligation from the history.
            recovery.retire_dispatch_barrier(
                repo, cfg, tid, f"revalidation dispatch ended {outcome}; the row is not DONE"
            )
        if recovered and judge_info and judge_info["final"] == "pass":
            rounds = judge_info["rounds"]
            recovery.clear_barrier(
                repo, tid, "judge",
                f"round {len(rounds)} scored {rounds[-1]['score']} "
                f"({rounds[-1]['member']}) on {recovered['implementation']['commit'][:10]}"
                if rounds else "judge passed",
            )

        session_info = sessions.finish(session_decision, session_evidence, outcome, judge_info,
                                       dispatches, session_returncode, session_started, session_head)
        if session_decision["mode"] != "off":
            journal(repo, {"event": "session_checkpoint", "task": tid, **session_info})

        results.append(
            {
                "task": tid,
                "outcome": outcome,
                "attempts": attempts,
                "dispatches": dispatches,
                "run": run_id,
                "worker": last_worker,
                "pin": pin or "",
                "judge": judge_info,
                "session": session_info,
            }
        )
        journal(repo, {"event": "task_end", "task": tid, "outcome": outcome,
                       "judge": (judge_info or {}).get("final"),
                       "attempts": attempts, "signature": last_sig})
        if outcome != "done":
            notify(
                cfg,
                repo,
                f"{tid} stopped: {outcome}",
                f"log: .gate/runs/{run_id}/{tid}-a{max(dispatches, 1)}.prompt.log",
            )

        if outcome != "done":
            stop_reason = f"{outcome}:{tid}"
            break
        if judge_info and judge_info["final"] == "escalated":
            stop_reason = f"judge_escalated:{tid}"
            break
        if judge_info and judge_info["final"] == "broke_verify":
            stop_reason = f"revision_broke_verify:{tid}"
            break

    elapsed = round(time.time() - started, 1)
    report = render_report(repo, cfg, run_id, results, stop_reason, elapsed)
    (repo / GATE_DIR / f"RUN-REPORT-{run_id}.md").write_text(report, encoding="utf-8")
    latest = repo / GATE_DIR / "RUN-REPORT.md"
    latest.write_text(report, encoding="utf-8")
    journal(repo, {"event": "run_end", "run": run_id, "stop": stop_reason,
                   "pid": os.getpid(), "host": platform.node()})

    print("\n" + report)

    done = sum(1 for r in results if r["outcome"] == "done")
    notify(
        cfg,
        repo,
        f"run {run_id} finished: {done}/{len(results)} done ({stop_reason})",
        report,
    )

    needs_human = stop_reason.split(":", 1)[0] in (
        "judge_escalated", "revision_broke_verify", "needs_approval", "execution_policy", "execution_mismatch"
    )
    if any(r["outcome"] != "done" for r in results) or not results or needs_human:
        raise SystemExit(3)


def render_report(
    repo: Path, cfg: dict, run_id: str, results: list[dict], stop: str, elapsed: float
) -> str:
    lines = [
        f"# Run report {run_id}",
        "",
        f"- elapsed: {elapsed}s",
        f"- stopped because: **{stop}**",
        "",
        "| task | outcome | task attempts | worker dispatches | worker | pin |",
        "|---|---|---:|---:|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['task']} | {r['outcome']} | {r['attempts']} | "
            f"{r.get('dispatches', r['attempts'])} | {r.get('worker', '')} | "
            f"{r.get('pin', '')} |"
        )
    if not results:
        lines.append("| — | nothing ran | 0 | 0 |")

    session_rows = [r for r in results if r.get("session", {}).get("mode", "off") != "off"]
    if session_rows:
        lines += ["", "## Sessions", "",
                  "| task | mode | reason | input tokens | cached input | reusable | checkpoint |",
                  "|---|---|---|---:|---:|---|---|"]
        for r in session_rows:
            s = r["session"]
            usage = s.get("usage", {})
            lines.append(f"| {r['task']} | {s['mode']} | {s['reason']} | "
                         f"{usage.get('input_tokens', 'unknown')} | "
                         f"{usage.get('cached_input_tokens', 'unknown')} | {s.get('reusable', False)} | "
                         f"{s.get('checkpoint_reason', 'unknown')} |")

    # Judge: rounds, scores, and what each round's worker was. A skipped
    # judge is printed loudly — quality silently switched off is the failure.
    judged = [r for r in results if r.get("judge")]
    if judged:
        lines += ["", "## Judge", "",
                  "| task | final | rounds | scores | workers | reason |",
                  "|---|---|---:|---|---|---|"]
        for r in judged:
            j = r["judge"]
            scores = " → ".join(str(x["score"]) for x in j["rounds"]) or "—"
            workers_used = ", ".join(
                x["worker"] + ("" if x.get("rotated", True) else " (not rotated)")
                for x in j["rounds"]
            ) or "—"
            lines.append(
                f"| {r['task']} | {j['final']} | {len(j['rounds'])} | {scores} | "
                f"{workers_used} | {j.get('reason', '')} |"
            )
        for r in judged:
            j = r["judge"]
            if j["final"] in ("escalated", "broke_verify") and j["rounds"]:
                last = j["rounds"][-1]
                lines += ["", f"### {r['task']} — last findings (round {last['round']}, "
                          f"{last['member']})", "", format_findings(last["findings"])]
                if last.get("brief"):
                    lines += ["", f"Brief: {last['brief']}"]

    st = tool_state(repo)
    now = time.time()
    disabled = [
        f"{k} (until {time.strftime('%H:%M UTC', time.gmtime(v['disabled_until']))}, "
        f"{v.get('reason')})"
        for k, v in st.items()
        if v.get("disabled_until", 0) > now
    ]
    lines += ["", "## Tools", ""]
    lines.append(
        "- disabled: " + (", ".join(disabled) if disabled else "none") + ""
    )

    # A failed attempt usually leaves edits behind. Say so — an uncommitted
    # tree is the thing most likely to confuse the next run.
    pre = (cfg.get("project_prefix") or "").strip("/")
    dirty = [
        ln
        for ln in git(repo, "status", "--porcelain", "--", pre or ".").splitlines()
        if ln.strip() and GATE_DIR not in ln
    ]
    if dirty:
        lines += ["", "## Uncommitted changes left in the project", "", "```"]
        lines += dirty[:40]
        if len(dirty) > 40:
            lines.append(f"... {len(dirty) - 40} more")
        lines += ["```", "", "Review these before starting another run."]

    v = read_verdict(repo)
    if v:
        lines += [
            "",
            "## Last acceptance run",
            "",
            f"- `{v['cmd']}` → **{v['result']}** (exit {v['exit_code']}, count {v.get('count')})",
        ]

    # The only section the user has to read. Empty on a clean run.
    waiting: list[str] = []
    if stop.startswith("needs_approval:"):
        tid = stop.split(":", 1)[1]
        waiting.append(
            f"- **{tid}** touches an irreversible path. Review its scope, then add "
            f"`<!-- task:{tid} approved: <who/date> -->` to the queue and start another run."
        )
    if stop.startswith("judge_escalated:"):
        tid = stop.split(":", 1)[1]
        waiting.append(
            f"- **{tid}** is DONE by the acceptance gate but the judge could not get it to "
            "the pass score. Read the Judge section; decide fix / accept / re-shape."
        )
    if stop.startswith("revision_broke_verify:"):
        tid = stop.split(":", 1)[1]
        waiting.append(
            f"- **{tid}**: a judge-driven revision left the tree failing acceptance or "
            "dirty. Inspect `git log`/`git status` before any further run."
        )
    for r in results:
        j = r.get("judge")
        if j and j["final"] == "skipped":
            waiting.append(
                f"- **{r['task']}** closed without a judge review ({j.get('reason')}). "
                "Not blocking; spot-check it."
            )
    lines += ["", "## Waiting on you", ""]
    lines += waiting or ["- nothing"]

    lines += [
        "",
        "## What to do next",
        "",
    ]
    if stop.startswith("blocked:"):
        lines.append(
            f"- The head task {stop.split(':', 1)[1]} is BLOCKED. Answer its blocking "
            "questions in the queue, set it to TODO, and start another run."
        )
    elif stop.startswith("in_progress:"):
        lines.append(
            "- The head task is IN_PROGRESS. Either another runner is active, or a "
            "previous one died mid-task. Check `git status` before restarting."
        )
    elif stop == "queue_empty":
        lines.append("- Nothing left to do. The queue has no eligible task.")
    elif stop in ("budget", "run_timeout"):
        lines.append("- Budget reached. Start another run to continue.")
    else:
        lines.append(
            "- A task did not close. Its prompt and full worker log are under "
            f"`.gate/runs/{run_id}/`. Read the log before re-running."
        )
    lines.append("- Re-derive the structural verdict any time: `gate.py audit --last 30`.")
    return "\n".join(lines) + "\n"


def cmd_recover_task(args) -> None:
    """Return one stopped claim to TODO, with the evidence that it is one.

    Refuses everything it cannot prove: a live claim, an open run, a stop that
    named another task, an implementation that is not an ancestor, changed
    declared scope or content, staged or dirty declared source. It never
    writes DONE, never commits, and never touches another row.
    """
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)
    missing = [
        flag for flag, value in (
            ("--task", args.task),
            ("--implementation", args.implementation),
            ("--run", args.run),
            ("--reason", args.reason),
        ) if not value
    ]
    if missing:
        die("recover-task needs " + ", ".join(missing))
    stopped_pid = None
    if args.stopped_pid:
        try:
            stopped_pid = int(args.stopped_pid)
        except ValueError:
            die("--stopped-pid must be the numeric pid of the runner you observed stopped")
    evidence_args = {
        "stopped_pid": stopped_pid,
        "requirement_mode": args.requirement_mode or "headings",
        "requirement_path": args.requirement,
    }
    try:
        if args.dry_run:
            proposal = recovery.plan(
                repo, cfg, args.task, args.implementation, args.run, args.reason,
                **evidence_args
            )
            print(json.dumps(proposal, indent=2, ensure_ascii=False))
            print(
                f"GATE: {args.task} is recoverable "
                f"({proposal['queue_transition']['from']} → {proposal['queue_transition']['to']}). "
                "Nothing was written."
            )
            return
        receipt = recovery.recover(
            repo, cfg, args.task, args.implementation, args.run, args.reason,
            args.note or "", **evidence_args
        )
    except recovery.RecoveryError as exc:
        journal(repo, {"event": "recovery_refused", "task": args.task,
                       "implementation": args.implementation, "run": args.run,
                       "reason": str(exc), "dry_run": bool(args.dry_run)})
        die(f"recovery refused: {exc}")
    queue = receipt["scope"]["queue_file"]
    print(
        f"GATE: {args.task} {receipt['queue_transition']['from']} → "
        f"{receipt['queue_transition']['to']} in {queue} (worktree only, not committed)"
    )
    print(f"  implementation : {receipt['implementation']['commit']}")
    print(f"  declared       : {', '.join(receipt['scope']['declared_files'])}")
    print(f"  requirement    : {', '.join(receipt['requirement']['sources']) or '(row only)'}"
          f" [{receipt['requirement']['mode']}]")
    print(f"  stopped owner  : pid {receipt['ownership']['owner_pid']} "
          f"({receipt['ownership']['owner_source']}), census "
          f"{receipt['ownership']['census_size']} process(es)")
    print(f"  receipt        : {recovery.receipt_path(repo, receipt['sha256'])}")
    print(
        "  next           : commit that one row, then run the task normally. "
        "Gate 1 will accept its queue-only DONE only after a fresh PASS from "
        f"{cfg['verify_cmd']!r} over these same blobs."
    )


def cmd_renew_revalidation(args) -> None:
    """One more revalidation dispatch for a lineage whose only dispatch passed
    the oracle and was then refused at the gate. Evidenced, journalled, once."""
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)
    if not args.task:
        die("renew-revalidation needs --task")
    try:
        grant = recovery.renew_revalidation(repo, cfg, args.task, args.reason or "")
    except recovery.RecoveryError as exc:
        journal(repo, {"event": "renewal_refused", "task": args.task, "reason": str(exc)})
        die(f"renewal refused: {exc}")
    print(f"GATE: {args.task} granted one more revalidation dispatch "
          f"(lineage {grant['implementation'][:10]}); recover the stopped claim, then run.")


def cmd_review_task(args) -> None:
    """Independent review only, under the same lock and persistent budgets."""
    repo = repo_root(Path(args.repo).resolve())
    try:
        lock = recovery.EngineLock(repo, "review task").acquire()
    except recovery.RecoveryError as exc:
        die(str(exc), code=3)
    try:
        _cmd_review_task_locked(args, repo)
    finally:
        lock.release()


def _cmd_review_task_locked(args, repo) -> None:
    cfg = load_config(repo)
    if not args.task:
        die("review-task needs --task")
    barriers = recovery.open_barriers(repo)
    receipt = None
    if args.task in barriers:
        receipt = recovery.receipt_for(repo, cfg, args.task)
        if receipt is None:
            die(f"no valid recovery receipt for {args.task}; nothing to review against")
    elif not (args.base and args.closure):
        print(
            f"GATE: {args.task} has no open review barrier. To review an ordinary DONE "
            "task whose judge was skipped, name its commits: --base <sha before the "
            "task> --closure <its closing commit>."
        )
        return
    jcfg = sub_cfg(cfg, "judge")
    if not jcfg.get("enabled"):
        die("judge.enabled is false; independent review is required.")
    head = git(repo, "rev-parse", "HEAD").strip()
    base = closure = None
    commits: list[str] = []
    if receipt is None:
        # An ordinary task: the review names exactly its commits. Later tasks
        # may already sit on top, so the range is pinned, not "since base".
        base = git(repo, "rev-parse", "--verify", f"{args.base}^{{commit}}").strip()
        closure = git(repo, "rev-parse", "--verify", f"{args.closure}^{{commit}}").strip()
        if subprocess.run(["git", "merge-base", "--is-ancestor", closure, head],
                          cwd=str(repo)).returncode != 0:
            die("closure must be an ancestor of HEAD")
        commits = [s for s in git(repo, "rev-list", f"{base}..{closure}").split() if s]
        if not commits:
            die(f"no commits in {base[:10]}..{closure[:10]}")
        if args.task not in newly_done(repo, cfg, base, closure):
            die(f"{args.task} does not flip to DONE between {base[:10]} and {closure[:10]}")
    run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-review"
    run_dir = repo / GATE_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        policy = execution.read_lock(repo)["tasks"][args.task]
        cfg = execution.bind(cfg, policy, args.task)
    except (execution.PolicyError, KeyError) as exc:
        die(f"execution profile for {args.task} is not locked: {exc}")
    text = (repo / prefixed(cfg, cfg["queue_file"])).read_text(encoding="utf-8")
    if receipt is None:
        journal(repo, {"event": "review_requested", "task": args.task, "base": base,
                       "closure": closure, "commits": commits, "pid": os.getpid()})
        info = judge_task(
            repo, cfg, run_id, run_dir, args.task, head,
            parse_task_files(text).get(args.task) or [],
            "", parse_task_workers(text).get(args.task),
            extra_commits=commits,
        )
        if info["final"] == "pass":
            print(f"GATE: {args.task} reviewed: pass.")
            return
        die(f"{args.task} review did not pass: {info['final']} ({info['reason']})", code=3)
    info = judge_task(
        repo, cfg, run_id, run_dir, args.task, receipt["implementation"]["parent"],
        parse_task_files(text).get(args.task) or receipt["scope"]["declared_files"],
        "", parse_task_workers(text).get(args.task),
        extra_commits=recovery.judge_extra_commits(repo, receipt, head),
    )
    if info["final"] == "pass":
        rounds = info["rounds"]
        recovery.clear_barrier(
            repo, args.task, "judge",
            f"round {len(rounds)} scored {rounds[-1]['score']} ({rounds[-1]['member']})"
            if rounds else "judge passed",
        )
        print(f"GATE: {args.task} reviewed and cleared.")
        return
    die(f"{args.task} remains unreviewed: {info['final']} ({info['reason']})", code=3)


def cmd_doctor(args) -> None:
    repo = repo_root(Path(args.repo).resolve())
    print(f"repo            : {repo}")
    cfgp = Path(os.environ["GATE_CONFIG"]) if os.environ.get("GATE_CONFIG") else repo / GATE_DIR / CONFIG_NAME
    print(f"config          : {cfgp} {'OK' if cfgp.exists() else 'MISSING'}")
    if not cfgp.exists():
        return
    cfg = load_config(repo)
    print(f"project_prefix  : {cfg.get('project_prefix') or '(whole repo)'}")
    qf = repo / prefixed(cfg, cfg["queue_file"])
    print(f"queue           : {qf} {'OK' if qf.exists() else 'MISSING'}")
    if qf.exists():
        tasks = parse_queue(qf.read_text(encoding="utf-8"))
        counts: dict[str, int] = {}
        for t in tasks.values():
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        print(f"tasks parsed    : {len(tasks)}  {counts}")
        pins = parse_task_workers(qf.read_text(encoding="utf-8"))
        todo_pins = {
            t: w
            for t, w in pins.items()
            if tasks.get(t, {}).get("status") in cfg["todo_markers"]
        }
        if todo_pins:
            print(f"worker pins     : {todo_pins}")
        try:
            policies = execution.resolve(cfg, qf.read_text(encoding="utf-8"), tasks, pins)
            print(f"execution       : {len(policies)} explicit task profiles OK")
        except execution.PolicyError as exc:
            print(f"execution       : MISSING/INVALID ({exc})")
    installed, hook_mode = hook_installation(repo)
    print(f"pre-commit gate : {'INSTALLED' if installed else 'not installed'} ({hook_mode})")
    print(f"verify_cmd      : {cfg['verify_cmd']}")
    v = read_verdict(repo)
    if v:
        age = int(time.time() - float(v.get("at_epoch", 0)))
        print(f"last verdict    : {v['result']} count={v.get('count')} age={age}s")
    else:
        print("last verdict    : none")


# recovery.py needs this module's queue parsing, path resolution and journal,
# and this module is loaded under several names (__main__ from the hook, a
# throwaway name from importlib in the tests). Handing over the namespace binds
# the copy that is actually running instead of importing a second one.
recovery.bind(globals())


def main() -> None:
    ap = argparse.ArgumentParser(prog="gate.py", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn, extra in [
        ("init", cmd_init, [("--force", "store_true")]),
        ("install-hook", cmd_install_hook, [("--force", "store_true")]),
        ("check-commit", cmd_check_commit, []),
        ("verify", cmd_verify, [("--cmd", "str")]),
        ("audit", cmd_audit, [("--last", "int")]),
        ("admit", cmd_admit, []),
        ("recover-task", cmd_recover_task, [
            ("--task", "str"),
            ("--implementation", "str"),
            ("--run", "str"),
            ("--reason", "str"),
            ("--note", "str"),
            ("--stopped-pid", "str"),
            ("--requirement", "str"),
            ("--requirement-mode", "str"),
            ("--dry-run", "store_true"),
        ]),
        ("review-task", cmd_review_task, [("--task", "str"), ("--base", "str"), ("--closure", "str")]),
        ("renew-revalidation", cmd_renew_revalidation, [("--task", "str"), ("--reason", "str")]),
        ("lock-execution", cmd_lock_execution, [("--reason", "str")]),
        ("usage", cmd_usage, []),
        ("doctor", cmd_doctor, []),
        ("run", cmd_run, [
            ("--max-tasks", "int"),
            ("--force", "store_true"),
            ("--strict-admit", "store_true"),
        ]),
    ]:
        p = sub.add_parser(name)
        p.add_argument("--repo", default=".")
        for flag, kind in extra:
            if kind == "store_true":
                p.add_argument(flag, action="store_true")
            elif kind == "int":
                # --last defaults to 30; --max-tasks falls back to the config.
                p.add_argument(flag, type=int, default=30 if flag == "--last" else None)
            else:
                p.add_argument(flag, default=None)
        p.set_defaults(func=fn)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
