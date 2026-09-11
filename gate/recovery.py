#!/usr/bin/env python3
"""
recovery.py — supported recovery of one stopped task claim.

A worker can die between "the implementation is committed" and "the queue row
says DONE". The code is in history, the row is still IN_PROGRESS, and the only
commit left to make is queue-only — exactly the shape Gate 1 refuses, and must
keep refusing, because a fabricated completion looks the same from the outside.

The difference between the two is evidence, so recovery produces evidence.
`gate.py recover-task` proves the claim is nobody's (OS lock + process census +
a named stopped owner), proves the prior implementation answers the task that
is still in the queue (declared blobs, row cells, the task's own acceptance
section), records one immutable receipt, and returns that one row to TODO.
Never to DONE. The task is then claimed, verified and reviewed again.

What the receipt buys is narrow and revalidated on every use: Gate 1 accepts a
queue-only DONE only when the receipt still passes its whole evidence chain,
the acceptance inputs captured before *and* after the oracle are identical and
still match the tree being committed, and the review barrier the runner raised
has been cleared by a judge. Every other gate still applies.

Nothing here deletes or rewrites anything. Receipts, the index, completions and
barriers are append-only; the queue moves by atomic replace; locks are OS
advisory locks that the kernel drops when a process dies, so they are never
stale and never deleted. Every check fails closed.
"""

from __future__ import annotations

import atexit
import calendar
import hashlib
import json
import os
import re
import subprocess
import time
import threading
from pathlib import Path

import execution

SCHEMA_VERSION = 2
GATE_DIR = ".gate"
RECOVERY_DIR = "recovery"
ENGINE_LOCK = "engine.lock"
ENGINE_LOCK_META = "engine.lock.json"
BARRIER_FILE = "review-barriers.ndjson"

# Only these stop reasons describe "the work is committed, the closure is not".
# A run that stopped because the worker made no progress, timed out, or left
# the tree dirty is not evidence about the work, and recovery must not turn it
# into a fresh attempt budget.
RECOVERABLE_OUTCOMES = ("not_done",)

# The configuration a verdict's meaning depends on. A receipt is bound to these
# values, so a queue-only DONE cannot be accepted under a weakened oracle, a
# different count regex, or a widened doc_only list.
VERIFY_CONFIG_FIELDS = (
    "verify_cmd",
    "verify_timeout_s",
    "count_regex",
    "frozen_globs",
    "doc_only_globs",
    "project_prefix",
    "queue_file",
    "done_markers",
    "todo_markers",
    "in_progress_markers",
    "blocked_markers",
    "verdict_max_age_s",
    "require_verdict_on_done",
    "quality",
    "recovery_inputs",
    "acceptance_globs",
)

# Runner-owned churn under .gate/, matched by exact path or directory prefix —
# never by substring, so a project file such as src/runtime.gate.py is bound
# like any other source. Everything else in the tree, including
# .gate/config.json, is part of what the oracle ran over.
GATE_ARTIFACT_PATHS = frozenset({
    f"{GATE_DIR}/verdict.json",
    f"{GATE_DIR}/verdict.tmp",
    f"{GATE_DIR}/journal.ndjson",
    f"{GATE_DIR}/tool-status.json",
    f"{GATE_DIR}/{ENGINE_LOCK}",
    f"{GATE_DIR}/{ENGINE_LOCK_META}",
    f"{GATE_DIR}/{BARRIER_FILE}",
    f"{GATE_DIR}/execution-lock.json",
    f"{GATE_DIR}/execution-changes.ndjson",
    f"{GATE_DIR}/RUN-REPORT.md",
    f"{GATE_DIR}/supervisor.log",
})
GATE_ARTIFACT_PREFIXES = (
    f"{GATE_DIR}/runs/",
    f"{GATE_DIR}/{RECOVERY_DIR}/",
    f"{GATE_DIR}/execution-locks/",
    f"{GATE_DIR}/RUN-REPORT-",
)

DEFAULT_ACCEPTANCE_GLOBS = ("docs/work/**/PLAN.md", "docs/work/**/spec.md")


class RecoveryError(ValueError):
    """A refusal. Every one of these means: not proven, therefore not allowed."""


# ----------------------------------------------------------- host binding

# gate.py is loaded under several names (``__main__`` from the hook, a throwaway
# name from importlib in the tests), so importing it back here would execute a
# second copy with its own state. It hands us its own namespace instead.
_gate: dict | None = None


def bind(namespace: dict) -> None:
    global _gate
    _gate = namespace


def _g(name: str):
    if _gate is None or name not in _gate:
        raise RecoveryError("recovery is not bound to gate.py")
    return _gate[name]


def _parse_queue(text: str) -> dict:
    return _g("parse_queue")(text)


def _parse_task_files(text: str) -> dict:
    return _g("parse_task_files")(text)


def _parse_task_after(text: str) -> dict:
    return _g("parse_task_after")(text)


def _parse_task_workers(text: str) -> dict:
    return _g("parse_task_workers")(text)


def _prefixed(cfg: dict, rel: str) -> str:
    return _g("prefixed")(cfg, rel)


def _project_rel(cfg: dict, path: str) -> str:
    return _g("project_rel")(cfg, path)


def _in_project(cfg: dict, path: str) -> bool:
    return _g("in_project")(cfg, path)


def _matches_any(path: str, globs: list) -> bool:
    return _g("matches_any")(path, globs)


def _blob(repo: Path, ref: str, path: str):
    return _g("blob")(repo, ref, path)


def _row_task_id(line: str):
    return _g("_task_id_from_row")(line)


def _queue_status_changes(before: str, after: str) -> dict:
    return _g("queue_status_changes")(before, after)


def _journal(repo: Path, event: dict) -> None:
    _g("journal")(repo, event)


def _read_verdict(repo: Path):
    return _g("read_verdict")(repo)


def _end_baseline_for(repo: Path, cfg: dict, ref: str, task: str):
    return _g("end_baseline_for")(repo, cfg, ref, task)


# ------------------------------------------------------------------- git


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise RecoveryError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def _out(repo: Path, *args: str) -> str:
    return _git(repo, *args).stdout.strip()


def _git_bytes(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True)
    if proc.returncode != 0:
        raise RecoveryError(
            f"git {' '.join(args)} failed: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout


def digest(value: object) -> str:
    # Preserve surrogateescaped filename bytes on POSIX as well as Unicode.
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _epoch(stamp: str) -> float:
    try:
        return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError) as exc:
        raise RecoveryError(f"unreadable journal timestamp {stamp!r}: {exc}") from exc


# ------------------------------------------------------- process ownership


def process_alive(pid: int) -> bool:
    """True unless the process is provably gone. Queries only; never signals.

    Every uncertain answer is 'alive', because refusing a recovery costs a
    message and taking one over from a live writer costs the repository. On
    Windows this must never go through os.kill: for a non-CTRL signal CPython
    calls TerminateProcess, so a naive liveness probe kills what it probes.

    Known limitation: a recycled PID reads as alive, which refuses. No recovery
    is ever allowed on the strength of a PID that cannot be resolved.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            handle = kernel32.OpenProcess(query_limited_information, False, pid)
            if not handle:
                # 87 ERROR_INVALID_PARAMETER is the only "no such process"
                # answer; 5 ERROR_ACCESS_DENIED means it exists and is not ours.
                return ctypes.get_last_error() != 87
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


CENSUS_PS1 = (
    "Get-CimInstance Win32_Process -ErrorAction Stop | ForEach-Object { "
    "\"$($_.ProcessId)|$($_.ParentProcessId)|$($_.CommandLine -replace '[\\r\\n]+', ' ')\" }"
)
CENSUS_ROW_RE = re.compile(r"^(\d+)\|(\d+)\|(.*)$")


def process_census() -> list[tuple[int, int, str]]:
    """Every process on this host as (pid, ppid, command line). Fails closed.

    An empty or unreadable census is never 'nobody is running': it is a refusal.
    The repository path is not passed to the census command, so the census
    process cannot match itself.
    """
    if os.name == "nt":
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-Command", CENSUS_PS1]
    else:
        argv = ["ps", "-eo", "pid=,ppid=,args="]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryError(
            f"the process census could not run ({exc}); ownership cannot be proved"
        ) from exc
    if proc.returncode != 0:
        raise RecoveryError(
            f"the process census failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}"
        )
    rows: list[tuple[int, int, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if os.name == "nt":
            match = CENSUS_ROW_RE.match(line)
            if not match:
                # A command line that still broke across lines. Append it to the
                # row it belongs to: a longer command can only match more, never
                # less, so this cannot hide a writer.
                if rows:
                    pid, ppid, command = rows[-1]
                    rows[-1] = (pid, ppid, f"{command} {line}")
                    continue
                raise RecoveryError(f"unreadable census row: {line[:120]!r}")
            pid_text, ppid_text, command = match.groups()
        else:
            parts = line.split(None, 2)
            if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
                if rows:
                    pid, ppid, command = rows[-1]
                    rows[-1] = (pid, ppid, f"{command} {line}")
                    continue
                raise RecoveryError(f"unreadable census row: {line[:120]!r}")
            pid_text, ppid_text = parts[0], parts[1]
            command = parts[2] if len(parts) > 2 else ""
        rows.append((int(pid_text), int(ppid_text), command))
    if not rows:
        raise RecoveryError("the process census returned nothing; ownership cannot be proved")
    return rows


def census_lineage(census: list[tuple[int, int, str]], pid: int) -> set[int]:
    """Our own pid, its ancestors and its descendants — not foreign writers."""
    parents = {entry[0]: entry[1] for entry in census}
    lineage = {pid}
    current = pid
    for _ in range(64):
        current = parents.get(current)
        if not current or current in lineage:
            break
        lineage.add(current)
    children: dict[int, list[int]] = {}
    for child, parent, _cmd in census:
        children.setdefault(parent, []).append(child)
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in lineage:
                lineage.add(child)
                stack.append(child)
    return lineage


def task_mentioned(task: str, command: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(task)}(?![\w-])", command) is not None


def repo_writers(repo: Path, task: str, census: list[tuple[int, int, str]],
                 self_pid: int | None = None) -> list[dict]:
    """Processes that could still be writing this repository or this task.

    Two signatures, because a stranded worker does not always carry the path:
    an engine command naming this repository, and any command naming this task
    (the worker prompt always does). Our own process lineage is excluded.
    """
    self_pid = os.getpid() if self_pid is None else self_pid
    lineage = census_lineage(census, self_pid)
    root = os.path.normcase(str(Path(repo).resolve()))
    variants = {root, root.replace("\\", "/")}
    found = []
    for pid, ppid, command in census:
        if pid in lineage:
            continue
        low = os.path.normcase(command)
        if any(variant in low for variant in variants) and any(
            token in low for token in ("gate.py", "flow.py")
        ):
            found.append({"pid": pid, "ppid": ppid, "why": "engine command in this repository",
                          "command": command[:300]})
        elif task_mentioned(task, command):
            found.append({"pid": pid, "ppid": ppid, "why": f"command names {task}",
                          "command": command[:300]})
    return found


def legacy_owner(repo: Path, run: str, pid: int | None) -> dict:
    """Bind a closed supervisor session, not an operator's arbitrary dead PID.

    The exact session bytes are retained in the immutable receipt. Later log
    appends are permitted; changing the recorded session invalidates it.
    """
    path = Path(repo) / GATE_DIR / "supervisor.log"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RecoveryError("legacy ownership needs the original supervisor.log") from exc
    starts = list(re.finditer(rb"(?m)^([^\r\n]+)  supervisor start: repo=(.*?) max_tasks=.*? pid=(\d+)\r?$", raw))
    matches = []
    for i, start in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(raw)
        session = raw[start.start():end]
        if (b"GATE run " + run.encode() + b"\n") not in session.replace(b"\r\n", b"\n"):
            continue
        exit_match = re.search(rb"(?m)^\S+  supervisor exit\r?$", session)
        if not exit_match or not re.search(rb"(?m)^\S+  runner exited code=", session[:exit_match.start()]):
            raise RecoveryError("legacy supervisor session has no corresponding runner/supervisor exit")
        owner_repo = os.fsdecode(start.group(2)).replace("\\", "/").rstrip("/")
        if os.path.normcase(owner_repo) != os.path.normcase(str(Path(repo).resolve()).replace("\\", "/")):
            raise RecoveryError("supervisor ownership names another repository")
        if int(start.group(3)) != pid:
            raise RecoveryError("--stopped-pid does not match the original supervisor ownership")
        bound = session[:exit_match.end()]
        matches.append({"offset": start.start(), "bytes": len(bound),
                        "sha256": hashlib.sha256(bound).hexdigest(),
                        "started": os.fsdecode(start.group(1)), "run": run})
    if len(matches) != 1:
        raise RecoveryError("legacy run must belong to exactly one closed supervisor session")
    return matches[0]


def ownership_evidence(repo: Path, task: str, recorded_pid: int | None,
                       stopped_pid: int | None, run: str = "") -> dict:
    """Prove no owned writer: a census with this repository's identity, plus a
    concrete stopped owner. A yes/no assertion is not accepted anywhere here."""
    census = process_census()
    writers = repo_writers(repo, task, census)
    if writers:
        raise RecoveryError(
            "a process may still own this repository or task: "
            + "; ".join(f"pid {w['pid']} ({w['why']}): {w['command']}" for w in writers)
        )
    owner = recorded_pid if recorded_pid else stopped_pid
    if owner is None:
        raise RecoveryError(
            "this run recorded no runner pid, so its owner must be named: pass "
            "--stopped-pid <pid of the stopped runner you observed>. An assertion "
            "that it stopped is not evidence; the pid is checked."
        )
    if recorded_pid and stopped_pid and recorded_pid != stopped_pid:
        raise RecoveryError(
            f"--stopped-pid {stopped_pid} is not the pid the run recorded ({recorded_pid})"
        )
    supervisor = legacy_owner(repo, run, stopped_pid) if not recorded_pid else None
    lineage = census_lineage(census, os.getpid())
    if owner in lineage:
        raise RecoveryError(f"pid {owner} is this process or its lineage, not a stopped owner")
    if any(entry[0] == owner for entry in census):
        raise RecoveryError(f"pid {owner} is still in the process census; it has not stopped")
    if process_alive(owner):
        raise RecoveryError(f"pid {owner} still answers as alive; it has not stopped")
    return {
        "owner_pid": owner,
        "owner_source": "journal" if recorded_pid else "supervisor.log",
        "supervisor": supervisor,
        "census_size": len(census),
        "census_at": _stamp(),
        "matched_writers": [],
    }


# ------------------------------------------------------------------ locks


def gate_path(repo: Path, name: str) -> Path:
    return Path(repo) / GATE_DIR / name


# Every lock this process holds, so an unhandled exception still retires the
# sidecar on the way out. The OS lock itself needs no help: the kernel drops it.
_HELD: list["EngineLock"] = []


def release_all_locks() -> None:
    for lock in list(_HELD):
        lock.release()


atexit.register(release_all_locks)


class EngineLock:
    """One OS advisory lock shared by `run` and `recover-task`.

    The kernel owns it: two holders cannot exist, and the lock disappears the
    moment the holding process dies, so there is no stale-lock heuristic and
    nothing to delete. The sidecar only describes the holder for humans, and is
    rewritten on release by the holder alone.
    """

    def __init__(self, repo: Path, what: str, run: str | None = None):
        self.repo = Path(repo)
        self.what = what
        self.run = run
        self.token = hashlib.sha256(f"{os.getpid()}:{time.time()}:{what}".encode()).hexdigest()[:16]
        self.fd: int | None = None

    @property
    def path(self) -> Path:
        return gate_path(self.repo, ENGINE_LOCK)

    @property
    def meta_path(self) -> Path:
        return gate_path(self.repo, ENGINE_LOCK_META)

    def acquire(self) -> "EngineLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if os.path.getsize(self.path) == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RecoveryError(
                f"the engine lock is held by another process ({describe_lock(self.repo)}); "
                f"{self.what} refuses to run beside it"
            ) from exc
        self.fd = fd
        _HELD.append(self)
        payload = {"pid": os.getpid(), "what": self.what, "run": self.run,
                   "token": self.token, "at": _stamp(), "released": False,
                   "repo": os.path.normcase(str(self.repo.resolve()))}
        _write_atomic(self.meta_path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return self

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            current = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        # Owner-checked: only the holder that wrote this token retires it.
        if current.get("token") == self.token:
            _write_atomic(self.meta_path,
                          json.dumps({**current, "released": True, "released_at": _stamp()},
                                     indent=2, ensure_ascii=False) + "\n")
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self.fd)
            self.fd = None
            if self in _HELD:
                _HELD.remove(self)

    def __enter__(self) -> "EngineLock":
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()


def describe_lock(repo: Path) -> str:
    try:
        payload = json.loads(gate_path(repo, ENGINE_LOCK_META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "holder unknown"
    return (f"pid {payload.get('pid')}, {payload.get('what')}"
            + (f", run {payload['run']}" if payload.get("run") else "")
            + f", since {payload.get('at')}")


def engine_lock_free(repo: Path) -> bool:
    """Probe without holding: acquire and release immediately."""
    probe = EngineLock(repo, "probe")
    try:
        probe.acquire()
    except RecoveryError:
        return False
    probe.release()
    return True


def _write_atomic(path: Path, text: str) -> None:
    """Replace a file in one step, with its bytes on disk before the swap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _append(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_ndjson(path: Path, label: str) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            raise RecoveryError(f"{label} line {number} is not readable: {exc}") from exc
    return rows


# ---------------------------------------------------------------- journal


def journal_events(repo: Path) -> list[dict]:
    return _read_ndjson(Path(repo) / GATE_DIR / "journal.ndjson", "journal")


def stopped_run(repo: Path, run_id: str, reason: str, task: str, fresh: bool = True) -> dict:
    """Prove the claim is nobody's: the run is closed and it stopped on this task.

    `fresh` adds the checks that only make sense before a recovery happens; the
    rest is recomputed identically later, so the stored evidence can be
    revalidated instead of trusted.
    """
    events = journal_events(repo)
    started: dict[str, int] = {}
    ended: dict[str, int] = {}
    for position, event in enumerate(events):
        if event.get("run") and event.get("event") == "run_start":
            started[event["run"]] = position
        elif event.get("run") and event.get("event") == "run_end":
            ended[event["run"]] = position
    if run_id not in started:
        raise RecoveryError(f"run {run_id} has no run_start in the journal")
    if run_id not in ended:
        raise RecoveryError(f"run {run_id} has no run_end in the journal")
    if fresh:
        still_open = sorted(run for run, position in started.items()
                            if run not in ended and position >= started[run_id])
        if still_open:
            raise RecoveryError(
                "the journal still has open run(s) from this point on: " + ", ".join(still_open)
                + ". A claim is only recoverable once the runs that could hold it are closed."
            )
    if reason.rsplit(":", 1)[-1] != task:
        raise RecoveryError(
            f"stop reason {reason!r} does not name {task}; only the task a run "
            "stopped on is recoverable from it"
        )
    start = started[run_id]
    end = next((i for i, e in enumerate(events[start:], start)
                if e.get("event") == "run_end" and e.get("run") == run_id), None)
    if end is None:
        raise RecoveryError(f"run {run_id} was reopened after its run_end")
    window = events[start:end + 1]
    close = window[-1]
    if close.get("stop") != reason:
        raise RecoveryError(
            f"run {run_id} stopped with {close.get('stop')!r}, not {reason!r}"
        )
    dispatched = [e for e in window
                  if e.get("event") == "attempt_start" and e.get("task") == task]
    if not dispatched:
        raise RecoveryError(f"run {run_id} never dispatched {task}")
    closed = [e for e in window if e.get("event") == "task_end" and e.get("task") == task]
    outcome = closed[-1].get("outcome") if closed else "unrecorded"
    if outcome not in RECOVERABLE_OUTCOMES:
        raise RecoveryError(
            f"run {run_id} ended {task} as {outcome!r}. Recovery revalidates work that was "
            "committed and only left unclosed; it never re-opens the budget of a task that "
            "failed to make progress. Re-queue that task instead."
        )
    if fresh:
        later = [e for e in events[end + 1:]
                 if e.get("event") in {"attempt_start", "revision_start"} and e.get("task") == task]
        if later:
            raise RecoveryError(
                f"{task} was dispatched again after run {run_id}; recover that later run instead"
            )
    runner_pid = close.get("pid") or events[start].get("pid")
    return {
        "run": run_id,
        "reason": reason,
        "ended_at": close.get("at"),
        "ended_at_epoch": _epoch(close.get("at")),
        "attempts": len(dispatched),
        "outcome": outcome,
        "runner_pid": runner_pid if isinstance(runner_pid, int) else None,
        "signature": (closed[-1].get("signature") if closed else None),
    }


# --------------------------------------------------------------- identity


def repo_identity(repo: Path) -> dict:
    root = os.path.normcase(str(Path(repo).resolve()))
    git_dir = _out(repo, "rev-parse", "--absolute-git-dir")
    return {"root": root, "git_dir": os.path.normcase(str(Path(git_dir).resolve()))}


def same_repo(repo: Path, recorded: dict) -> bool:
    try:
        identity = repo_identity(repo)
    except RecoveryError:
        return False
    return all(recorded.get(key) == value for key, value in identity.items())


def verify_config(cfg: dict) -> dict:
    return {field: cfg.get(field) for field in VERIFY_CONFIG_FIELDS}


# ------------------------------------------------------- acceptance inputs


def is_gate_artifact(rel: str) -> bool:
    """Runner-owned churn, matched by exact path or prefix — never substring."""
    norm = rel.replace("\\", "/")
    return (
        norm in GATE_ARTIFACT_PATHS
        or norm.startswith(GATE_ARTIFACT_PREFIXES)
        or norm in {p.rstrip("/") for p in GATE_ARTIFACT_PREFIXES if p.endswith("/")}
        or bool(re.fullmatch(r"\.gate/(verdict\.json|engine\.lock\.json)\.\d+\.tmp", norm))
    )


def git_paths(repo: Path, *args: str) -> list[str]:
    """Git -z emits original filename bytes, independent of core.quotepath."""
    return [os.fsdecode(p) for p in _git_bytes(repo, *args).split(b"\0") if p]


def ignored_entries(repo: Path) -> list[str]:
    """Ignored inputs at the granularity a project can actually answer for.

    Directories collapse to one entry (`node_modules/`), so a dependency tree
    of two million files is one classification, not two million refusals and
    a twenty-second scan on every acceptance run.
    """
    return git_paths(repo, "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
                     "--directory")


def classify_inputs(repo: Path, cfg: dict) -> tuple[list[str], list[str]]:
    """(paths the oracle is bound to, ignored entries nobody has classified).

    Tracked and untracked files are always bound. Ignored entries are bound
    when `recovery_inputs.include` names them, skipped when `.exclude` does,
    and reported otherwise — capture never refuses, so an ordinary task's
    verify keeps working; a recovered completion refuses on anything reported.
    """
    contract = cfg.get("recovery_inputs") or {}
    include = contract.get("include", [])
    exclude = contract.get("exclude", [])
    if not isinstance(include, list) or not isinstance(exclude, list):
        raise RecoveryError("recovery_inputs requires include/exclude path lists")
    for path in include + exclude:
        if (not isinstance(path, str) or not path or "\\" in path
                or Path(path).is_absolute() or ".." in Path(path).parts
                or any(c in path for c in "*?[]")):
            raise RecoveryError("recovery_inputs paths must be exact repository-relative paths")

    def listed(path: str, entries: list[str]) -> bool:
        return any(path == p.rstrip("/") or (p.endswith("/") and path.startswith(p))
                   for p in entries)

    paths = set(git_paths(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard"))
    if any(listed(p, exclude) for p in paths if not is_gate_artifact(p)):
        raise RecoveryError("recovery_inputs exclusions may only classify ignored files")
    unclassified: list[str] = []
    for entry in ignored_entries(repo):
        if is_gate_artifact(entry) or entry.rstrip("/") == GATE_DIR:
            # Runner-owned; its configuration is bound explicitly below.
            continue
        if listed(entry, include):
            root = Path(repo) / entry
            if entry.endswith("/"):
                paths.update(
                    str(f.relative_to(repo)).replace("\\", "/")
                    for f in root.rglob("*") if f.is_file()
                )
            else:
                paths.add(entry)
        elif not listed(entry, exclude):
            unclassified.append(entry)
    for path in include:
        if not path.endswith("/"):
            paths.add(path)
    paths.add(".gate/config.json")
    return sorted(p for p in paths if not is_gate_artifact(p)), sorted(unclassified)


def input_paths(repo: Path, cfg: dict) -> list[str]:
    return classify_inputs(repo, cfg)[0]


def change_stamp(path: Path) -> list[int]:
    """Kernel change metadata detects write-and-restore, including restored mtime.

    Windows stat ctime is creation time; query NTFS ChangeTime explicitly.
    Filesystems without a trustworthy change counter are unsupported.
    """
    st = path.stat()
    changed = st.st_ctime_ns
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [(n, ctypes.c_longlong) for n in
                        ("CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime")] + [("Attributes", wintypes.DWORD)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x02000000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise RecoveryError(f"cannot inspect change metadata: {path}")
        try:
            basic = Basic()
            if not kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
                raise RecoveryError(f"filesystem has no change metadata: {path}")
            changed = basic.ChangeTime
        finally:
            kernel.CloseHandle(handle)
    if not changed:
        raise RecoveryError(f"filesystem has no change metadata: {path}")
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, changed]


def worktree_state(repo: Path, cfg: dict | None = None) -> dict:
    state = {}
    index = {}
    for entry in _git_bytes(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        info, path = entry.split(b"\t", 1)
        mode, oid, stage = info.split()
        if stage != b"0":
            raise RecoveryError("unmerged acceptance index")
        index[os.fsdecode(path)] = [mode.decode(), oid.decode()]
    for rel in input_paths(repo, cfg or {}):
        full = Path(repo) / rel
        # Never follow a dependency outside the repository, including junctions.
        if not full.resolve().is_relative_to(Path(repo).resolve()) or full.is_symlink():
            raise RecoveryError(f"external/symlink acceptance dependency unsupported: {rel}")
        if not full.exists():
            state[rel] = {"sha256": "absent", "change": None}
            continue
        if not full.is_file():
            raise RecoveryError(f"acceptance dependency is not a regular file: {rel}")
        before = change_stamp(full)
        content = hashlib.sha256(full.read_bytes()).hexdigest()
        after = change_stamp(full)
        if before != after:
            raise RecoveryError(f"acceptance input changed during capture: {rel}")
        state[rel] = {"sha256": content, "change": after, "index": index.get(rel)}
    return state


def acceptance_inputs(repo: Path, cfg: dict) -> dict:
    return {"head": _out(repo, "rev-parse", "HEAD"),
            "worktree": worktree_state(repo, cfg), "verify": verify_config(cfg),
            "unclassified": classify_inputs(repo, cfg)[1]}


class InputMonitor:
    """Windows recursive kernel notifications, armed before the first capture.

    Fail closed on overflow or I/O errors. Metadata remains a second check.
    POSIX uses its non-user-restorable ctime; Windows needs notifications since
    native APIs can reset ChangeTime as well as mtime.
    """
    def __init__(self, repo: Path, cfg: dict):
        self.repo, self.cfg = Path(repo), cfg
        self.changed = set()
        self.errors = []
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread = None

    def start(self):
        if os.name != "nt":
            return self
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()
        if not self.ready.wait(10) or self.errors:
            self.finish()
            raise RecoveryError("cannot arm acceptance filesystem monitor")
        return self

    def _watch(self):
        import ctypes
        import struct
        from ctypes import wintypes as w
        class Overlapped(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        k.CreateEventW.argtypes = [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
        k.CreateEventW.restype = w.HANDLE
        k.ReadDirectoryChangesW.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, w.BOOL, w.DWORD,
                                            ctypes.c_void_p, ctypes.POINTER(Overlapped), ctypes.c_void_p]
        k.GetOverlappedResult.argtypes = [w.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(w.DWORD), w.BOOL]
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.CancelIoEx.argtypes = [w.HANDLE, ctypes.POINTER(Overlapped)]
        k.CloseHandle.argtypes = [w.HANDLE]
        k.ResetEvent.argtypes = [w.HANDLE]
        handle = k.CreateFileW(str(self.repo), 1, 7, None, 3, 0x42000000, None)
        event = k.CreateEventW(None, True, False, None)
        try:
            if handle == w.HANDLE(-1).value or not event:
                raise OSError("opening directory monitor failed")
            # Local directories accept large buffers; a dependency tree that
            # the oracle writes into must not overflow this before it is drained.
            buf = ctypes.create_string_buffer(4 * 1024 * 1024)
            while True:
                ov = Overlapped(); ov.hEvent = event
                k.ResetEvent(event)
                if not k.ReadDirectoryChangesW(handle, buf, len(buf), True, 0x15f, None, ctypes.byref(ov), None):
                    raise OSError("arming directory monitor failed")
                self.ready.set()
                while k.WaitForSingleObject(event, 10) == 258:
                    if self.stop.is_set():
                        k.CancelIoEx(handle, ctypes.byref(ov))
                        break
                count = w.DWORD()
                if not k.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(count), True):
                    if self.stop.is_set() and ctypes.get_last_error() == 995:
                        break
                    raise OSError("directory monitor failed")
                if not count.value:
                    raise OSError("directory notification overflow")
                offset = 0
                while True:
                    nxt, action, length = struct.unpack_from("III", buf.raw, offset)
                    path = buf.raw[offset+12:offset+12+length].decode("utf-16-le").replace("\\", "/")
                    excluded = (self.cfg.get("recovery_inputs") or {}).get("exclude", [])
                    if (path not in (".git", ".gate") and not path.startswith(".git/") and not is_gate_artifact(path)
                            and not any(path == p.rstrip("/") or (p.endswith("/") and path.startswith(p)) for p in excluded)):
                        self.changed.add(path)
                    if not nxt:
                        break
                    offset += nxt
                if self.stop.is_set():
                    break
        except BaseException as exc:
            self.errors.append(str(exc))
            self.ready.set()
        finally:
            k.CloseHandle(handle)
            if event:
                k.CloseHandle(event)

    def finish(self) -> list[str]:
        self.stop.set()
        if self.thread:
            self.thread.join(10)
            if self.thread.is_alive():
                self.errors.append("monitor did not stop")
        return sorted(self.changed) + ["<monitor: " + e + ">" for e in self.errors]


def inputs_differences(before: dict, after: dict) -> list[str]:
    """What changed between two captures, named path by path."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ["<missing capture>"]
    changed = []
    for field in ("head", "config", "verify"):
        if before.get(field) != after.get(field):
            changed.append(f"<{field}>")
    old, new = before.get("worktree") or {}, after.get("worktree") or {}
    for path in sorted(set(old) | set(new)):
        if old.get(path) != new.get(path):
            changed.append(path)
    return changed


# ------------------------------------------------------------ scope/state


def declared_files(cfg: dict, text: str, task: str) -> list[str]:
    files = _parse_task_files(text).get(task)
    if not files:
        raise RecoveryError(
            f"no '<!-- task:{task} files: … -->' scope line; an undeclared task "
            "cannot be bound to any implementation"
        )
    for rel in files:
        norm = rel.replace("\\", "/")
        if not norm or norm.startswith("/") or ".." in norm.split("/") or ":" in norm:
            raise RecoveryError(f"declared scope is not a project-relative path: {rel}")
    source = [rel for rel in files if not _matches_any(rel, cfg["doc_only_globs"])]
    if not source:
        raise RecoveryError(
            f"{task} declares documentation only; there is no implementation to revalidate"
        )
    return sorted(files)


def source_state(repo: Path, cfg: dict, files: list[str], ref: str) -> dict:
    """Blob identity of every declared file at one ref. Git computes the hash,
    so it is the same number here, in the commit, and on the other platform."""
    state = {}
    for rel in sorted(files):
        path = _prefixed(cfg, rel)
        oid = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}:{path}",
                   check=False).stdout.strip()
        if not oid:
            raise RecoveryError(f"declared file {path} does not exist at {ref}")
        content = _git_bytes(repo, "cat-file", "blob", oid)
        state[rel] = {
            "path": path,
            "blob": oid,
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
    return state


def assert_clean_sources(repo: Path, cfg: dict, files: list[str]) -> None:
    paths = [_prefixed(cfg, rel) for rel in files]
    dirty = [
        line
        for line in _git(repo, "status", "--porcelain=v1", "-uall", "--", *paths).stdout.splitlines()
        if line.strip()
    ]
    if dirty:
        raise RecoveryError(
            "declared source is staged or modified; a recovered task is a task whose "
            "implementation is already committed:\n  " + "\n  ".join(dirty)
        )


def present_sources(repo: Path, cfg: dict, files: list[str]) -> dict:
    """What the declared files are right now. Cleanliness is checked first, so
    the committed blob is also the working-tree content — one hash, no filter
    ambiguity between a worktree read and a committed object."""
    assert_clean_sources(repo, cfg, files)
    return source_state(repo, cfg, files, "HEAD")


# ----------------------------------------------------- requirement binding


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _sections(text: str) -> list[tuple[str, str]]:
    """Markdown split into (heading, body-including-heading) sections."""
    lines = text.splitlines(keepends=True)
    headings = [(i, HEADING_RE.match(line.rstrip("\r\n"))) for i, line in enumerate(lines)]
    headings = [(i, m) for i, m in headings if m]
    sections = []
    for position, (start, match) in enumerate(headings):
        end = next((i for i, m in headings[position + 1:]
                    if len(m.group(1)) <= len(match.group(1))), len(lines))
        sections.append((lines[start].rstrip("\r\n"), "".join(lines[start:end])))
    return sections


def _blocks(text: str) -> list[str]:
    blocks, buffer = [], []
    for line in text.splitlines(keepends=True):
        if line.strip():
            buffer.append(line)
        elif buffer:
            blocks.append("".join(buffer))
            buffer = []
    if buffer:
        blocks.append("".join(buffer))
    return blocks


def acceptance_files(repo: Path, cfg: dict, ref: str, explicit: str | None) -> list[str]:
    if explicit:
        return [_prefixed(cfg, explicit)]
    globs = cfg.get("acceptance_globs") or list(DEFAULT_ACCEPTANCE_GLOBS)
    listed = (git_paths(repo, "ls-files", "-z", "--cached") if ref in (":", "WORKTREE")
              else git_paths(repo, "ls-tree", "-r", "-z", "--name-only", ref))
    return sorted(
        path for path in listed
        if _in_project(cfg, path) and _matches_any(_project_rel(cfg, path), globs)
    )


REQUIREMENT_SKIP_CELL = re.compile(r"^[-–—\s]*$")


BOOKKEEPING_HEADERS = ("status", "worker", "baseline", "passed", "count")


def row_headers(text: str, task: str) -> list[str] | None:
    """The header cells of the table that holds this task's row, if any."""
    headers: list[str] | None = None
    row_re = _g("ROW_RE")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):
            continue
        if _row_task_id(line) == task:
            return headers
        if row_re.match(line) is None:
            headers = [c.lower().strip("*` ") for c in cells]
    return None


def requirement_cells(cfg: dict, text: str, task: str) -> list[str]:
    """The row's substance: everything except the bookkeeping cells.

    Bookkeeping is what the table header says it is: the index, the status
    (it is what recovery moves), the worker pin, and the baseline / count
    columns, whose placeholders ("measured at dispatch", "pending") are filled
    in by the closing worker and are not requirement text. Without a header
    the older content heuristics apply.
    """
    row = _parse_queue(text).get(task)
    if not row:
        raise RecoveryError(f"{task} has no queue row")
    cells = list(row["cells"])
    headers = row_headers(text, task)
    pin_value = _parse_task_workers(text).get(task)
    count = re.compile(cfg["count_regex"])
    status_index = next((i for i, cell in enumerate(cells) if STATUS_CELL_RE.search(cell)), -1)
    kept = []
    for index, cell in enumerate(cells):
        stripped = cell.strip()
        if index in (0, status_index):
            continue
        if headers is not None and index < len(headers) and (
            headers[index] == "#"
            or any(token in headers[index] for token in BOOKKEEPING_HEADERS)
        ):
            continue
        if count.search(stripped) or REQUIREMENT_SKIP_CELL.match(stripped):
            continue
        if pin_value and stripped.strip("*` ").lower() == pin_value:
            continue
        kept.append(stripped)
    return kept


def requirement_binding(repo: Path, cfg: dict, task: str, ref: str, text: str,
                        mode: str = "headings", explicit: str | None = None) -> dict:
    """Bind what the task actually asks for, not that its id still appears.

    The row's own substance, plus the task's section of the acceptance
    document. Unrelated sections of the same document may change freely; the
    task's own may not. `blocks` mode is opt-in because a bookkeeping line that
    merely mentions the task would otherwise bind as a requirement.
    """
    if mode not in ("headings", "blocks"):
        raise RecoveryError(f"unknown requirement mode: {mode}")
    sources: dict[str, dict] = {}
    for path in acceptance_files(repo, cfg, ref, explicit):
        content = ((Path(repo) / path).read_text(encoding="utf-8") if ref == "WORKTREE" and (Path(repo) / path).is_file()
                   else (None if ref == "WORKTREE" else _blob(repo, ref, path)))
        if content is None:
            if explicit:
                raise RecoveryError(f"{path} does not exist at {ref[:10]}")
            continue
        if mode == "headings":
            parts = [body for heading, body in _sections(content)
                     if task_mentioned(task, heading)]
        else:
            parts = [block for block in _blocks(content) if task_mentioned(task, block)]
        if parts:
            sources[path] = {"mode": mode, "parts": len(parts),
                             "sha256": hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()}
    if not sources:
        raise RecoveryError(
            f"no acceptance text for {task} at {ref[:10]}: no {mode} in "
            + (explicit or ", ".join(cfg.get("acceptance_globs") or DEFAULT_ACCEPTANCE_GLOBS))
            + f" names {task}. Point --requirement at the document that states this "
            "task's acceptance, or use --requirement-mode blocks if it is not a heading. "
            "A task id in a title is not a requirement."
        )
    binding = {"cells": requirement_cells(cfg, text, task), "sources": sources,
               "mode": mode, "path": explicit}
    binding["digest"] = digest(binding)
    return binding


# -------------------------------------------------------- the queue's row


def queue_row(text: str, task: str) -> str:
    rows = [line for line in text.splitlines(keepends=True) if _row_task_id(line) == task]
    if len(rows) != 1:
        raise RecoveryError(f"{task} has {len(rows)} queue rows; expected exactly one")
    return rows[0]


STATUS_CELL_RE = re.compile(r"`([A-Z_]+)`")


def flipped_row(line: str, expected: str, marker: str) -> str:
    match = STATUS_CELL_RE.search(line)
    if not match or match.group(1) != expected:
        raise RecoveryError(
            f"queue row status is {match.group(1) if match else 'absent'}, not {expected}"
        )
    return line[: match.start()] + f"`{marker}`" + line[match.end():]


def recovered_queue_text(cfg: dict, text: str, task: str, marker: str) -> str:
    """Rewrite exactly one row. Every other byte of the file is preserved, so
    concurrent edits to other rows and to the scope lines survive."""
    lines = text.splitlines(keepends=True)
    indexes = [i for i, line in enumerate(lines) if _row_task_id(line) == task]
    if len(indexes) != 1:
        raise RecoveryError(f"{task} has {len(indexes)} queue rows; expected exactly one")
    index = indexes[0]
    status = _parse_queue(text).get(task, {}).get("status")
    if status not in cfg["in_progress_markers"]:
        raise RecoveryError(
            f"{task} is {status}, not IN_PROGRESS; only a stopped claim is recoverable"
        )
    lines[index] = flipped_row(lines[index], status, marker)
    after = "".join(lines)
    changes = _queue_status_changes(text, after)
    if changes != {task: (status, marker)}:
        raise RecoveryError(f"the row rewrite changed {changes}; refusing to write the queue")
    before_lines, after_lines = text.splitlines(keepends=True), after.splitlines(keepends=True)
    if len(before_lines) != len(after_lines) or any(
        before_line != after_line
        for position, (before_line, after_line) in enumerate(zip(before_lines, after_lines))
        if position != index
    ):
        raise RecoveryError("the row rewrite touched another line; refusing to write the queue")
    return after


# ---------------------------------------------------------------- receipts


def recovery_dir(repo: Path) -> Path:
    return Path(repo) / GATE_DIR / RECOVERY_DIR


def index_path(repo: Path) -> Path:
    return recovery_dir(repo) / "index.ndjson"


def completions_path(repo: Path) -> Path:
    return recovery_dir(repo) / "completions.ndjson"


def receipt_path(repo: Path, sha: str) -> Path:
    return recovery_dir(repo) / f"{sha}.json"


def receipt_digest(receipt: dict) -> str:
    body = {key: value for key, value in receipt.items() if key != "sha256"}
    return digest(body)


# Everything a later revalidation must be able to derive again from the
# repository. `ownership` is deliberately absent: it is a fact about the moment
# of recovery, and re-deriving it inside a hook would flag the running runner.
REVALIDATED = ("implementation", "stopped_run", "scope", "sources", "requirement")


def evidence(repo: Path, cfg: dict, task: str, implementation: str, run_id: str, reason: str,
             text: str, requirement_mode: str = "headings", requirement_path: str | None = None,
             fresh: bool = True) -> dict:
    """The part of a recovery that is re-derivable, and therefore re-checkable."""
    queue_rel = _prefixed(cfg, cfg["queue_file"])
    run_evidence = stopped_run(repo, run_id, reason, task, fresh=fresh)

    head_text = _blob(repo, "HEAD", queue_rel)
    if head_text is None:
        raise RecoveryError(f"{queue_rel} is not committed at HEAD")
    declared = declared_files(cfg, text, task)
    if declared_files(cfg, head_text, task) != declared:
        raise RecoveryError("declared scope at HEAD differs from the working queue")

    commit = _git(repo, "rev-parse", "--verify", "--quiet", f"{implementation}^{{commit}}",
                  check=False).stdout.strip()
    if not commit:
        raise RecoveryError(f"{implementation} is not a commit in this repository")
    parents = _out(repo, "rev-list", "--parents", "-n", "1", commit).split()[1:]
    if len(parents) != 1:
        raise RecoveryError(
            f"{commit[:10]} has {len(parents)} parents; only an ordinary single-parent "
            "implementation commit can be revalidated"
        )
    parent = parents[0]
    head = _out(repo, "rev-parse", "HEAD")
    if _git(repo, "merge-base", "--is-ancestor", commit, head, check=False).returncode != 0:
        raise RecoveryError(f"{commit[:10]} is not an ancestor of HEAD")
    committed_epoch = int(_out(repo, "show", "-s", "--format=%ct", commit))
    if committed_epoch > run_evidence["ended_at_epoch"]:
        raise RecoveryError(
            f"{commit[:10]} was committed after run {run_id} ended; it is not that run's work"
        )

    # The commit's own diff is the evidence. Its subject is never read: a
    # message that names the task proves nothing about what changed.
    changed = git_paths(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit)
    code_changed = [
        p for p in changed
        if _in_project(cfg, p) and not _matches_any(_project_rel(cfg, p), cfg["doc_only_globs"])
    ]
    if not code_changed:
        raise RecoveryError(
            f"{commit[:10]} declares no code change (only {', '.join(changed) or 'nothing'}); "
            "a queue-only or documentation commit is not an implementation"
        )
    declared_paths = {_prefixed(cfg, rel) for rel in declared}
    outside = sorted(set(code_changed) - declared_paths)
    if outside:
        raise RecoveryError(
            f"{commit[:10]} changed code outside the declared scope of {task}: "
            + ", ".join(outside)
        )
    touched = sorted(set(code_changed) & declared_paths)
    numstat = [
        line for line in _out(repo, "diff", "--numstat", parent, commit, "--",
                              *touched).splitlines() if line.strip()
    ]
    if not any(line.split("\t")[0] != "0" or line.split("\t")[1] != "0" for line in numstat):
        raise RecoveryError(f"{commit[:10]} changes no content in the declared files of {task}")

    commit_queue = _blob(repo, commit, queue_rel)
    if commit_queue is None:
        raise RecoveryError(f"{queue_rel} does not exist at {commit[:10]}")
    if task not in _parse_queue(commit_queue):
        raise RecoveryError(f"{task} has no queue row at {commit[:10]}")
    if declared_files(cfg, commit_queue, task) != declared:
        raise RecoveryError(
            f"the declared scope of {task} changed since {commit[:10]}; the implementation "
            "no longer answers the task that is in the queue"
        )

    # What the task asks for, then and now. Bookkeeping around it may move.
    then = requirement_binding(repo, cfg, task, commit, commit_queue,
                               requirement_mode, requirement_path)
    now_requirement = requirement_binding(repo, cfg, task, "HEAD", head_text,
                                          requirement_mode, requirement_path)
    if then["digest"] != now_requirement["digest"]:
        raise RecoveryError(
            f"the requirement of {task} changed since {commit[:10]}: "
            f"row cells {then['cells']} → {now_requirement['cells']}, acceptance "
            f"{list(then['sources'])} → {list(now_requirement['sources'])}. The prior "
            "implementation answers a question the queue no longer asks."
        )

    for ref in (":", "WORKTREE"):
        queue = _blob(repo, ref, queue_rel) if ref == ":" else text
        bound = requirement_binding(repo, cfg, task, ref, queue, requirement_mode, requirement_path)
        if bound["digest"] != then["digest"]:
            raise RecoveryError(f"the requirement of {task} changed in {ref}")

    at_commit = source_state(repo, cfg, declared, commit)
    now_sources = present_sources(repo, cfg, declared)
    drifted = sorted(rel for rel in declared if at_commit[rel]["blob"] != now_sources[rel]["blob"])
    if drifted:
        raise RecoveryError(
            "declared content changed since the implementation commit: " + ", ".join(drifted)
            + ". The prior implementation is no longer what is in the tree; run the task again."
        )
    return {
        "implementation": {
            "commit": commit,
            "parent": parent,
            "committed_at_epoch": committed_epoch,
            "declared_paths_changed": touched,
            "numstat": numstat,
        },
        "stopped_run": run_evidence,
        "scope": {"queue_file": queue_rel, "declared_files": declared,
                  "after": _parse_task_after(head_text).get(task, [])},
        "sources": now_sources,
        "requirement": now_requirement,
    }


def plan(repo: Path, cfg: dict, task: str, implementation: str, run_id: str, reason: str,
         stopped_pid: int | None = None, requirement_mode: str = "headings",
         requirement_path: str | None = None) -> dict:
    """Every check, no writes. `recover` runs this again before it touches anything."""
    repo = Path(repo)
    identity = repo_identity(repo)
    queue_rel = _prefixed(cfg, cfg["queue_file"])
    queue_file = repo / queue_rel
    if not queue_file.exists():
        raise RecoveryError(f"queue not found: {queue_file}")
    with queue_file.open(encoding="utf-8", newline="") as handle:
        text = handle.read()

    status = _parse_queue(text).get(task, {}).get("status")
    if status is None:
        raise RecoveryError(f"{task} is not in {queue_rel}")

    # The row itself must be committed: an uncommitted edit to the very row
    # being recovered is somebody else already working in it. Checked before
    # the status, because the common case — a DONE the hook has just refused,
    # still sitting in the worktree — needs that diagnosis.
    head_text = _blob(repo, "HEAD", queue_rel)
    if head_text is None:
        raise RecoveryError(f"{queue_rel} is not committed at HEAD")
    row_now, row_head = queue_row(text, task), queue_row(head_text, task)
    if row_now.rstrip("\r\n") != row_head.rstrip("\r\n"):
        raise RecoveryError(
            f"the {task} row differs from HEAD (worktree {status}, HEAD "
            f"{_parse_queue(head_text).get(task, {}).get('status')}); commit or revert "
            "that row before recovering it"
        )
    if status not in cfg["in_progress_markers"]:
        raise RecoveryError(
            f"{task} is {status}; recovery restores a stopped IN_PROGRESS claim and nothing else"
        )

    found = evidence(repo, cfg, task, implementation, run_id, reason, text,
                     requirement_mode, requirement_path, fresh=True)
    ownership = ownership_evidence(repo, task, found["stopped_run"]["runner_pid"], stopped_pid, run_id)
    lineage = found["implementation"]["commit"]
    if len(lineage_dispatches(repo, task, lineage)) > len(lineage_renewals(repo, task, lineage)):
        raise RecoveryError("revalidation allowance exhausted for original implementation lineage")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "task_recovery",
        "task": task,
        "repo": {**identity, "head": _out(repo, "rev-parse", "HEAD")},
        **found,
        "ownership": ownership,
        "sources_digest": digest(found["sources"]),
        "verify_config": digest(verify_config(cfg)),
        "verify_config_fields": verify_config(cfg),
        "queue_transition": {
            "from": status,
            "to": cfg["todo_markers"][0],
            "row_before": row_now.rstrip("\r\n"),
        },
    }


def recover(repo: Path, cfg: dict, task: str, implementation: str, run_id: str, reason: str,
            note: str = "", stopped_pid: int | None = None,
            requirement_mode: str = "headings", requirement_path: str | None = None) -> dict:
    """Move one stopped claim back to TODO and leave the evidence behind.

    The order is the recovery protocol: the receipt exists before the queue
    moves, the queue moves in one atomic replace, and the index entry — which
    is what makes a receipt usable — is appended last. Interrupted anywhere,
    the repository is left in a state that grants nothing.
    """
    repo = Path(repo)
    with EngineLock(repo, f"recover {task}"):
        pending = pending_transactions(repo)
        if pending:
            intent = pending.get(task)
            if not intent:
                raise RecoveryError("another recovery transaction is pending")
            receipt = intent["receipt"]
            if (receipt["implementation"]["commit"] != _out(repo, "rev-parse", implementation)
                    or receipt["stopped_run"]["run"] != run_id
                    or receipt["stopped_run"]["reason"] != reason):
                raise RecoveryError("pending recovery transaction arguments differ")
            revalidate(repo, cfg, receipt)
            ownership_evidence(repo, task, receipt["stopped_run"]["runner_pid"],
                               receipt["ownership"]["owner_pid"], run_id)
            finish_transaction(repo, cfg, intent)
            return receipt
        receipt = plan(repo, cfg, task, implementation, run_id, reason, stopped_pid,
                       requirement_mode, requirement_path)
        marker = cfg["todo_markers"][0]
        queue_file = repo / receipt["scope"]["queue_file"]
        # Byte-preserving on purpose: read_text/write_text would rewrite every
        # line ending in the file on Windows, turning a one-row recovery into a
        # whole-file diff.
        with queue_file.open(encoding="utf-8", newline="") as handle:
            before = handle.read()
        after = recovered_queue_text(cfg, before, task, marker)

        receipt["queue_transition"]["row_after"] = queue_row(after, task).rstrip("\r\n")
        receipt["note"] = note
        receipt["recovered_at_epoch"] = time.time()
        receipt["recovered_at"] = _stamp()
        receipt["sha256"] = receipt_digest(receipt)

        path = receipt_path(repo, receipt["sha256"])
        if not path.exists():
            _write_atomic(path, json.dumps(receipt, indent=2, ensure_ascii=True) + "\n")
        intent = {"event": "intent", "task": task, "receipt": receipt,
                  "before": before, "after": after}
        _append(recovery_dir(repo) / "transactions.ndjson", intent)
        finish_transaction(repo, cfg, intent)
        return receipt


def pending_transactions(repo: Path) -> dict:
    pending = {}
    for event in _read_ndjson(recovery_dir(repo) / "transactions.ndjson", "recovery transactions"):
        if event["event"] == "intent":
            pending[event["task"]] = event
        elif event["event"] == "committed":
            pending.pop(event["task"], None)
    return pending


def finish_transaction(repo: Path, cfg: dict, intent: dict) -> None:
    receipt = intent["receipt"]
    task = receipt["task"]
    queue_file = Path(repo) / receipt["scope"]["queue_file"]
    with queue_file.open(encoding="utf-8", newline="") as handle:
        current = handle.read()
    if current not in (intent["before"], intent["after"]):
        raise RecoveryError("pending transaction queue differs; retaining dispatch barrier")
    if current != intent["after"]:
        _write_atomic(queue_file, intent["after"])
    if not any(e.get("receipt") == receipt["sha256"] for e in
               _read_ndjson(index_path(repo), "recovery index")):
        _append(index_path(repo), {"at": receipt["recovered_at"], "task": task,
                                   "receipt": receipt["sha256"],
                                   "implementation": receipt["implementation"]["commit"],
                                   "run": receipt["stopped_run"]["run"]})
    if not any(e.get("event") == "task_recovered" and e.get("receipt") == receipt["sha256"]
               for e in journal_events(repo)):
        _journal(repo, {"event": "task_recovered", "task": task,
                        "receipt": receipt["sha256"], **receipt["stopped_run"],
                        "implementation": receipt["implementation"]["commit"],
                        "owner_pid": receipt["ownership"]["owner_pid"],
                        "from": receipt["queue_transition"]["from"],
                        "to": receipt["queue_transition"]["to"]})
    _append(recovery_dir(repo) / "transactions.ndjson",
            {"event": "committed", "task": task, "receipt": receipt["sha256"]})


def revalidate(repo: Path, cfg: dict, receipt: dict, entry: dict | None = None) -> dict:
    """Re-derive a receipt's evidence and refuse anything that no longer holds.

    A receipt is a claim about the repository, not a token. Structure (its own
    digest, its file name, its index entry) must agree, and every re-derivable
    fact must still be the fact, so recomputing the digest over edited contents
    buys nothing.
    """
    if receipt.get("schema_version") != SCHEMA_VERSION or receipt.get("kind") != "task_recovery":
        raise RecoveryError("not a task recovery receipt of this schema")
    sha = receipt.get("sha256")
    if sha != receipt_digest(receipt):
        raise RecoveryError("receipt digest does not match its contents (tampered)")
    if not isinstance(sha, str) or not receipt_path(repo, sha).exists():
        raise RecoveryError("receipt is not stored under its own digest")
    if entry is not None:
        for field, value in (("task", receipt.get("task")),
                             ("receipt", sha),
                             ("implementation", receipt.get("implementation", {}).get("commit")),
                             ("run", receipt.get("stopped_run", {}).get("run")),
                             ("at", receipt.get("recovered_at"))):
            if entry.get(field) != value:
                raise RecoveryError(
                    f"the recovery index disagrees with the receipt on {field}"
                )
    if not same_repo(repo, receipt.get("repo") or {}):
        raise RecoveryError("receipt belongs to another repository")
    if receipt.get("verify_config") != digest(verify_config(cfg)):
        raise RecoveryError("verification configuration changed since the recovery")

    if receipt.get("ownership", {}).get("supervisor"):
        own = receipt["ownership"]
        if legacy_owner(repo, receipt["stopped_run"]["run"], own["owner_pid"]) != own["supervisor"]:
            raise RecoveryError("original supervisor ownership evidence changed")
    queue_rel = receipt["scope"]["queue_file"]
    queue_file = Path(repo) / queue_rel
    if not queue_file.exists():
        raise RecoveryError(f"queue not found: {queue_file}")
    with queue_file.open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    fresh = evidence(repo, cfg, receipt["task"], receipt["implementation"]["commit"],
                     receipt["stopped_run"]["run"], receipt["stopped_run"]["reason"], text,
                     receipt["requirement"]["mode"],
                     receipt["requirement"].get("path"), fresh=False)
    for field in REVALIDATED:
        if digest(fresh[field]) != digest(receipt[field]):
            raise RecoveryError(f"recorded {field} no longer matches the repository")
    if receipt.get("sources_digest") != digest(receipt["sources"]):
        raise RecoveryError("recorded sources digest does not match the recorded sources")
    return receipt


def receipts(repo: Path, cfg: dict, task: str | None = None) -> tuple[list[dict], list[str]]:
    """Every receipt that still survives revalidation, newest first, plus why
    the others were rejected."""
    good: list[dict] = []
    problems: list[str] = []
    for entry in _read_ndjson(index_path(repo), "recovery index"):
        if task is not None and entry.get("task") != task:
            continue
        name = str(entry.get("receipt"))
        try:
            receipt = json.loads(receipt_path(repo, name).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{name}: unreadable receipt ({exc})")
            continue
        try:
            good.append(revalidate(repo, cfg, receipt, entry))
        except RecoveryError as exc:
            problems.append(f"{name}: {exc}")
    good.sort(key=lambda receipt: receipt.get("recovered_at_epoch", 0), reverse=True)
    return good, problems


def receipt_for(repo: Path, cfg: dict, task: str) -> dict | None:
    try:
        good, _ = receipts(repo, cfg, task)
    except RecoveryError:
        return None
    return good[0] if good else None


# --------------------------------------------------------- verdict binding


def verdict_binding(repo: Path, cfg: dict) -> dict:
    """Per-task source evidence recorded with an acceptance run.

    The run's whole input capture lives beside this in the verdict; this names
    the declared blobs of each recovered task so a refusal can say which file
    moved.
    """
    try:
        if not index_path(repo).exists():
            return {}
        good, _ = receipts(repo, cfg)
    except RecoveryError:
        return {}
    out: dict = {}
    for receipt in good:
        task = receipt["task"]
        if task in out:
            continue
        entry = {"receipt": receipt["sha256"]}
        try:
            sources = present_sources(repo, cfg, receipt["scope"]["declared_files"])
            entry.update(sources=sources, sources_digest=digest(sources))
        except RecoveryError as exc:
            entry["error"] = str(exc)
        out[task] = entry
    return out


# ------------------------------------------------------------- gate 1 hook


def done_allowance(repo: Path, cfg: dict, flipped: list[str], staged: list[str]) -> dict:
    """May this queue-only DONE be accepted? Only with a task-specific receipt
    that still revalidates, and an acceptance run whose inputs were identical
    before and after the oracle and are still the tree being committed."""
    repo = Path(repo)

    def no(detail: str) -> dict:
        return {"allowed": False, "detail": detail, "receipt": None}

    if len(flipped) != 1:
        return no(
            f"a recovered completion closes exactly one task; this one closes {len(flipped)}"
        )
    task = flipped[0]
    try:
        good, problems = receipts(repo, cfg, task)
    except RecoveryError as exc:
        return no(f"recovery evidence unreadable: {exc}")
    if not good:
        detail = f"no valid recovery receipt for {task}"
        return no(detail + (" (" + "; ".join(problems) + ")" if problems else ""))
    receipt = good[0]

    declared = receipt["scope"]["declared_files"]
    queue_rel = receipt["scope"]["queue_file"]
    staged_queue = _blob(repo, ":", queue_rel)
    if staged_queue is None:
        return no(f"{queue_rel} is not staged; a recovered completion commits the queue row")
    try:
        if declared_files(cfg, staged_queue, task) != declared:
            return no(
                f"declared scope of {task} changed since the recovery; the receipt covers "
                + ", ".join(declared)
            )
    except RecoveryError as exc:
        return no(f"declared scope of {task} is no longer usable: {exc}")

    try:
        sources = present_sources(repo, cfg, declared)
    except RecoveryError as exc:
        return no(str(exc))
    if digest(sources) != receipt["sources_digest"]:
        changed = sorted(
            rel for rel in declared
            if sources.get(rel, {}).get("blob") != receipt["sources"].get(rel, {}).get("blob")
        )
        return no(
            "declared content changed since the recovery: "
            + (", ".join(changed) or "(scope differs)")
            + ". A recovered task closes the implementation it was recovered for."
        )

    verdict = _read_verdict(repo)
    if not verdict:
        return no("no verdict recorded; a recovered completion needs a fresh acceptance run")
    if verdict.get("result") != "PASS":
        return no(
            f"last verdict is {verdict.get('result')}, not PASS"
            + (" — an acceptance run that started and did not finish invalidates the last one"
               if verdict.get("result") in ("RUNNING", "TIMEOUT", "INTERRUPTED") else "")
        )
    if verdict.get("cmd") != cfg["verify_cmd"]:
        return no(
            f"the verdict came from {verdict.get('cmd')!r}, not the canonical "
            f"{cfg['verify_cmd']!r}"
        )
    age = time.time() - float(verdict.get("at_epoch", 0))
    if age > float(cfg["verdict_max_age_s"]):
        return no(f"verdict is {int(age)}s old (limit {cfg['verdict_max_age_s']}s)")
    if float(verdict.get("at_epoch", 0)) <= float(receipt["recovered_at_epoch"]):
        return no("the verdict predates the recovery; re-run the acceptance command")

    # What the oracle ran over, before it started and after it finished, and
    # whether that is still what is being committed.
    drift = inputs_differences(verdict.get("inputs_before"), verdict.get("inputs_after"))
    if not verdict.get("inputs_before") or not verdict.get("inputs_after"):
        return no("the verdict did not capture its acceptance inputs; re-run the acceptance command")
    if drift or verdict.get("inputs_drift"):
        return no("the tree changed while the oracle ran: " + ", ".join(drift))
    unclassified = verdict["inputs_after"].get("unclassified") or []
    if unclassified:
        return no(
            "ignored acceptance inputs are neither bound nor excluded: " + ", ".join(unclassified)
            + ". Declare each under recovery_inputs.include (bound and hashed) or "
            ".exclude (accepted as outside the oracle) in .gate/config.json."
        )
    allowed = {queue_rel}
    now = acceptance_inputs(repo, cfg)
    moved = [path for path in inputs_differences(verdict["inputs_after"], now)
             if path not in allowed]
    if moved:
        return no(
            "the tree changed after the acceptance run: " + ", ".join(moved)
            + ". A recovered completion may only add the queue row it closes."
        )

    binding = (verdict.get("recovery") or {}).get(task)
    if not binding:
        return no("the verdict carries no recovery binding for this task; re-run the acceptance command")
    if binding.get("error"):
        return no(f"the acceptance run could not bind the declared sources: {binding['error']}")
    if binding.get("receipt") != receipt["sha256"]:
        return no("the verdict is bound to a different recovery receipt")
    if binding.get("sources_digest") != receipt["sources_digest"]:
        return no("the declared sources changed between the acceptance run and this commit")

    # verdict.json is local and writable: on its own it is a claim, not an
    # acceptance run. The engine's append-only journal is what says the run
    # happened, so the two have to agree.
    recorded = [event for event in journal_events(repo)
                if event.get("event") == "acceptance"
                and event.get("verdict_digest") == digest(verdict)]
    if not recorded:
        return no(
            "this verdict was not recorded by this engine — no acceptance event in the "
            "journal matches it. Run the acceptance command through the gate."
        )

    expected = _end_baseline_for(repo, cfg, ":", task)
    if expected is not None and verdict.get("count") is not None:
        if int(verdict["count"]) != int(expected):
            return no(
                f"queue declares end baseline {expected} for {task} but the acceptance "
                f"run measured {verdict['count']}"
            )
    return {
        "allowed": True,
        "receipt": receipt["sha256"],
        "verdict": verdict,
        "queue_file": queue_rel,
        "detail": (
            f"{task} is a recovered claim: implementation "
            f"{receipt['implementation']['commit'][:10]} revalidated against receipt "
            f"{receipt['sha256'][:10]}, fresh {verdict['result']} (count {verdict.get('count')}) "
            "from the canonical oracle over an unchanged tree"
        ),
    }


def record_completion(repo: Path, cfg: dict, task: str, allowance: dict) -> dict:
    """Append what this commit was allowed on, bound to the commit it becomes.

    Without this record an audit cannot tell a verified recovered closure from
    a queue edit made straight after a recovery, and must not try.
    """
    queue_rel = allowance["queue_file"]
    verdict = allowance["verdict"]
    record = {
        "at": _stamp(),
        "task": task,
        "receipt": allowance["receipt"],
        "parent": _out(repo, "rev-parse", "HEAD"),
        "queue_blob": _out(repo, "rev-parse", f":{queue_rel}"),
        "inputs_digest": digest(verdict.get("inputs_after")),
        "verdict_at_epoch": verdict.get("at_epoch"),
        "verdict_count": verdict.get("count"),
    }
    raise_barrier(repo, task, allowance["receipt"], "hook completion requires independent review")
    _append(completions_path(repo), record)
    return record


# ------------------------------------------------------------------ audit


def audit_recovered(repo: Path, cfg: dict, sha: str, flipped: list[str]) -> tuple[str, bool] | None:
    """Classify a queue-only DONE. Returns (label, clean) or None for 'not one'.

    A receipt alone never blesses a commit: the acceptance that allowed it must
    be recorded against this very commit, and the review it owes must be done.
    """
    repo = Path(repo)
    if len(flipped) != 1:
        return None
    task = flipped[0]
    parent = _out(repo, "rev-parse", f"{sha}^")
    try:
        valid, _ = receipts(repo, cfg, task)
        if not valid:
            return None
        receipt = valid[0]
        blob_here = _out(repo, "rev-parse", f"{sha}:{receipt['scope']['queue_file']}")
        records = [
            record for record in _read_ndjson(completions_path(repo), "completions")
            if record.get("task") == task
            and record.get("parent") == parent
            and record.get("queue_blob") == blob_here
            and record.get("receipt") == receipt["sha256"]
        ]
        if not records:
            return None
        at_done = source_state(repo, cfg, receipt["scope"]["declared_files"], sha)
    except (RecoveryError, ValueError, KeyError):
        return None
    if any(at_done[rel]["blob"] != receipt["sources"].get(rel, {}).get("blob") for rel in at_done):
        return None
    label = (f"RECOVERED (implementation {receipt['implementation']['commit'][:10]}, "
             f"receipt {receipt['sha256'][:10]})")
    reviews = _read_ndjson(barrier_path(repo), "review barriers")
    approved = any(e.get("event") == "barrier_cleared" and e.get("by") == "judge"
                   and e.get("task") == task and e.get("receipt") == receipt["sha256"]
                   and e.get("closure") == sha and e.get("completion") in [digest(r) for r in records]
                   for e in reviews)
    if task in open_barriers(repo) or not approved:
        return f"{label} — REVIEW PENDING", False
    return label, True


# --------------------------------------------------------- review barriers


def barrier_path(repo: Path) -> Path:
    return Path(repo) / GATE_DIR / BARRIER_FILE


def open_barriers(repo: Path) -> dict[str, dict]:
    """Tasks whose recovered work still owes an independent review.

    Durable on purpose: a DONE row is not evidence of review, and a crash
    between closing and judging must not become an accidental approval.
    """
    state, cleared = _raw_barriers(repo, with_cleared=True)
    # Closure can happen outside the runner or without the hook. Absence of
    # a barrier event must never imply approval of a recovered DONE row.
    entries = _read_ndjson(index_path(repo), "recovery index")
    if entries:
        cfg = _g("load_config")(Path(repo))
        queue = Path(repo) / _prefixed(cfg, cfg["queue_file"])
        tasks = _parse_queue(queue.read_text(encoding="utf-8"))
        latest = {e["task"]: e for e in entries}
        for task, entry in latest.items():
            if tasks.get(task, {}).get("status") not in cfg["done_markers"]:
                continue
            approval = cleared.get(task, {})
            if approval.get("by") == "judge" and approval.get("receipt") == entry["receipt"]:
                continue
            state.setdefault(task, {"task": task, "receipt": entry["receipt"],
                                    "reason": "recovered closure lacks affirmative independent review"})
    return state


def raise_barrier(repo: Path, task: str, receipt: str, reason: str) -> None:
    if task in open_barriers(repo):
        return
    _append(barrier_path(repo), {"at": _stamp(), "event": "barrier_raised", "task": task,
                                 "receipt": receipt, "reason": reason, "pid": os.getpid()})


def _raw_barriers(repo: Path, with_cleared: bool = False):
    """Fold the barrier log alone: raised minus cleared, no reconstruction."""
    state: dict[str, dict] = {}
    cleared: dict[str, dict] = {}
    for event in _read_ndjson(barrier_path(repo), "review barriers"):
        task = event.get("task")
        if not task:
            continue
        if event.get("event") == "barrier_raised":
            state[task] = event
        elif event.get("event") == "barrier_cleared":
            state.pop(task, None)
            cleared[task] = event
    return (state, cleared) if with_cleared else state


def retire_dispatch_barrier(repo: Path, cfg: dict, task: str, note: str) -> bool:
    """Retire the barrier a dispatch raised when that dispatch closed nothing.

    Raised before the worker starts, the barrier guards a crash between a DONE
    commit and its review. If the row is not DONE at HEAD or in the worktree,
    nothing was closed and there is nothing to review; leaving it would block
    every later run for a review no judge can ever perform. A DONE row is
    never retired here: `open_barriers` reconstructs that obligation from the
    recovery history whatever this record says.
    """
    queue_rel = _prefixed(cfg, cfg["queue_file"])
    for text in (_blob(repo, "HEAD", queue_rel) or "",
                 (Path(repo) / queue_rel).read_text(encoding="utf-8")):
        if _parse_queue(text).get(task, {}).get("status") in cfg["done_markers"]:
            return False
    barrier = _raw_barriers(repo).get(task)
    if not barrier:
        return False
    _append(barrier_path(repo), {"at": _stamp(), "event": "barrier_cleared", "task": task,
                                 "by": "runner", "evidence": note,
                                 "receipt": barrier.get("receipt"), "pid": os.getpid()})
    return True


def clear_barrier(repo: Path, task: str, by: str, evidence_note: str) -> bool:
    barrier = open_barriers(repo).get(task)
    if not barrier:
        return False
    if by != "judge":
        raise RecoveryError("only an affirmative independent judge may clear a review barrier")
    head = _out(repo, "rev-parse", "HEAD")
    reviews = [e for e in journal_events(repo) if e.get("event") == "judge_verdict"
               and e.get("task") == task and e.get("commit") == head
               and e.get("receipt") == barrier["receipt"] and e.get("passed") is True]
    if not reviews:
        raise RecoveryError("no affirmative judge result bound to this receipt and content")
    records = [e for e in _read_ndjson(completions_path(repo), "completions")
               if e.get("task") == task and e.get("receipt") == barrier["receipt"]]
    if not records:
        raise RecoveryError("no recorded closure for independent review")
    record = records[-1]
    candidates = _out(repo, "rev-list", f"{record['parent']}..{head}").splitlines()
    closure = next((sha for sha in candidates if _out(repo, "rev-parse", f"{sha}^") == record["parent"]), None)
    if not closure:
        raise RecoveryError("reviewed closure is not in current history")
    cfg = _g("load_config")(Path(repo))
    if _out(repo, "rev-parse", f"{closure}:{_prefixed(cfg, cfg['queue_file'])}") != record["queue_blob"]:
        raise RecoveryError("review does not bind the recorded closure content")
    _append(barrier_path(repo), {"at": _stamp(), "event": "barrier_cleared", "task": task,
                                 "receipt": barrier["receipt"], "closure": closure,
                                 "reviewed_head": head, "completion": digest(record),
                                 "by": by, "evidence": evidence_note, "pid": os.getpid()})
    return True


# ------------------------------------------------------ revalidation budget


def lineage_dispatches(repo: Path, task: str, implementation: str) -> list[dict]:
    entries = {e["receipt"]: e.get("implementation") for e in
               _read_ndjson(index_path(repo), "recovery index") if e.get("task") == task}
    return [e for e in journal_events(repo) if e.get("event") == "revalidation_dispatch"
            and e.get("task") == task
            and (e.get("implementation") or entries.get(e.get("receipt"))) == implementation]


def lineage_renewals(repo: Path, task: str, implementation: str) -> list[dict]:
    return [e for e in journal_events(repo) if e.get("event") == "revalidation_renewed"
            and e.get("task") == task and e.get("implementation") == implementation]


def renew_revalidation(repo: Path, cfg: dict, task: str, reason: str) -> dict:
    """Grant one more revalidation dispatch, once, on evidence, never silently.

    Only for the case the protocol itself produced: the single dispatch ran
    the canonical oracle to a PASS and the gate then refused the closure. A
    dispatch whose oracle failed, a task that closed, or a lineage that was
    already renewed is refused. The operator's reason is journalled with the
    grant; there is no flag that grants without one.
    """
    repo = Path(repo)
    if not reason or not reason.strip():
        raise RecoveryError("renewal needs --reason stating why the gate refused a passing closure")
    receipt = receipt_for(repo, cfg, task)
    if receipt is None:
        raise RecoveryError(f"no valid recovery receipt for {task}")
    implementation = receipt["implementation"]["commit"]
    dispatches = lineage_dispatches(repo, task, implementation)
    renewals = lineage_renewals(repo, task, implementation)
    if len(dispatches) != 1 or renewals:
        raise RecoveryError(
            f"renewal is granted once per implementation lineage: {len(dispatches)} dispatch(es), "
            f"{len(renewals)} renewal(s) already"
        )
    events = journal_events(repo)
    start = next(i for i, e in enumerate(events) if e == dispatches[0])
    window = events[start:]
    if any(e.get("event") == "task_done" and e.get("task") == task for e in window):
        raise RecoveryError(f"{task} closed after its revalidation dispatch; nothing to renew")
    ends = [e for e in window if e.get("event") == "task_end" and e.get("task") == task]
    if not ends or ends[-1].get("outcome") != "not_done":
        raise RecoveryError("the revalidation dispatch has not ended not_done")
    if not any(e.get("event") == "run_end" for e in window):
        raise RecoveryError("the run that dispatched the revalidation is still open")
    passes = [e for e in window if e.get("event") == "acceptance" and e.get("result") == "PASS"
              and e.get("cmd") == cfg["verify_cmd"]]
    if not passes:
        raise RecoveryError(
            "the revalidation dispatch recorded no PASS from the canonical oracle; a failed "
            "verification is the task's own result and is not renewed"
        )
    queue = repo / receipt["scope"]["queue_file"]
    status = _parse_queue(queue.read_text(encoding="utf-8")).get(task, {}).get("status")
    if status in cfg["done_markers"]:
        raise RecoveryError(f"{task} is DONE; nothing to renew")
    grant = {"event": "revalidation_renewed", "task": task, "implementation": implementation,
             "receipt": receipt["sha256"], "reason": reason.strip(),
             "after_dispatch": dispatches[0].get("at"), "oracle_pass_at": passes[-1].get("at"),
             "pid": os.getpid()}
    _journal(repo, grant)
    return grant


def revalidation_budget(repo: Path, cfg: dict, task: str, receipt: dict) -> dict:
    """One dispatch, named and counted — never a fresh attempt budget.

    The attempts the task already spent are carried forward, so the original
    limit still bounds it; the single revalidation dispatch is recorded against
    this receipt so a restart cannot renew it.
    """
    events = journal_events(repo)
    recovered_at = None
    for position, event in enumerate(events):
        if event.get("event") == "task_recovered" and event.get("receipt") == receipt["sha256"]:
            recovered_at = position
    prior = [e for e in events[:recovered_at if recovered_at is not None else len(events)]
             if e.get("event") == "attempt_start" and e.get("task") == task]
    used = lineage_dispatches(repo, task, receipt["implementation"]["commit"])
    ends = [e for e in events[:recovered_at if recovered_at is not None else len(events)]
            if e.get("event") == "task_end" and e.get("task") == task]
    return {
        "receipt": receipt["sha256"],
        "prior_attempts": len(prior),
        "prior_outcome": ends[-1].get("outcome") if ends else None,
        "prior_signature": ends[-1].get("signature") if ends else None,
        "allowed": 1 + len(lineage_renewals(repo, task, receipt["implementation"]["commit"])),
        "used": len(used),
    }


def record_revalidation_dispatch(repo: Path, task: str, receipt: dict, budget: dict) -> None:
    current = revalidation_budget(repo, {}, task, receipt)
    if current["used"] >= current["allowed"]:
        raise RecoveryError("revalidation allowance exhausted")
    _journal(repo, {"event": "revalidation_dispatch", "task": task,
                    "implementation": receipt["implementation"]["commit"],
                    "receipt": receipt["sha256"], "prior_attempts": budget["prior_attempts"],
                    "prior_outcome": budget["prior_outcome"], "allowed": budget["allowed"],
                    "pid": os.getpid()})


# ------------------------------------------------------------------ judge


def judge_extra_commits(repo: Path, receipt: dict | None, head: str) -> list[str]:
    """The implementation a recovered task closes, so the reviewer reads the
    code and not only the queue row that closed it."""
    if not receipt:
        return []
    commit = receipt["implementation"]["commit"]
    if _git(Path(repo), "merge-base", "--is-ancestor", commit, head, check=False).returncode != 0:
        return []
    return [commit]

