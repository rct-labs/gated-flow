"""Offline deterministic acceptance tests for IL-15 read-only progress projection."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

GATE_DIR = Path(__file__).resolve().parent
if str(GATE_DIR) not in sys.path:
    sys.path.insert(0, str(GATE_DIR))

from test_compat import FLOW, committed_runner_repo, run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONTINUITY_PATH = ROOT / "flow" / "continuity.py"


def load_continuity():
    spec = importlib.util.spec_from_file_location("flow_continuity_test", CONTINUITY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def flow(*args: str) -> subprocess.CompletedProcess[str]:
    return run(sys.executable, str(FLOW), *args)


def all_files_digest(root: Path) -> dict[str, str]:
    """Include .gate/ and project files so cursor/lock/config/journal writes are caught."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".git" not in path.relative_to(root).parts:
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    return proc.pid


def write_records(repo: Path, *records: dict | bytes | str) -> Path:
    jpath = repo / ".gate" / "journal.ndjson"
    jpath.parent.mkdir(parents=True, exist_ok=True)
    with jpath.open("ab") as fh:
        for rec in records:
            if isinstance(rec, bytes):
                fh.write(rec)
            elif isinstance(rec, str):
                fh.write(rec.encode("utf-8"))
            else:
                fh.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
    return jpath


class ProgressProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _repo(self, name: str = "sub") -> Path:
        sub = self.root / name
        sub.mkdir(parents=True, exist_ok=True)
        repo = committed_runner_repo(sub, ["codex"])
        (repo / "CONTEXT.md").write_text(
            "# CONTEXT - fixture\n\n"
            "## 1. Goal & red lines\nKeep progress read-only.\n\n"
            "## 2. Now\nImplementing IL-15.\n\n"
            "## 3. Last stable checkpoint\nBaseline.\n\n"
            "## 4. Next step\nVerify IL-15.\n\n"
            "## 5. Waiting on a human\nNone.\n",
            encoding="utf-8",
        )
        return repo

    def test_waiting_live_dead_and_unknown_cases(self) -> None:
        cont = load_continuity()

        # 1. Live active run (not waiting on lock).
        live_repo = self._repo("live")
        write_records(
            live_repo,
            {"at": "2026-11-17T01:00:00Z", "event": "run_start", "run": "r-live", "runner_pid": os.getpid(), "stage": "delivery"},
            {"at": "2026-11-17T01:00:02Z", "event": "attempt_start", "task": "IL-15", "worker": "agy-flow", "attempt": 1},
        )
        p_live = cont.build_progress(cont.Project(str(live_repo)))
        self.assertEqual(p_live["state"], "live")
        self.assertEqual(p_live["run"]["state"], "live")
        self.assertEqual(p_live["run"]["liveness"], "live")
        self.assertEqual(p_live["task"], "IL-15")
        self.assertEqual(p_live["acceptance_wait"]["state"], "none")
        self.assertFalse(p_live["acceptance_wait"]["active"])

        # 2. Live run actively waiting on shared full-acceptance lock.
        wait_repo = self._repo("waiting")
        write_records(
            wait_repo,
            {"at": "2026-11-17T01:05:00Z", "event": "run_start", "run": "r-wait", "runner_pid": os.getpid(), "stage": "delivery"},
            {"at": "2026-11-17T01:05:10Z", "event": "full_acceptance_wait", "holder": "/tmp/other-project", "holder_pid": 4125984},
        )
        p_wait = cont.build_progress(cont.Project(str(wait_repo)))
        self.assertEqual(p_wait["state"], "waiting")
        self.assertEqual(p_wait["run"]["state"], "waiting")
        self.assertEqual(p_wait["run"]["liveness"], "live")
        self.assertTrue(p_wait["acceptance_wait"]["active"])
        self.assertEqual(p_wait["acceptance_wait"]["state"], "waiting")
        self.assertEqual(p_wait["acceptance_wait"]["holder"], "/tmp/other-project")
        self.assertEqual(p_wait["acceptance_wait"]["holder_pid"], 4125984)
        self.assertIsNone(p_wait["acceptance_wait"]["waited_s"])

        # 3. Dead runner PID with uncleared full_acceptance_wait MUST NOT assert live waiting.
        dead_repo = self._repo("dead")
        exited = dead_pid()
        write_records(
            dead_repo,
            {"at": "2026-11-17T01:10:00Z", "event": "run_start", "run": "r-dead", "runner_pid": exited, "stage": "delivery"},
            {"at": "2026-11-17T01:10:05Z", "event": "full_acceptance_wait", "holder": "/tmp/other-project", "holder_pid": os.getpid()},
        )
        p_dead = cont.build_progress(cont.Project(str(dead_repo)))
        self.assertEqual(p_dead["state"], "dead")
        self.assertEqual(p_dead["run"]["state"], "dead")
        self.assertEqual(p_dead["run"]["liveness"], "dead")
        self.assertFalse(p_dead["acceptance_wait"]["active"])
        self.assertEqual(p_dead["acceptance_wait"]["state"], "dead")
        dead_txt = cont.render_progress(p_dead)
        self.assertNotIn("acceptance-lock wait: waiting", dead_txt)

        # 4a. Unknown PID (missing runner_pid) with uncleared wait MUST NOT assert live waiting.
        unk_repo = self._repo("unk-missing-pid")
        write_records(
            unk_repo,
            {"at": "2026-11-17T01:15:00Z", "event": "run_start", "run": "r-unk"},
            {"at": "2026-11-17T01:15:05Z", "event": "full_acceptance_wait", "holder": "/other/repo", "holder_pid": 999},
        )
        p_unk = cont.build_progress(cont.Project(str(unk_repo)))
        self.assertEqual(p_unk["state"], "unknown")
        self.assertEqual(p_unk["run"]["liveness"], "unknown")
        self.assertFalse(p_unk["acceptance_wait"]["active"])
        self.assertEqual(p_unk["acceptance_wait"]["state"], "unknown")

        # 4b. Unknown liveness (process_alive returns None, e.g. EPERM) with uncleared wait.
        perm_repo = self._repo("unk-perm")
        write_records(
            perm_repo,
            {"at": "2026-11-17T01:16:00Z", "event": "run_start", "run": "r-perm", "runner_pid": 424242},
            {"at": "2026-11-17T01:16:05Z", "event": "full_acceptance_wait", "holder": "/other/repo", "holder_pid": 999},
        )
        with mock.patch.object(cont, "process_alive", return_value=None):
            p_perm = cont.build_progress(cont.Project(str(perm_repo)))
        self.assertEqual(p_perm["state"], "unknown")
        self.assertEqual(p_perm["run"]["liveness"], "unknown")
        self.assertFalse(p_perm["acceptance_wait"]["active"])
        self.assertEqual(p_perm["acceptance_wait"]["state"], "unknown")

    def test_wait618_fixture_completion_and_missing_historical_waited_s_is_unknown(self) -> None:
        cont = load_continuity()

        # Exact wait618 completion sequence modeled on historical run 20261116-132417-review.
        repo_618 = self._repo("wait618")
        write_records(
            repo_618,
            {
                "at": "2026-11-16T13:24:17Z",
                "event": "run_start",
                "run": "20261116-132417-review",
                "runner_pid": dead_pid(),
                "mode": "review",
                "stage": "delivery",
            },
            {
                "at": "2026-11-16T13:24:17Z",
                "event": "review_start",
                "tasks": ["IL-12", "IL-13"],
                "stage": "delivery",
            },
            {
                "at": "2026-11-16T13:28:43Z",
                "event": "review_verdict",
                "tasks": ["IL-12", "IL-13"],
                "passed": True,
                "score": 97,
            },
            {
                "at": "2026-11-16T13:28:43Z",
                "event": "full_acceptance_wait",
                "holder": "/tmp/other-project",
                "holder_pid": 4125984,
            },
            {
                "at": "2026-11-16T13:39:44Z",
                "event": "acceptance_check",
                "scope": "queue",
                "task": None,
                "seconds": 42.4,
                "waited_s": 618.0,
                "result": "PASS",
                "head": "8a4ad81eb2ba34b1e20b8ee6b07232d6777acf56",
            },
            {
                "at": "2026-11-16T13:39:44Z",
                "event": "full_acceptance",
                "run": "20261116-132417-review",
                "tasks": ["IL-12", "IL-13"],
                "stage": "delivery",
                "result": "PASS",
                "count": 5,
                "seconds": 42.4,
                "log": ".gate/runs/20261116-132417-review/full-acceptance.log",
                "reason": "delivery candidate requires complete acceptance",
                "head": "8a4ad81eb2ba34b1e20b8ee6b07232d6777acf56",
            },
            {
                "at": "2026-11-16T13:39:44Z",
                "event": "package_settled",
                "run": "20261116-132417-review",
                "tasks": ["IL-12", "IL-13"],
                "stage": "delivery",
            },
            {
                "at": "2026-11-16T13:39:44Z",
                "event": "run_end",
                "run": "20261116-132417-review",
                "stop": "queue_empty",
                "seconds": 926.642,
            },
        )
        p618 = cont.build_progress(cont.Project(str(repo_618)))
        self.assertEqual(p618["state"], "finished")
        self.assertFalse(p618["acceptance_wait"]["active"])
        self.assertEqual(p618["acceptance_wait"]["state"], "cleared")
        self.assertEqual(p618["acceptance_wait"]["holder"], "/tmp/other-project")
        self.assertEqual(p618["acceptance_wait"]["holder_pid"], 4125984)
        self.assertEqual(p618["acceptance_wait"]["waited_s"], 618.0)
        self.assertEqual(p618["acceptance_wait"]["cleared_by"], "full_acceptance")
        txt618 = cont.render_progress(p618)
        self.assertIn("618.0s", txt618)
        self.assertIn("/tmp/other-project", txt618)

        # Historical full_acceptance without waited_s (even with timestamps 618s apart!) must report unknown, never 618.
        repo_hist = self._repo("hist-no-waited")
        write_records(
            repo_hist,
            {"at": "2026-11-13T04:00:00Z", "event": "run_start", "run": "r-hist", "runner_pid": dead_pid()},
            {"at": "2026-11-13T04:00:00Z", "event": "full_acceptance_wait", "holder": "/tmp/other-project", "holder_pid": 692960},
            {"at": "2026-11-13T04:10:18Z", "event": "full_acceptance", "run": "r-hist", "result": "PASS", "seconds": 21.0},
            {"at": "2026-11-13T04:10:18Z", "event": "run_end", "run": "r-hist", "stop": "queue_empty", "seconds": 639.0},
        )
        p_hist = cont.build_progress(cont.Project(str(repo_hist)))
        self.assertEqual(p_hist["acceptance_wait"]["state"], "cleared")
        self.assertFalse(p_hist["acceptance_wait"]["active"])
        self.assertIsNone(p_hist["acceptance_wait"]["waited_s"])
        txt_hist = cont.render_progress(p_hist)
        self.assertIn("waited unknown", txt_hist)
        self.assertNotIn("618", txt_hist)

    def test_new_run_and_run_end_reset_prior_state(self) -> None:
        cont = load_continuity()

        # Run end clears an open wait even if acceptance never ran (e.g. run_timeout).
        repo_end = self._repo("end-clears-wait")
        write_records(
            repo_end,
            {"at": "2026-11-17T02:00:00Z", "event": "run_start", "run": "r1", "runner_pid": dead_pid()},
            {"at": "2026-11-17T02:01:00Z", "event": "full_acceptance_wait", "holder": "/other/repo", "holder_pid": 111},
            {"at": "2026-11-17T02:05:00Z", "event": "run_end", "run": "r1", "stop": "run_timeout"},
        )
        p_end = cont.build_progress(cont.Project(str(repo_end)))
        self.assertEqual(p_end["state"], "finished")
        self.assertFalse(p_end["acceptance_wait"]["active"])
        self.assertEqual(p_end["acceptance_wait"]["state"], "cleared")
        self.assertEqual(p_end["acceptance_wait"]["cleared_by"], "run_end")

        # New run_start resets prior uncleared wait, tasks_done, and errors from earlier run.
        write_records(
            repo_end,
            {"at": "2026-11-17T02:06:00Z", "event": "run_start", "run": "r-stale-wait", "runner_pid": dead_pid()},
            {"at": "2026-11-17T02:06:10Z", "event": "task_done", "task": "OLD-TASK", "worker": "codex"},
            {"at": "2026-11-17T02:06:20Z", "event": "worker_error", "task": "OLD-TASK", "worker": "codex", "err": "old_err"},
            {"at": "2026-11-17T02:06:30Z", "event": "full_acceptance_wait", "holder": "/stale/holder", "holder_pid": 222},
            {"at": "2026-11-17T02:10:00Z", "event": "run_start", "run": "r2", "runner_pid": os.getpid(), "stage": "development"},
            {"at": "2026-11-17T02:10:05Z", "event": "attempt_start", "task": "IL-15", "worker": "agy-flow", "attempt": 1},
        )
        p_new = cont.build_progress(cont.Project(str(repo_end)))
        self.assertEqual(p_new["run"]["id"], "r2")
        self.assertEqual(p_new["state"], "live")
        self.assertEqual(p_new["task"], "IL-15")
        self.assertEqual(p_new["meaningful_progress"]["tasks_done"], [])
        self.assertEqual(p_new["acceptance_wait"]["state"], "none")
        self.assertIsNone(p_new["acceptance_wait"]["holder"])
        self.assertIsNone(p_new["last_error"])

    def test_explicit_cross_run_isolation_and_positional_attribution_labeling(self) -> None:
        cont = load_continuity()
        repo = self._repo("cross-run")
        write_records(
            repo,
            {"at": "2026-11-17T03:00:00Z", "event": "run_start", "run": "r-current", "runner_pid": os.getpid(), "stage": "delivery"},
            {"at": "2026-11-17T03:00:05Z", "event": "attempt_start", "task": "IL-15", "worker": "agy-flow", "attempt": 1},
            {"at": "2026-11-17T03:00:10Z", "event": "full_acceptance_wait", "holder": "/lock/holder", "holder_pid": 777},
            # Foreign explicit run IDs and standalone empty run IDs must be isolated.
            {"at": "2026-11-17T03:00:15Z", "event": "task_done", "run": "r-foreign", "task": "FOREIGN-1", "worker": "codex"},
            {"at": "2026-11-17T03:00:16Z", "event": "worker_error", "run": "r-foreign", "task": "FOREIGN-1", "err": "foreign_boom"},
            {"at": "2026-11-17T03:00:17Z", "event": "full_acceptance", "run": "r-foreign", "result": "PASS", "waited_s": 12.0},
            {"at": "2026-11-17T03:00:18Z", "event": "full_acceptance", "run": "", "result": "PASS"},
            {"at": "2026-11-17T03:00:19Z", "event": "run_end", "run": "r-foreign", "stop": "queue_empty"},
        )
        proj = cont.build_progress(cont.Project(str(repo)))
        self.assertEqual(proj["run"]["id"], "r-current")
        self.assertEqual(proj["state"], "waiting")
        self.assertTrue(proj["acceptance_wait"]["active"])
        self.assertEqual(proj["acceptance_wait"]["attribution"], "positional")
        self.assertEqual(proj["meaningful_progress"]["tasks_done"], [])
        self.assertEqual(proj["meaningful_progress"]["last"]["attribution"], "positional")
        self.assertIsNone(proj["last_error"])
        self.assertEqual(proj["integrity"]["cross_run_excluded"], 5)
        rendered = cont.render_progress(proj)
        self.assertIn("positional", rendered)
        self.assertNotIn("FOREIGN-1", rendered)
        self.assertNotIn("foreign_boom", rendered)

        # Matching explicit run ID is labeled explicit, and unscoped events after matching run_end stay outside the run boundary.
        write_records(
            repo,
            {"at": "2026-11-17T03:01:00Z", "event": "full_acceptance", "run": "r-current", "result": "PASS", "waited_s": 50.0},
            {"at": "2026-11-17T03:01:01Z", "event": "run_end", "run": "r-current", "stop": "queue_empty"},
            {"at": "2026-11-17T03:02:00Z", "event": "worker_error", "task": "POST-END-ORPHAN", "err": "outside_boundary"},
            {"at": "2026-11-17T03:02:05Z", "event": "full_acceptance_wait", "holder": "/post/end", "holder_pid": 888},
        )
        proj_after = cont.build_progress(cont.Project(str(repo)))
        self.assertEqual(proj_after["state"], "finished")
        self.assertEqual(proj_after["meaningful_progress"]["last"]["event"], "run_end")
        self.assertEqual(proj_after["meaningful_progress"]["last"]["attribution"], "explicit")
        self.assertFalse(proj_after["acceptance_wait"]["active"])
        self.assertEqual(proj_after["acceptance_wait"]["waited_s"], 50.0)
        self.assertIsNone(proj_after["last_error"])
        self.assertIn("explicit", cont.render_progress(proj_after))

    def test_malformed_truncated_missing_start_and_bad_timestamps_are_explicit_unknowns(self) -> None:
        cont = load_continuity()

        # 1. Missing run_start + malformed records + unterminated trailing record.
        repo_missing = self._repo("missing-start")
        write_records(
            repo_missing,
            b"not-json-at-all\n",
            b"12345\n",
            b"\xff\xfeinvalid-utf8\n",
            {"at": "2026-11-17T04:00:00Z", "event": "attempt_start", "task": "IL-15", "worker": "agy-flow"},
            {"at": "2026-11-17T04:00:05Z", "event": "worker_error", "task": "IL-15", "worker": "agy-flow", "err": "orphan_crash"},
            {"at": "2026-11-17T04:00:10Z", "event": "full_acceptance_wait", "holder": "/orphan/holder", "holder_pid": os.getpid()},
            b'{"at": "2026-11-17T04:00:15Z", "event": "task_done", "task": "HALF-WRITTEN"',
        )
        p_miss = cont.build_progress(cont.Project(str(repo_missing)))
        self.assertEqual(p_miss["state"], "unknown")
        self.assertIsNone(p_miss["run"]["id"])
        self.assertEqual(p_miss["run"]["state"], "unknown")
        self.assertEqual(p_miss["run"]["liveness"], "unknown")
        self.assertFalse(p_miss["acceptance_wait"]["active"])
        self.assertEqual(p_miss["acceptance_wait"]["state"], "unknown")
        self.assertEqual(p_miss["meaningful_progress"]["count"], 0)
        self.assertIsNone(p_miss["meaningful_progress"]["last"])
        self.assertEqual(p_miss["last_error"]["err" if "err" in p_miss["last_error"] else "reason"], "orphan_crash")
        self.assertEqual(p_miss["last_error"]["attribution"], "unattributed")
        self.assertFalse(p_miss["integrity"]["ok"])
        self.assertEqual(p_miss["integrity"]["malformed_records"], 3)
        self.assertTrue(p_miss["integrity"]["unterminated_record"])
        self.assertTrue(p_miss["integrity"]["truncated_tail"])
        self.assertTrue(p_miss["integrity"]["missing_run_start"])
        miss_txt = cont.render_progress(p_miss)
        self.assertIn("missing run_start", miss_txt)
        self.assertIn("incomplete/truncated", miss_txt)
        self.assertIn("malformed", miss_txt)
        self.assertNotIn("HALF-WRITTEN", miss_txt)

        # 2. >256 KiB tail window truncation pushing run_start out of the bounded window.
        repo_big = self._repo("window-truncated")
        write_records(
            repo_big,
            {"at": "2026-11-17T04:10:00Z", "event": "run_start", "run": "r-big", "runner_pid": os.getpid()},
        )
        filler = json.dumps({
            "at": "2026-11-17T04:11:00Z",
            "event": "heartbeat",
            "task": "IL-15",
            "worker": "agy-flow",
            "seconds": 60,
            "last_line": "x" * 900,
        }) + "\n"
        repeats = (cont.JOURNAL_TAIL_BYTES // len(filler)) + 25
        write_records(repo_big, filler * repeats)
        p_big = cont.build_progress(cont.Project(str(repo_big)))
        self.assertTrue(p_big["integrity"]["window_truncated"])
        self.assertTrue(p_big["integrity"]["truncated_tail"])
        self.assertTrue(p_big["integrity"]["missing_run_start"])
        self.assertEqual(p_big["state"], "unknown")

        # 3. Bad timestamps remain explicit None / "unknown".
        repo_ts = self._repo("bad-timestamps")
        write_records(
            repo_ts,
            {"at": "not-a-timestamp", "event": "run_start", "run": "r-ts", "runner_pid": os.getpid()},
            {"event": "attempt_start", "task": "IL-15", "worker": "agy-flow"},
            {"at": "2026-02-30T12:00:00Z", "event": "worker_error", "task": "IL-15", "worker": "agy-flow", "err": "bad_date_err"},
            {"at": 1700000000, "event": "heartbeat", "task": "IL-15", "worker": "agy-flow", "seconds": 120},
        )
        p_ts = cont.build_progress(cont.Project(str(repo_ts)))
        self.assertEqual(p_ts["integrity"]["bad_timestamps"], 4)
        self.assertIsNone(p_ts["run"]["started_at"])
        self.assertIsNone(p_ts["meaningful_progress"]["last"]["at"])
        self.assertIsNone(p_ts["last_error"]["at"])
        self.assertIsNone(p_ts["last_sign_of_life"]["at"])
        ts_txt = cont.render_progress(p_ts)
        self.assertIn("invalid timestamp", ts_txt)
        self.assertNotIn("not-a-timestamp", ts_txt)
        self.assertNotIn("2026-02-30", ts_txt)

    def test_errors_retained_among_heartbeat_noise_and_heartbeats_are_not_progress(self) -> None:
        cont = load_continuity()
        repo = self._repo("heartbeat-noise")
        write_records(
            repo,
            {"at": "2026-11-17T05:00:00Z", "event": "run_start", "run": "r-hb", "runner_pid": os.getpid(), "stage": "delivery"},
            {"at": "2026-11-17T05:00:02Z", "event": "attempt_start", "task": "IL-15", "worker": "codex", "attempt": 1},
            {"at": "2026-11-17T05:00:10Z", "event": "tool_disabled", "task": "IL-15", "worker": "codex", "reason": "quota_exhausted"},
            {"at": "2026-11-17T05:00:12Z", "event": "attempt_start", "task": "IL-15", "worker": "agy-flow", "attempt": 1},
        )
        before = cont.build_progress(cont.Project(str(repo)))
        hb_batch = [
            {
                "at": f"2026-11-17T05:{i:02d}:30Z",
                "event": "heartbeat",
                "task": "IL-15",
                "worker": "agy-flow",
                "seconds": i * 60,
                "lines": i * 15,
                "last_line": "HEARTBEAT_SECRET_TRANSCRIPT_LINE",
            }
            for i in range(1, 35)
        ]
        write_records(
            repo,
            {"at": "2026-11-17T05:00:13Z", "event": "model_call", "call": "c1", "worker": "agy-flow", "role": "worker"},
            *hb_batch,
        )
        after = cont.build_progress(cont.Project(str(repo)))
        self.assertEqual(after["meaningful_progress"], before["meaningful_progress"])
        self.assertEqual(after["last_sign_of_life"]["event"], "heartbeat")
        self.assertTrue(after["last_sign_of_life"]["is_heartbeat"])
        self.assertEqual(after["last_sign_of_life"]["at"], "2026-11-17T05:34:30Z")
        self.assertEqual(after["last_sign_of_life"]["seconds"], 34 * 60.0)
        self.assertEqual(after["last_error"]["event"], "tool_disabled")
        self.assertEqual(after["last_error"]["reason"], "quota_exhausted")
        self.assertEqual(after["last_error"]["worker"], "codex")
        self.assertNotIn("HEARTBEAT_SECRET_TRANSCRIPT_LINE", json.dumps(after))
        self.assertNotIn("HEARTBEAT_SECRET_TRANSCRIPT_LINE", cont.render_progress(after))

        # Root-cause reason and log survive trailing task_end and run_end summary events.
        write_records(
            repo,
            {
                "at": "2026-11-17T05:35:00Z",
                "event": "scope_request",
                "task": "IL-15",
                "worker": "agy-flow",
                "reason": "Need flow/extra.py for caller wiring",
                "log": ".gate/runs/r-hb/IL-15-a1.prompt.log",
            },
            {"at": "2026-11-17T05:35:01Z", "event": "task_end", "task": "IL-15", "outcome": "scope_request"},
            {"at": "2026-11-17T05:35:02Z", "event": "run_end", "run": "r-hb", "stop": "scope_request:IL-15"},
        )
        closed = cont.build_progress(cont.Project(str(repo)))
        self.assertEqual(closed["last_error"]["event"], "scope_request")
        self.assertEqual(closed["last_error"]["reason"], "Need flow/extra.py for caller wiring")
        self.assertEqual(closed["last_error"]["log"], ".gate/runs/r-hb/IL-15-a1.prompt.log")

    def test_cli_and_resume_expose_same_projection_with_byte_identical_journal(self) -> None:
        cont = load_continuity()

        # 1. Absent journal: neither progress nor resume creates journal/cursor/lock files.
        empty_repo = self._repo("absent-journal")
        self.assertFalse((empty_repo / ".gate" / "journal.ndjson").exists())
        digest_empty_before = all_files_digest(empty_repo)
        res_prog_empty = flow("progress", "--repo", str(empty_repo), "--json")
        self.assertEqual(res_prog_empty.returncode, 0, res_prog_empty.stderr)
        res_resume_empty = flow("resume", "--repo", str(empty_repo), "--json")
        self.assertEqual(res_resume_empty.returncode, 0, res_resume_empty.stderr)
        self.assertEqual(json.loads(res_prog_empty.stdout), json.loads(res_resume_empty.stdout)["progress"])
        self.assertEqual(all_files_digest(empty_repo), digest_empty_before)

        # 2. Populated journal: byte-identical journal & .gate directory, identical JSON & shared rendered lines.
        repo = self._repo("cli-parity")
        jpath = write_records(
            repo,
            {"at": "2026-11-17T06:00:00Z", "event": "run_start", "run": "r-cli", "runner_pid": os.getpid(), "stage": "delivery"},
            {"at": "2026-11-17T06:00:05Z", "event": "task_done", "task": "IL-14", "worker": "agy-flow", "commit": "a3550c9f67a56bedcecfb46b12d294c3071868df"},
            {"at": "2026-11-17T06:00:10Z", "event": "attempt_start", "task": "IL-15", "worker": "agy-flow", "attempt": 1},
            {"at": "2026-11-17T06:01:10Z", "event": "heartbeat", "task": "IL-15", "worker": "agy-flow", "seconds": 60, "last_line": "noisy"},
            {"at": "2026-11-17T06:01:20Z", "event": "full_acceptance_wait", "holder": "/tmp/other-project", "holder_pid": 4125984},
        )
        journal_bytes_before = jpath.read_bytes()
        repo_digest_before = all_files_digest(repo)

        # Python API does zero subprocess or network calls.
        def _forbid(*_a, **_kw):
            raise AssertionError("build_progress must not spawn subprocesses or open sockets")

        p_obj = cont.Project(str(repo))
        with (
            mock.patch.object(subprocess, "Popen", side_effect=_forbid),
            mock.patch.object(subprocess, "run", side_effect=_forbid),
            mock.patch.object(socket, "create_connection", side_effect=_forbid),
            mock.patch.object(urllib.request, "urlopen", side_effect=_forbid),
        ):
            api_prog = cont.build_progress(p_obj)
        self.assertEqual(api_prog["state"], "waiting")

        cli_prog_json = flow("progress", "--repo", str(repo), "--json")
        cli_prog_txt = flow("progress", "--repo", str(repo))
        cli_res_json = flow("resume", "--repo", str(repo), "--json")
        cli_res_txt = flow("resume", "--repo", str(repo))

        for completed in (cli_prog_json, cli_prog_txt, cli_res_json, cli_res_txt):
            self.assertEqual(completed.returncode, 0, completed.stderr)

        prog_payload = json.loads(cli_prog_json.stdout)
        resume_payload = json.loads(cli_res_json.stdout)
        self.assertEqual(prog_payload, api_prog)
        self.assertEqual(prog_payload, resume_payload["progress"])

        for bullet in cont.progress_lines(prog_payload):
            self.assertIn(bullet, cli_prog_txt.stdout)
            self.assertIn(bullet, cli_res_txt.stdout)

        self.assertEqual(jpath.read_bytes(), journal_bytes_before)
        self.assertEqual(all_files_digest(repo), repo_digest_before)
        self.assertFalse((repo / ".gate" / "watch-journal.cursor.json").exists())


if __name__ == "__main__":
    unittest.main()
