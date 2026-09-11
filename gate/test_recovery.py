"""Supported claim recovery: evidence gets in, fake completion still does not.

Every repository here is a throwaway synthetic Git repo; the worker, the judge
and the process census are mocked. Nothing in this file runs a real CLI, touches
a real project, or performs a real recovery.

The counterexample classes at the end are the acceptance criteria of the ten
findings an independent review reproduced against the first implementation:
each one is the reviewer's attack, and each one must now fail.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import recovery
from test_compat import load_gate_module, new_repo, run

HERE = Path(__file__).resolve().parent
TASK = "PRA-06"
OTHER = "PRA-07"
RUN_ID = "20260911-045608"
REASON = f"not_done:{TASK}"
SOURCE = "src/feature.py"
SUITE = "tests/test_feature.py"
PLAN = "docs/work/PRA/PLAN.md"
VERIFY_CMD = f'"{sys.executable}" -c "print(\'2 passed\')"'
JUDGE_JSON = json.dumps(
    {"score": 92, "verdict": "pass", "findings": [], "revision_brief": ""}
)
IMPLEMENTATION = "def feature():\n    return 2\n"


def stopped_pid() -> int:
    """A pid that is really gone: nothing else proves 'not running' as cheaply."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


DEAD_PID = stopped_pid()


def queue_text(status: str = "IN_PROGRESS", scope: str = f"{SOURCE}, {SUITE}",
               name: str = "feature returns two", extra_rows: str = "",
               extra_notes: str = "") -> str:
    return (
        "# TASK_QUEUE\n\n"
        "| # | ID | name | status | start baseline | end baseline |\n"
        "|---|---|---|---|---|---|\n"
        f"| 1 | {TASK} | {name} | `{status}` | 1 passed | 2 passed |\n"
        f"| 2 | {OTHER} | next | `TODO` | 2 passed | 3 passed |\n"
        f"{extra_rows}\n"
        f"<!-- task:{TASK} files: {scope} -->\n"
        f"<!-- task:{OTHER} files: src/other.py -->\n"
        f"{extra_notes}"
    )


def plan_text(acceptance: str = "`feature()` returns 2 and the suite passes.",
              other: str = "Unrelated to this recovery.") -> str:
    return (
        "# Work package PRA\n\n"
        "Bookkeeping: owner, dates, and a status table nobody implements from.\n\n"
        f"## {TASK} — feature returns two\n\n"
        f"Acceptance: {acceptance}\n\n"
        f"## {OTHER} — next\n\n"
        f"Acceptance: {other}\n"
    )


def config(prefix: str = "", **over) -> dict:
    cfg = {
        "project_prefix": prefix,
        "queue_file": "TASK_QUEUE.md",
        "verify_cmd": VERIFY_CMD,
        "workers": ["claude"],
        "worker_prompt": "Execute exactly task {task}.",
        "max_tasks": 1,
        "max_attempts_per_task": 1,
        "strict_admit": False,
        "probe": {"enabled": False},
        "judge": {"enabled": False},
        "execution": {"defaults": {
            "workers": {"claude": {"model": "claude-test-model", "reasoning_effort": "medium"}},
            "judges": {"codex": {"model": "codex-test-model", "reasoning_effort": "medium"}},
        }},
    }
    cfg.update(over)
    return cfg


def stopped_journal(stop: str = REASON, closed: bool = True, outcome: str = "not_done",
                    task: str = TASK, run_id: str = RUN_ID, attempts: int = 1,
                    pid: int | None = DEAD_PID) -> list[dict]:
    """The journal a crashed run leaves behind. Stamped after the commits the
    fixture makes, because an implementation cannot postdate its own run."""
    base = time.time() + 300

    def at(offset: int) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base + offset))

    events = [{"at": at(0), "event": "run_start", "run": run_id, "workers": ["claude"],
               "pid": pid}]
    for attempt in range(attempts):
        events.append({"at": at(1 + attempt), "event": "attempt_start", "task": task,
                       "worker": "claude", "attempt": attempt + 1})
    events.append({"at": at(8), "event": "task_end", "task": task, "outcome": outcome,
                   "attempts": attempts, "signature": "0:worker stopped without closing"})
    if closed:
        events.append({"at": at(9), "event": "run_end", "run": run_id, "stop": stop,
                       "pid": pid})
    return events


def write_journal(repo: Path, events: list[dict]) -> None:
    path = repo / ".gate" / "journal.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def git(env_or_repo, *args: str):
    repo = getattr(env_or_repo, "repo", env_or_repo)
    result = run("git", "-C", str(repo), *args)
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)}: {result.stderr}")
    return result.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_queue(path: Path, text: str, crlf: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.replace("\n", "\r\n") if crlf else text,
                    encoding="utf-8", newline="")


def build(root: Path, *, status: str = "IN_PROGRESS", prefix: str = "", crlf: bool = False,
          journal: list[dict] | None = None, acceptance_note: str = "",
          **cfg_over) -> SimpleNamespace:
    """A repo shaped like the real blocker: the implementation is committed,
    the claim is still IN_PROGRESS, and the run that held it is closed."""
    root.mkdir(parents=True, exist_ok=True)
    repo = new_repo(root)
    git(repo, "config", "user.name", "Gate Test")
    git(repo, "config", "user.email", "gate@example.invalid")
    # No normalization on checkin: whatever the tests write is what git stores,
    # so a line-ending rewrite cannot hide behind core.autocrlf.
    git(repo, "config", "core.autocrlf", "false")
    proj = repo / prefix if prefix else repo
    queue_rel = f"{prefix}/TASK_QUEUE.md" if prefix else "TASK_QUEUE.md"
    here = (prefix + "/") if prefix else ""

    write(repo / ".gate" / "config.json", json.dumps(config(prefix, **cfg_over), indent=2))
    write_queue(proj / "TASK_QUEUE.md", queue_text("TODO"), crlf)
    write(proj / PLAN, plan_text())
    if acceptance_note:
        write(proj / acceptance_note,
              f"Bookkeeping line mentioning {TASK}.\n\nSecond block, no headings.\n")
    write(proj / "src" / "other.py", "value = 0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "queue")

    claim = None
    if status != "TODO":
        write_queue(proj / "TASK_QUEUE.md", queue_text(status), crlf)
        git(repo, "add", queue_rel)
        git(repo, "commit", "-q", "-m", f"claim {TASK}")
        claim = git(repo, "rev-parse", "HEAD")

    write(proj / SOURCE, IMPLEMENTATION)
    write(proj / SUITE,
          "from src.feature import feature\n\n\ndef test_feature():\n    assert feature() == 2\n")
    git(repo, "add", f"{here}{SOURCE}", f"{here}{SUITE}")
    git(repo, "commit", "-q", "-m", f"{TASK}: implement feature")
    implementation = git(repo, "rev-parse", "HEAD")

    write(proj / "src" / "other.py", "value = 1\n")
    git(repo, "add", f"{here}src/other.py")
    git(repo, "commit", "-q", "-m", "unrelated later work")
    unrelated = git(repo, "rev-parse", "HEAD")

    write_journal(repo, stopped_journal() if journal is None else journal)
    return SimpleNamespace(repo=repo, proj=proj, queue_rel=queue_rel, claim=claim,
                           implementation=implementation, unrelated=unrelated,
                           prefix=prefix, here=here, queue=proj / "TASK_QUEUE.md")


class RecoveryTestCase(unittest.TestCase):
    # A census with our own lineage in it and no writer of this repository.
    QUIET_CENSUS = [(0, 0, "System Idle Process"), (1, 0, "/sbin/init"),
                    (os.getpid(), 1, f"{sys.executable} -m unittest")]

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gate = load_gate_module()
        # gate.py binds itself on import; make the copy under test explicit so
        # the order tests happen to run in cannot matter.
        recovery.bind(vars(self.gate))
        patcher = mock.patch.dict(os.environ, {"GATE_CONFIG": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        # A test that interrupts a run leaves its engine lock open; the OS
        # would free it on exit, but the temporary directory is cleaned first.
        self.addCleanup(recovery.release_all_locks)

    def cfg(self, env) -> dict:
        return self.gate.load_config(env.repo)

    @contextlib.contextmanager
    def census(self, extra=()):
        with mock.patch.object(recovery, "process_census",
                               return_value=self.QUIET_CENSUS + list(extra)):
            yield

    def recover(self, env, *, task=TASK, implementation=None, run_id=RUN_ID, reason=REASON,
                extra_census=(), **kwargs):
        with self.census(extra_census):
            return recovery.recover(env.repo, self.cfg(env), task,
                                    implementation or env.implementation, run_id, reason,
                                    **kwargs)

    def plan(self, env, **kwargs):
        with self.census(kwargs.pop("extra_census", ())):
            return recovery.plan(env.repo, self.cfg(env), kwargs.pop("task", TASK),
                                 kwargs.pop("implementation", None) or env.implementation,
                                 kwargs.pop("run_id", RUN_ID), kwargs.pop("reason", REASON),
                                 **kwargs)

    def commit_row(self, env, message: str = "recover row") -> None:
        git(env, "add", env.queue_rel)
        git(env, "commit", "-q", "-m", message)

    def verify(self, env) -> dict:
        return self.gate.run_acceptance(env.repo, self.cfg(env))

    def stage_done(self, env, task: str = TASK) -> None:
        text = env.queue.read_text(encoding="utf-8")
        row = recovery.queue_row(text, task)
        status = self.gate.parse_queue(text)[task]["status"]
        env.queue.write_text(text.replace(row, recovery.flipped_row(row, status, "DONE")),
                             encoding="utf-8", newline="")
        git(env, "add", env.queue_rel)

    def check_commit(self, env):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.gate.cmd_check_commit(SimpleNamespace(repo=str(env.repo)))
        return stream.getvalue()

    def blocked(self, env) -> str:
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                self.gate.cmd_check_commit(SimpleNamespace(repo=str(env.repo)))
        return stderr.getvalue()

    def recovered_and_verified(self, env) -> dict:
        """The whole supported path up to the moment of the queue-only commit."""
        receipt = self.recover(env)
        self.commit_row(env)
        self.verify(env)
        self.stage_done(env)
        return receipt

    def close_recovered(self, env) -> dict:
        """…and the commit itself, through Gate 1."""
        receipt = self.recovered_and_verified(env)
        self.check_commit(env)
        git(env, "commit", "-q", "-m", f"close {TASK} after recovery")
        return receipt


class RecoverTaskValidationTests(RecoveryTestCase):
    def test_happy_path_restores_one_row_to_todo_and_records_evidence(self) -> None:
        env = build(self.root)
        before = env.queue.read_text(encoding="utf-8")
        receipt = self.recover(env)
        after = env.queue.read_text(encoding="utf-8")

        tasks = self.gate.parse_queue(after)
        self.assertEqual(tasks[TASK]["status"], "TODO")
        self.assertEqual(tasks[OTHER]["status"], "TODO")
        self.assertEqual(self.gate.queue_status_changes(before, after),
                         {TASK: ("IN_PROGRESS", "TODO")})
        self.assertNotIn("DONE", after)

        stored = json.loads(
            recovery.receipt_path(env.repo, receipt["sha256"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["sha256"], recovery.receipt_digest(stored))
        self.assertEqual(stored["queue_transition"]["to"], "TODO")
        self.assertEqual(stored["implementation"]["commit"], env.implementation)
        self.assertEqual(sorted(stored["sources"]), [SOURCE, SUITE])
        self.assertEqual(stored["ownership"]["owner_pid"], DEAD_PID)
        self.assertEqual(list(stored["requirement"]["sources"]), [PLAN])
        index = [json.loads(line) for line in
                 recovery.index_path(env.repo).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(e["task"], e["receipt"]) for e in index],
                         [(TASK, receipt["sha256"])])
        self.assertEqual(git(env, "rev-list", "--count", "HEAD"), "4")

    def test_recovery_after_a_claim_only_commit_keeps_that_commit(self) -> None:
        env = build(self.root)
        self.assertEqual(
            git(env, "show", "--name-only", "--format=", env.claim).split(), [env.queue_rel])
        self.recover(env)
        self.assertEqual(git(env, "rev-parse", env.claim), env.claim)
        self.assertEqual(
            self.gate.parse_queue(git(env, "show", f"{env.claim}:{env.queue_rel}"))[TASK]["status"],
            "IN_PROGRESS")

    def test_the_queue_keeps_its_bytes_apart_from_one_status_cell(self) -> None:
        for crlf in (False, True):
            with self.subTest("crlf" if crlf else "lf"):
                env = build(self.root / f"eol-{int(crlf)}", crlf=crlf)
                before = env.queue.read_bytes()
                self.recover(env)
                after = env.queue.read_bytes()
                self.assertEqual(after.count(b"\r\n"), before.count(b"\r\n"))
                self.assertEqual(before.count(b"\n"), after.count(b"\n"))
                changed = [(b, a) for b, a in
                           zip(before.splitlines(keepends=True), after.splitlines(keepends=True))
                           if b != a]
                self.assertEqual(len(changed), 1)
                self.assertEqual(changed[0][0].replace(b"`IN_PROGRESS`", b"`TODO`"), changed[0][1])

    def test_open_run_or_mismatched_stop_is_refused(self) -> None:
        cases = {
            "has no run_end": (stopped_journal(closed=False), REASON),
            "has no run_start": (stopped_journal(run_id="20260911-999999"), REASON),
            "does not name": (stopped_journal(stop="budget"), "budget"),
            "stopped with": (stopped_journal(stop="not_done:PRA-09"), REASON),
            "never dispatched": (
                [e for e in stopped_journal() if e["event"] != "attempt_start"], REASON),
            "ended PRA-06 as 'done'": (stopped_journal(outcome="done"), REASON),
        }
        for expected, (events, reason) in cases.items():
            with self.subTest(expected):
                env = build(self.root / expected.replace(" ", "-")[:20], journal=events)
                with self.assertRaisesRegex(recovery.RecoveryError, expected):
                    self.recover(env, reason=reason)

    def test_a_later_open_run_blocks_recovery_but_an_earlier_one_does_not(self) -> None:
        later = stopped_journal() + [
            {"at": "2026-09-11T09:00:00Z", "event": "run_start", "run": "20260911-090000"},
        ]
        env = build(self.root / "later", journal=later)
        with self.assertRaisesRegex(recovery.RecoveryError, "open run"):
            self.recover(env)

        earlier = [
            {"at": "2026-09-10T22:00:00Z", "event": "run_start", "run": "20260910-220000"},
            {"at": "2026-09-10T22:01:00Z", "event": "attempt_start", "task": TASK,
             "worker": "claude", "attempt": 1},
        ] + stopped_journal()
        env = build(self.root / "earlier", journal=earlier)
        self.recover(env)
        self.assertEqual(
            self.gate.parse_queue(env.queue.read_text(encoding="utf-8"))[TASK]["status"], "TODO")

    def test_redispatched_task_must_be_recovered_from_its_later_run(self) -> None:
        events = stopped_journal() + [
            {"at": "2026-09-11T09:00:00Z", "event": "run_start", "run": "20260911-090000"},
            {"at": "2026-09-11T09:01:00Z", "event": "attempt_start", "task": TASK,
             "worker": "claude"},
            {"at": "2026-09-11T09:02:00Z", "event": "run_end", "run": "20260911-090000",
             "stop": REASON},
        ]
        env = build(self.root, journal=events)
        with self.assertRaisesRegex(recovery.RecoveryError, "dispatched again"):
            self.recover(env)

    def test_unrelated_non_ancestor_or_claim_only_commit_is_refused(self) -> None:
        env = build(self.root)
        branch = git(env, "rev-parse", "--abbrev-ref", "HEAD")
        git(env, "checkout", "-q", "-b", "side")
        write(env.proj / SOURCE, "def feature():\n    return 3\n")
        git(env, "add", SOURCE)
        git(env, "commit", "-q", "-m", f"{TASK}: side implementation")
        side = git(env, "rev-parse", "HEAD")
        git(env, "checkout", "-q", branch)

        for expected, commit in (
            ("outside the declared scope", env.unrelated),
            ("not an ancestor", side),
            ("declares no code change", env.claim),
        ):
            with self.subTest(expected):
                with self.assertRaisesRegex(recovery.RecoveryError, expected):
                    self.recover(env, implementation=commit)
        with self.assertRaisesRegex(recovery.RecoveryError, "not a commit"):
            self.recover(env, implementation="0" * 40)

    def test_a_commit_titled_like_the_task_proves_nothing(self) -> None:
        env = build(self.root)
        write(env.proj / "NEXT_SESSION.md", f"{TASK} is finished, honest.\n")
        git(env, "add", "NEXT_SESSION.md")
        git(env, "commit", "-q", "-m", f"{TASK}: implement feature (done)")
        titled = git(env, "rev-parse", "HEAD")
        with self.assertRaisesRegex(recovery.RecoveryError, "declares no code change"):
            self.recover(env, implementation=titled)

    def test_only_a_stopped_in_progress_claim_of_that_task_is_recoverable(self) -> None:
        env = build(self.root)
        with self.assertRaisesRegex(recovery.RecoveryError, f"{OTHER} is TODO"):
            self.recover(env, task=OTHER, reason=f"not_done:{OTHER}")
        with self.assertRaisesRegex(recovery.RecoveryError, "not in"):
            self.recover(env, task="PRA-99", reason="not_done:PRA-99")
        done = build(self.root / "done", status="DONE")
        with self.assertRaisesRegex(recovery.RecoveryError, "is DONE"):
            self.recover(done)

    def test_changed_scope_or_content_is_refused(self) -> None:
        env = build(self.root)
        text = env.queue.read_text(encoding="utf-8")
        env.queue.write_text(text.replace(f"files: {SOURCE}, {SUITE}", f"files: {SOURCE}"),
                             encoding="utf-8", newline="")
        with self.assertRaisesRegex(recovery.RecoveryError, "scope at HEAD differs"):
            self.recover(env)
        env.queue.write_text(text, encoding="utf-8", newline="")

        write(env.proj / SOURCE, "def feature():\n    return 99\n")
        with self.assertRaisesRegex(recovery.RecoveryError, "staged or modified"):
            self.recover(env)
        git(env, "add", SOURCE)
        with self.assertRaisesRegex(recovery.RecoveryError, "staged or modified"):
            self.recover(env)
        git(env, "commit", "-q", "-m", "later edit to declared source")
        with self.assertRaisesRegex(recovery.RecoveryError, "declared content changed"):
            self.recover(env)

    def test_scope_change_since_the_implementation_is_refused(self) -> None:
        env = build(self.root)
        text = env.queue.read_text(encoding="utf-8")
        env.queue.write_text(text.replace(f"files: {SOURCE}, {SUITE}",
                                          f"files: {SOURCE}, {SUITE}, src/other.py"),
                             encoding="utf-8", newline="")
        git(env, "add", env.queue_rel)
        git(env, "commit", "-q", "-m", "widen scope")
        with self.assertRaisesRegex(recovery.RecoveryError, "declared scope of .* changed since"):
            self.recover(env)

    def test_an_edited_task_row_is_somebody_elses_work(self) -> None:
        env = build(self.root)
        text = env.queue.read_text(encoding="utf-8")
        env.queue.write_text(text.replace("| feature returns two |", "| mine now |"),
                             encoding="utf-8", newline="")
        with self.assertRaisesRegex(recovery.RecoveryError, "differs from HEAD"):
            self.recover(env)

        self.stage_done(env)
        with self.assertRaisesRegex(
            recovery.RecoveryError, r"worktree DONE, HEAD IN_PROGRESS.*commit or revert"
        ):
            self.recover(env)

    def test_project_prefix_repo_binds_prefixed_paths(self) -> None:
        env = build(self.root, prefix="app")
        receipt = self.recover(env)
        self.assertEqual(receipt["sources"][SOURCE]["path"], f"app/{SOURCE}")
        self.assertEqual(receipt["scope"]["queue_file"], "app/TASK_QUEUE.md")
        self.assertEqual(list(receipt["requirement"]["sources"]), [f"app/{PLAN}"])
        self.assertEqual(receipt["implementation"]["declared_paths_changed"],
                         [f"app/{SOURCE}", f"app/{SUITE}"])
        self.assertEqual(
            self.gate.parse_queue(env.queue.read_text(encoding="utf-8"))[TASK]["status"], "TODO")

    def test_dry_run_writes_nothing(self) -> None:
        env = build(self.root)
        before = env.queue.read_text(encoding="utf-8")
        stream = io.StringIO()
        with self.census(), contextlib.redirect_stdout(stream):
            self.gate.cmd_recover_task(SimpleNamespace(
                repo=str(env.repo), task=TASK, implementation=env.implementation,
                run=RUN_ID, reason=REASON, note=None, dry_run=True, stopped_pid=None,
                requirement=None, requirement_mode=None))
        self.assertIn("is recoverable", stream.getvalue())
        self.assertEqual(env.queue.read_text(encoding="utf-8"), before)
        self.assertFalse(recovery.index_path(env.repo).exists())

    def test_refusal_is_journalled_and_exits_nonzero(self) -> None:
        env = build(self.root)
        with self.census(), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.gate.cmd_recover_task(SimpleNamespace(
                    repo=str(env.repo), task=TASK, implementation=env.unrelated,
                    run=RUN_ID, reason=REASON, note=None, dry_run=False, stopped_pid=None,
                    requirement=None, requirement_mode=None))
        refusals = [e for e in recovery.journal_events(env.repo)
                    if e["event"] == "recovery_refused"]
        self.assertEqual(len(refusals), 1)
        self.assertIn("outside the declared scope", refusals[0]["reason"])


class RecoveredCompletionGateTests(RecoveryTestCase):
    def test_queue_only_done_is_accepted_with_receipt_and_fresh_oracle(self) -> None:
        env = build(self.root)
        receipt = self.recovered_and_verified(env)
        output = self.check_commit(env)
        self.assertIn("recovered claim", output)
        self.assertIn(receipt["sha256"][:10], output)
        self.assertIn("recovered claim", self.check_commit(env))

    def test_queue_only_done_without_a_receipt_is_still_fake_completion(self) -> None:
        env = build(self.root)
        env.queue.write_text(queue_text("TODO"), encoding="utf-8", newline="")
        self.commit_row(env, "no recovery, just a queue edit")
        self.verify(env)
        self.stage_done(env)
        detail = self.blocked(env)
        self.assertIn("fake completion", detail)
        self.assertIn("no valid recovery receipt", detail)

    def test_stale_failed_or_different_oracle_is_refused(self) -> None:
        env = build(self.root)
        receipt = self.recover(env)
        self.commit_row(env)
        self.verify(env)
        self.stage_done(env)

        verdict = self.gate.read_verdict(env.repo)
        for expected, mutation in (
            ("not PASS", {"result": "FAIL"}),
            ("not the canonical", {"cmd": VERIFY_CMD + "  # relaxed"}),
            ("predates the recovery", {"at_epoch": receipt["recovered_at_epoch"] - 1}),
            ("verdict is", {"at_epoch": time.time() - 100_000}),
            ("measured", {"count": 7}),
            ("no recovery binding", {"recovery": {}}),
            ("different recovery receipt", {"recovery": {TASK: {**verdict["recovery"][TASK],
                                                                "receipt": "0" * 64}}}),
            ("changed between", {"recovery": {TASK: {**verdict["recovery"][TASK],
                                                     "sources_digest": "0" * 64}}}),
            ("did not capture", {"inputs_after": None}),
        ):
            with self.subTest(expected):
                self.gate.write_verdict(env.repo, {**verdict, **mutation})
                self.assertIn(expected, self.blocked(env))
        self.gate.write_verdict(env.repo, verdict)
        self.assertIn("recovered claim", self.check_commit(env))

    def test_a_forged_verdict_without_an_acceptance_record_is_refused(self) -> None:
        """verdict.json is writable; inventing one must not invent a run."""
        env = build(self.root)
        receipt = self.recover(env)
        self.commit_row(env)
        cfg = self.cfg(env)
        inputs = recovery.acceptance_inputs(env.repo, cfg)
        sources = recovery.present_sources(env.repo, cfg, receipt["scope"]["declared_files"])
        self.gate.write_verdict(env.repo, {
            "result": "PASS", "cmd": VERIFY_CMD, "count": 2, "exit_code": 0,
            "at_epoch": time.time(), "at": "invented", "elapsed_s": 0.1,
            "head": git(env, "rev-parse", "HEAD"), "tail": "2 passed", "quality": None,
            "inputs_before": inputs, "inputs_after": inputs, "inputs_drift": [],
            "recovery": {TASK: {"receipt": receipt["sha256"], "sources": sources,
                                "sources_digest": recovery.digest(sources)}},
        })
        self.stage_done(env)
        self.assertIn("not recorded by this engine", self.blocked(env))

        # The same tree, closed the supported way, is accepted.
        self.verify(env)
        self.assertIn("recovered claim", self.check_commit(env))

    def test_verifying_before_the_row_is_committed_is_not_fresh_enough(self) -> None:
        env = build(self.root)
        self.recover(env)
        self.verify(env)          # recorded against the pre-recovery HEAD
        self.commit_row(env)
        self.stage_done(env)
        self.assertIn("<head>", self.blocked(env))

    def test_source_or_scope_edited_after_recovery_is_refused(self) -> None:
        env = build(self.root)
        self.recovered_and_verified(env)
        write(env.proj / SOURCE, "def feature():\n    return 3\n")
        self.assertIn("staged or modified", self.blocked(env))

        git(env, "checkout", "--", SOURCE)
        self.assertIn("changed after the acceptance run", self.blocked(env))
        self.verify(env)
        self.assertIn("recovered claim", self.check_commit(env))

        text = env.queue.read_text(encoding="utf-8")
        env.queue.write_text(text.replace(f"files: {SOURCE}, {SUITE}", f"files: {SOURCE}"),
                             encoding="utf-8", newline="")
        git(env, "add", env.queue_rel)
        self.assertIn("declared scope", self.blocked(env))

    def test_a_receipt_cannot_be_replayed_for_another_task(self) -> None:
        env = build(self.root)
        self.recover(env)
        self.commit_row(env)
        self.verify(env)
        self.stage_done(env, task=OTHER)
        self.assertIn(f"no valid recovery receipt for {OTHER}", self.blocked(env))

        self.stage_done(env)
        self.assertIn("closes exactly one task", self.blocked(env))

    def test_normal_tasks_are_unchanged(self) -> None:
        env = build(self.root, status="TODO")
        write(env.proj / SOURCE, "def feature():\n    return 2  # revised\n")
        self.stage_done(env)
        git(env, "add", SOURCE)
        verdict = self.verify(env)
        self.assertEqual(verdict["recovery"], {})
        output = self.check_commit(env)
        self.assertIn("code path(s)", output)
        self.assertNotIn("recovered", output)
        self.assertFalse(recovery.completions_path(env.repo).exists())


class RecoveredRunTests(RecoveryTestCase):
    """The runner closes a recovered task exactly as it closes a normal one:
    one dispatch, the gate's own verdict, and a review that sees the code."""

    JUDGE_ON = {"enabled": True, "chain": ["codex"], "pass_score": 85,
                "max_revisions": 2, "min_gain": 5, "timeout_s": 60}

    def args(self, env, **over):
        return SimpleNamespace(repo=str(env.repo), max_tasks=over.pop("max_tasks", 1),
                               strict_admit=over.pop("strict_admit", False),
                               force=True, **over)

    def worker(self, env, prompts: list[str], close: bool = True, verify_only: bool = False,
               tools: list[str] | None = None):
        def fake_spawn(repo_arg, cfg, tool, prompt, prompt_file, **kwargs):
            prompts.append(prompt)
            if tools is not None:
                tools.append(tool)
            if kwargs.get("extra"):  # the judge, not the worker
                return self.gate.WorkerResult(0, JUDGE_JSON), None
            if verify_only:
                text = env.queue.read_text(encoding="utf-8")
                row = recovery.queue_row(text, TASK)
                env.queue.write_text(
                    text.replace(row, recovery.flipped_row(row, "TODO", "IN_PROGRESS")),
                    encoding="utf-8", newline="")
                git(env, "add", env.queue_rel)
                git(env, "commit", "-q", "-m", f"claim {TASK} again")
                self.verify(env)
                return self.gate.WorkerResult(0, "verified, closure refused"), None
            if not close:
                return self.gate.WorkerResult(0, "could not finish"), None
            text = env.queue.read_text(encoding="utf-8")
            row = recovery.queue_row(text, TASK)
            env.queue.write_text(
                text.replace(row, recovery.flipped_row(row, "TODO", "IN_PROGRESS")),
                encoding="utf-8", newline="")
            git(env, "add", env.queue_rel)
            git(env, "commit", "-q", "-m", f"claim {TASK} again")
            if not recovery.receipt_for(env.repo, self.cfg(env), TASK):
                # No recovery: an ordinary task closes on code it wrote itself.
                write(env.proj / SOURCE, "def feature():\n    return 2  # revised\n")
                git(env, "add", SOURCE)
            self.verify(env)
            self.stage_done(env)
            self.gate.cmd_check_commit(SimpleNamespace(repo=str(env.repo)))
            git(env, "commit", "-q", "-m", f"{TASK} done")
            return self.gate.WorkerResult(0, "closed"), None

        return fake_spawn

    def run_gate(self, env, prompts, close: bool = True, verify_only: bool = False,
                 tools: list[str] | None = None, **over):
        with mock.patch.object(self.gate, "resolve_tool", side_effect=lambda name: name), \
                mock.patch.object(self.gate, "spawn_worker",
                                  side_effect=self.worker(env, prompts, close, verify_only, tools)), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = 0
            try:
                self.gate.cmd_run(self.args(env, **over))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue() + err.getvalue()

    def test_end_to_end_recovery_claim_verify_done_and_review(self) -> None:
        env = build(self.root, judge=self.JUDGE_ON)
        self.recover(env)
        self.commit_row(env)
        prompts: list[str] = []
        code, report = self.run_gate(env, prompts)

        self.assertEqual(code, 0, report)
        self.assertEqual(
            self.gate.parse_queue(env.queue.read_text(encoding="utf-8"))[TASK]["status"], "DONE")
        journal = recovery.journal_events(env.repo)
        self.assertEqual([e["outcome"] for e in journal if e["event"] == "task_end"][-1], "done")
        self.assertEqual([e["verdict"] for e in journal if e["event"] == "judge_verdict"], ["pass"])
        judged = [p for p in prompts if "acceptance judge" in p]
        self.assertEqual(len(judged), 1)
        self.assertIn(env.implementation[:10], judged[0])
        self.assertEqual(recovery.open_barriers(env.repo), {})

    def test_a_recovered_task_never_closes_unreviewed(self) -> None:
        for label, judge in (
            ("disabled", {"enabled": False}),
            ("unavailable", {**self.JUDGE_ON, "chain": []}),
        ):
            with self.subTest(label):
                env = build(self.root / f"unreviewed-{label}", judge=judge)
                self.recover(env)
                self.commit_row(env)
                code, report = self.run_gate(env, [])
                self.assertEqual(code, 3)
                self.assertIn(f"judge_escalated:{TASK}", report)
                self.assertIn(TASK, recovery.open_barriers(env.repo))

    def test_a_normal_task_still_closes_without_a_judge(self) -> None:
        env = build(self.root, status="TODO")
        code, report = self.run_gate(env, [])
        self.assertEqual(code, 0, report)
        self.assertEqual(
            self.gate.parse_queue(env.queue.read_text(encoding="utf-8"))[TASK]["status"], "DONE")
        self.assertNotIn("judge_escalated", report)
        self.assertEqual(recovery.open_barriers(env.repo), {})


# ---------------------------------------------------------------------------
# The ten reproduced findings. Each test is one reviewer counterexample.
# ---------------------------------------------------------------------------


CHILD_LOCK = """
import sys, time
sys.path.insert(0, r"{gate}")
import recovery
lock = recovery.EngineLock(r"{repo}", "child").acquire()
print("locked", flush=True)
sys.stdin.readline()
lock.release()
"""


class F1MutualExclusionTests(RecoveryTestCase):
    """Finding 1: two callers both entered the critical section."""

    def test_the_engine_lock_is_exclusive_in_this_process(self) -> None:
        env = build(self.root)
        first = recovery.EngineLock(env.repo, "first").acquire()
        try:
            with self.assertRaisesRegex(recovery.RecoveryError, "held by another process"):
                recovery.EngineLock(env.repo, "second").acquire()
        finally:
            first.release()
        second = recovery.EngineLock(env.repo, "second").acquire()
        second.release()

    def test_the_engine_lock_is_exclusive_across_processes(self) -> None:
        env = build(self.root)
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD_LOCK.format(gate=str(HERE), repo=str(env.repo))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(recovery.RecoveryError, "held by another process"):
                recovery.EngineLock(env.repo, "parent").acquire()
            with self.assertRaisesRegex(recovery.RecoveryError, "held by another process"):
                self.recover(env)
            self.assertEqual(
                self.gate.parse_queue(env.queue.read_text(encoding="utf-8"))[TASK]["status"],
                "IN_PROGRESS")
        finally:
            child.stdin.write("go\n")
            child.stdin.flush()
            child.wait(timeout=30)
        # The kernel released it the moment that process ended.
        self.assertTrue(recovery.engine_lock_free(env.repo))
        self.recover(env)

    def test_a_dead_holder_leaves_no_stale_lock(self) -> None:
        env = build(self.root)
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD_LOCK.format(gate=str(HERE), repo=str(env.repo))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(child.stdout.readline().strip(), "locked")
        child.kill()
        child.wait(timeout=30)
        self.assertTrue(recovery.engine_lock_free(env.repo))
        self.assertTrue(recovery.gate_path(env.repo, recovery.ENGINE_LOCK).exists())

    def test_release_is_owner_checked(self) -> None:
        env = build(self.root)
        lock = recovery.EngineLock(env.repo, "mine").acquire()
        foreign = {"pid": 4242, "what": "someone else", "token": "not-mine", "released": False}
        lock.meta_path.write_text(json.dumps(foreign), encoding="utf-8")
        lock.release()
        self.assertEqual(json.loads(lock.meta_path.read_text(encoding="utf-8")), foreign)

    def test_run_and_recover_share_one_lock(self) -> None:
        env = build(self.root)
        held = recovery.EngineLock(env.repo, "recover PRA-06").acquire()
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit):
                    self.gate.cmd_run(SimpleNamespace(repo=str(env.repo), max_tasks=1,
                                                      strict_admit=False, force=True))
            self.assertIn("engine lock is held", err.getvalue())
        finally:
            held.release()


class F2OwnershipProofTests(RecoveryTestCase):
    """Finding 2: a closed journal and a missing lock were treated as proof."""

    def test_the_census_runs_and_names_possible_writers(self) -> None:
        env = build(self.root)
        stranded = (4242, 1, 'claude -p "Execute exactly task PRA-06 from the queue"')
        runner = (4243, 1, f'python {env.repo}\\..\\gate\\gate.py run --repo {env.repo}')
        elsewhere = (4244, 1, "python /other/repo/gate/gate.py run --repo /other/repo")
        unrelated = (4245, 1, "notepad PRA-060-notes.txt")
        census = self.QUIET_CENSUS + [stranded, runner, elsewhere, unrelated]
        flagged = recovery.repo_writers(env.repo, TASK, census)
        self.assertEqual(sorted(entry["pid"] for entry in flagged), [4242, 4243])

        for extra in ([stranded], [runner]):
            with self.subTest(extra[0][0]):
                with self.assertRaisesRegex(recovery.RecoveryError, "may still own"):
                    self.recover(env, extra_census=extra)

    def test_our_own_lineage_is_not_a_writer(self) -> None:
        env = build(self.root)
        parent = (99001, 99002, f"bash -c 'gate.py recover-task --repo {env.repo}'")
        grandparent = (99002, 1, "sshd")
        child = (99003, os.getpid(), "powershell census")
        census = [(os.getpid(), 99001, f"python gate.py recover-task --task {TASK}"),
                  parent, grandparent, child, (1, 0, "init")]
        self.assertEqual(recovery.repo_writers(env.repo, TASK, census), [])

    def test_a_census_that_cannot_run_is_a_refusal(self) -> None:
        env = build(self.root)
        for failure in (OSError("no powershell"),
                        subprocess.TimeoutExpired(cmd="census", timeout=60)):
            with self.subTest(type(failure).__name__):
                with mock.patch.object(recovery.subprocess, "run", side_effect=failure):
                    with self.assertRaisesRegex(recovery.RecoveryError, "census"):
                        recovery.process_census()
        with mock.patch.object(recovery.subprocess, "run",
                               return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
            with self.assertRaisesRegex(recovery.RecoveryError, "returned nothing"):
                recovery.process_census()
        with mock.patch.object(
            recovery.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout="garbage\n", stderr="")
        ):
            with self.assertRaisesRegex(recovery.RecoveryError, "unreadable census row"):
                recovery.process_census()

    def test_the_real_census_lists_this_process(self) -> None:
        census = recovery.process_census()
        self.assertIn(os.getpid(), [entry[0] for entry in census])
        self.assertGreater(len(census), 1)

    def test_a_legacy_run_needs_a_named_stopped_owner(self) -> None:
        env = build(self.root, journal=stopped_journal(pid=None))
        with self.assertRaisesRegex(recovery.RecoveryError, "--stopped-pid"):
            self.recover(env)
        write(env.repo / ".gate/supervisor.log", supervisor_log(env))
        self.recover(env, stopped_pid=DEAD_PID)
        self.assertEqual(
            self.gate.parse_queue(env.queue.read_text(encoding="utf-8"))[TASK]["status"], "TODO")

    def test_the_named_owner_must_be_gone_and_must_match(self) -> None:
        env = build(self.root)
        with self.assertRaisesRegex(recovery.RecoveryError, "not the pid the run recorded"):
            self.recover(env, stopped_pid=DEAD_PID + 100000)
        with self.assertRaisesRegex(recovery.RecoveryError, "still in the process census"):
            self.recover(env, extra_census=[(DEAD_PID, 1, "the runner, still going")])

        alive = build(self.root / "alive", journal=stopped_journal(pid=os.getpid()))
        with self.assertRaisesRegex(recovery.RecoveryError, "lineage|alive"):
            self.recover(alive)

    def test_process_checks_never_signal_and_fail_closed(self) -> None:
        self.assertTrue(recovery.process_alive(os.getpid()))
        self.assertFalse(recovery.process_alive(DEAD_PID))
        for bad in (0, -1, None, True):
            self.assertTrue(recovery.process_alive(bad))
        with mock.patch.object(recovery.os, "name", "posix"), \
                mock.patch.object(recovery.os, "kill", side_effect=PermissionError) as kill:
            self.assertTrue(recovery.process_alive(4242))
            kill.assert_called_once_with(4242, 0)


class F3AcceptanceInputTests(RecoveryTestCase):
    """Finding 3: the oracle's inputs were sampled only after it finished."""

    def oracle(self, env, during=None, after=None):
        """An oracle that can mutate the tree while it 'runs'."""
        real = subprocess.run

        def fake(*args, **kwargs):
            if kwargs.get("shell"):
                if during:
                    during()
                return SimpleNamespace(returncode=0, stdout="2 passed\n", stderr="")
            return real(*args, **kwargs)

        return mock.patch.object(self.gate.subprocess, "run", side_effect=fake)

    def test_an_edit_reverted_before_the_oracle_returns_is_caught(self) -> None:
        env = build(self.root)
        self.recover(env)
        self.commit_row(env)
        # The tree the oracle started on is not the tree it ended on.
        write(env.proj / SOURCE, "def feature():\n    return 3\n")
        with self.oracle(env, during=lambda: write(env.proj / SOURCE, IMPLEMENTATION)):
            self.verify(env)
        self.stage_done(env)
        detail = self.blocked(env)
        self.assertIn("INPUTS_CHANGED", detail)
        verdict = self.gate.read_verdict(env.repo)
        self.assertEqual(verdict["result"], "INPUTS_CHANGED")
        self.assertIn(SOURCE, verdict["inputs_drift"], "the verdict must name the file that moved")

    def test_a_file_named_like_a_gate_artifact_is_still_bound(self) -> None:
        env = build(self.root)
        self.recovered_and_verified(env)
        write(env.proj / "src" / "runtime.gate.py", "SECRET = 1\n")
        detail = self.blocked(env)
        self.assertIn("changed after the acceptance run", detail)
        self.assertIn("src/runtime.gate.py", detail)

    def test_runner_artifacts_do_not_count_as_tree_changes(self) -> None:
        env = build(self.root)
        self.recovered_and_verified(env)
        (env.repo / ".gate" / "runs" / "later").mkdir(parents=True, exist_ok=True)
        (env.repo / ".gate" / "runs" / "later" / "x.log").write_text("noise", encoding="utf-8")
        (env.repo / ".gate" / "RUN-REPORT-later.md").write_text("noise", encoding="utf-8")
        self.assertIn("recovered claim", self.check_commit(env))

    def test_config_and_untracked_test_changes_block_a_completion(self) -> None:
        env = build(self.root)
        self.recovered_and_verified(env)
        write(env.proj / "tests" / "test_extra.py", "def test_x():\n    assert True\n")
        self.assertIn("tests/test_extra.py", self.blocked(env))
        (env.proj / "tests" / "test_extra.py").unlink()
        self.assertIn("recovered claim", self.check_commit(env))

        config_file = env.repo / ".gate" / "config.json"
        config_file.write_text(
            json.dumps({**json.loads(config_file.read_text(encoding="utf-8")),
                        "verdict_max_age_s": 99999}), encoding="utf-8")
        self.assertIn("verification configuration changed", self.blocked(env))


class F4RequirementBindingTests(RecoveryTestCase):
    """Finding 4: only the id and the file list were bound."""

    def commit_all(self, env, message: str) -> None:
        git(env, "add", "-A")
        git(env, "commit", "-q", "-m", message)

    def test_a_changed_task_description_is_refused(self) -> None:
        env = build(self.root)
        text = env.queue.read_text(encoding="utf-8")
        env.queue.write_text(text.replace("| feature returns two |", "| feature returns three |"),
                             encoding="utf-8", newline="")
        self.commit_all(env, "restate the task")
        with self.assertRaisesRegex(recovery.RecoveryError, "requirement of PRA-06 changed"):
            self.recover(env)

    def test_a_changed_acceptance_section_is_refused(self) -> None:
        env = build(self.root)
        write(env.proj / PLAN, plan_text(acceptance="`feature()` now returns 3."))
        self.commit_all(env, "restate acceptance")
        with self.assertRaisesRegex(recovery.RecoveryError, "requirement of PRA-06 changed"):
            self.recover(env)

    def test_unrelated_sections_and_a_new_dependency_are_bookkeeping(self) -> None:
        env = build(self.root)
        # Exactly the shape of the real queue: another section rewritten, a new
        # dependency row added, and a dependency note for this task.
        write(env.proj / PLAN, plan_text(other="Rewritten entirely; nothing to do with PRA-06."))
        env.queue.write_text(
            queue_text(extra_rows="| 3 | PRA-00R | dependency | `TODO` | 2 passed | 2 passed |\n",
                       extra_notes=f"<!-- task:{TASK} after: PRA-00R -->\n"
                                   "<!-- task:PRA-00R files: src/dep.py -->\n"),
            encoding="utf-8", newline="")
        self.commit_all(env, "queue dependency and unrelated spec edits")
        receipt = self.recover(env)
        self.assertEqual(receipt["requirement"]["cells"], [TASK, "feature returns two"])
        self.assertEqual(receipt["scope"]["after"], ["PRA-00R"])

    def test_an_id_that_appears_nowhere_in_acceptance_is_refused(self) -> None:
        env = build(self.root)
        write(env.proj / PLAN, "# Work package PRA\n\nNo task headings at all.\n")
        self.commit_all(env, "drop the acceptance headings")
        with self.assertRaisesRegex(recovery.RecoveryError, "no acceptance text"):
            self.recover(env)

    def test_block_mode_and_an_explicit_document_are_opt_in(self) -> None:
        env = build(self.root, acceptance_note="docs/acceptance.md")
        with self.assertRaisesRegex(recovery.RecoveryError, "no acceptance text"):
            self.recover(env, requirement_path="docs/acceptance.md")
        receipt = self.recover(env, requirement_path="docs/acceptance.md",
                               requirement_mode="blocks")
        self.assertEqual(receipt["requirement"]["path"], "docs/acceptance.md")
        self.assertEqual(receipt["requirement"]["sources"]["docs/acceptance.md"]["parts"], 1)


class F5ReceiptIntegrityTests(RecoveryTestCase):
    """Finding 5: recomputing the digest made a tampered receipt valid again."""

    def retamper(self, env, receipt, mutate) -> None:
        path = recovery.receipt_path(env.repo, receipt["sha256"])
        body = json.loads(path.read_text(encoding="utf-8"))
        mutate(body)
        body["sha256"] = recovery.receipt_digest(body)      # the reviewer's move
        path.write_text(json.dumps(body, indent=2), encoding="utf-8")

    def test_a_recomputed_digest_does_not_rescue_a_tampered_receipt(self) -> None:
        env = build(self.root)
        receipt = self.recovered_and_verified(env)

        def swap(body):
            body["implementation"]["commit"] = env.unrelated
            body["stopped_run"]["reason"] = "invented"

        self.retamper(env, receipt, swap)
        detail = self.blocked(env)
        self.assertIn("no valid recovery receipt", detail)
        self.assertRegex(detail, "stored under its own digest|disagrees with the receipt")

    def test_the_filename_the_index_and_the_digest_must_agree(self) -> None:
        env = build(self.root)
        receipt = self.recovered_and_verified(env)
        self.assertIn("recovered claim", self.check_commit(env))

        # Same bytes, a different name, and an index pointing at the new name.
        path = recovery.receipt_path(env.repo, receipt["sha256"])
        body = json.loads(path.read_text(encoding="utf-8"))
        alias = "a" * 64
        recovery.receipt_path(env.repo, alias).write_text(json.dumps(body), encoding="utf-8")
        index = recovery.index_path(env.repo)
        index.write_text(index.read_text(encoding="utf-8").replace(receipt["sha256"], alias),
                         encoding="utf-8")
        self.assertIn("no valid recovery receipt", self.blocked(env))

    def test_an_index_entry_that_disagrees_with_its_receipt_is_refused(self) -> None:
        env = build(self.root)
        receipt = self.recovered_and_verified(env)
        index = recovery.index_path(env.repo)
        entry = json.loads(index.read_text(encoding="utf-8").splitlines()[0])
        entry["implementation"] = env.unrelated
        index.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        self.assertIn("disagrees with the receipt", self.blocked(env))

    def test_evidence_is_re_derived_not_trusted(self) -> None:
        env = build(self.root)
        self.recovered_and_verified(env)
        self.assertIn("recovered claim", self.check_commit(env))
        # The journal is the receipt's other half; rewriting it invalidates it.
        write_journal(env.repo, stopped_journal(outcome="no_progress"))
        self.assertIn("no valid recovery receipt", self.blocked(env))


class F6OracleLifecycleTests(RecoveryTestCase):
    """Finding 6: a timeout left the previous PASS standing."""

    def failing_oracle(self, failure):
        real = subprocess.run

        def fake(*args, **kwargs):
            if kwargs.get("shell"):
                raise failure
            return real(*args, **kwargs)

        return mock.patch.object(self.gate.subprocess, "run", side_effect=fake)

    def test_a_timeout_invalidates_the_previous_pass(self) -> None:
        env = build(self.root)
        self.recovered_and_verified(env)
        self.assertIn("recovered claim", self.check_commit(env))

        with self.failing_oracle(subprocess.TimeoutExpired(cmd=VERIFY_CMD, timeout=1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.verify(env)
        self.assertEqual(self.gate.read_verdict(env.repo)["result"], "TIMEOUT")
        detail = self.blocked(env)
        self.assertIn("not PASS", detail)
        self.assertIn("did not finish", detail)

    def test_an_interrupted_oracle_invalidates_the_previous_pass(self) -> None:
        env = build(self.root)
        self.recovered_and_verified(env)
        with self.failing_oracle(KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.verify(env)
        self.assertEqual(self.gate.read_verdict(env.repo)["result"], "INTERRUPTED")
        self.assertIn("not PASS", self.blocked(env))

    def test_a_verdict_is_running_while_the_oracle_runs(self) -> None:
        env = build(self.root)
        self.recovered_and_verified(env)
        seen = {}
        real = subprocess.run

        def fake(*args, **kwargs):
            if kwargs.get("shell"):
                seen.update(self.gate.read_verdict(env.repo) or {})
            return real(*args, **kwargs)

        with mock.patch.object(self.gate.subprocess, "run", side_effect=fake):
            self.verify(env)
        self.assertEqual(seen.get("result"), "RUNNING")
        self.assertIsNotNone(seen.get("inputs_before"))


class F7TransactionTests(RecoveryTestCase):
    """Finding 7: an interrupted write truncated the whole queue."""

    def test_a_failed_queue_replace_leaves_everything_intact(self) -> None:
        env = build(self.root)
        before = env.queue.read_bytes()
        real_replace = os.replace

        def fail_on_queue(src, dst, *args, **kwargs):
            if str(dst).endswith("TASK_QUEUE.md"):
                raise OSError(28, "No space left on device")
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch.object(recovery.os, "replace", side_effect=fail_on_queue):
            with self.assertRaises(OSError):
                self.recover(env)

        self.assertEqual(env.queue.read_bytes(), before)
        self.assertFalse(recovery.index_path(env.repo).exists())
        self.assertFalse(any(recovery.recovery_dir(env.repo).glob("*.tmp")))
        # The lock went back, so the next attempt is not blocked by the failure.
        self.assertTrue(recovery.engine_lock_free(env.repo))
        self.recover(env)
        self.assertEqual(
            self.gate.parse_queue(env.queue.read_text(encoding="utf-8"))[TASK]["status"], "TODO")

    def test_a_receipt_without_an_index_entry_grants_nothing(self) -> None:
        env = build(self.root)
        receipt = self.recovered_and_verified(env)
        index = recovery.index_path(env.repo)
        index.write_text("", encoding="utf-8")
        self.assertIn("no valid recovery receipt", self.blocked(env))
        self.assertTrue(recovery.receipt_path(env.repo, receipt["sha256"]).exists())


class F8ReviewBarrierTests(RecoveredRunTests):
    """Finding 8: an unreviewed recovered task only stopped one run."""

    def test_the_barrier_survives_a_restart(self) -> None:
        env = build(self.root, judge={"enabled": False})
        self.recover(env)
        self.commit_row(env)
        code, _ = self.run_gate(env, [])
        self.assertEqual(code, 3)
        self.assertIn(TASK, recovery.open_barriers(env.repo))

        # A second run must not simply move on to the next task.
        code, report = self.run_gate(env, [], max_tasks=2)
        self.assertEqual(code, 3)
        self.assertIn("closed but unreviewed", report)
        dispatched = [e["task"] for e in recovery.journal_events(env.repo)
                      if e["event"] == "attempt_start"]
        self.assertNotIn(OTHER, dispatched)

    def test_a_crash_before_review_still_leaves_the_barrier(self) -> None:
        env = build(self.root, judge=self.JUDGE_ON)
        receipt = self.recover(env)
        self.commit_row(env)

        def crashing_spawn(repo_arg, cfg, tool, prompt, prompt_file, **kwargs):
            raise KeyboardInterrupt("the host died mid-task")

        with mock.patch.object(self.gate, "resolve_tool", side_effect=lambda name: name), \
                mock.patch.object(self.gate, "spawn_worker", side_effect=crashing_spawn), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                self.gate.cmd_run(self.args(env))
        self.assertEqual(recovery.open_barriers(env.repo)[TASK]["receipt"], receipt["sha256"])

    def test_review_task_requires_independent_judge(self) -> None:
        env = build(self.root, judge={"enabled": False})
        self.recover(env)
        self.commit_row(env)
        self.run_gate(env, [])
        self.assertIn(TASK, recovery.open_barriers(env.repo))

        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                self.gate.cmd_review_task(
                    SimpleNamespace(repo=str(env.repo), task=TASK, accept=None))
        self.assertIn("judge.enabled is false", err.getvalue())
        self.assertIn(TASK, recovery.open_barriers(env.repo))

        with self.assertRaises(recovery.RecoveryError):
            recovery.clear_barrier(env.repo, TASK, "human", "operator")
        self.assertIn(TASK, recovery.open_barriers(env.repo))

    def test_a_judge_pass_clears_the_barrier(self) -> None:
        env = build(self.root, judge=self.JUDGE_ON)
        self.recover(env)
        self.commit_row(env)
        code, report = self.run_gate(env, [])
        self.assertEqual(code, 0, report)
        self.assertEqual(recovery.open_barriers(env.repo), {})


class F9AuditEvidenceTests(RecoveryTestCase):
    """Finding 9: a recovery receipt alone made a queue-only DONE legitimate."""

    def audit(self, env):
        stream = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(stream):
            try:
                self.gate.cmd_audit(SimpleNamespace(repo=str(env.repo), last=10))
            except SystemExit as exc:
                code = exc.code
        return code, stream.getvalue()

    def test_a_recovery_without_verification_is_still_a_fake_completion(self) -> None:
        env = build(self.root)
        self.recover(env)
        self.commit_row(env)
        # No verify, no hook: straight to a queue-only DONE.
        self.stage_done(env)
        git(env, "commit", "-q", "-m", f"{TASK} done (it is not)")
        code, report = self.audit(env)
        self.assertEqual(code, 1)
        self.assertIn("FAKE_COMPLETION", report)
        self.assertFalse(recovery.completions_path(env.repo).exists())

    def test_a_verified_and_reviewed_closure_is_recognised(self) -> None:
        env = build(self.root)
        receipt = self.close_recovered(env)
        recovery.raise_barrier(env.repo, TASK, receipt["sha256"], "pending review")
        code, report = self.audit(env)
        self.assertEqual(code, 1)
        self.assertIn("REVIEW PENDING", report)

        self.gate.journal(env.repo, {"event": "judge_verdict", "task": TASK,
            "commit": git(env, "rev-parse", "HEAD"), "receipt": receipt["sha256"],
            "passed": True, "score": 92, "verdict": "pass", "member": "mock-independent"})
        recovery.clear_barrier(env.repo, TASK, "judge", "score 92")
        code, report = self.audit(env)
        self.assertEqual(code, 0, report)
        self.assertIn("RECOVERED", report)
        self.assertIn(receipt["sha256"][:10], report)

    def test_a_completion_record_does_not_travel_to_another_commit(self) -> None:
        env = build(self.root)
        self.close_recovered(env)
        code, report = self.audit(env)
        self.assertEqual(code, 1, report)
        self.assertIn("REVIEW PENDING", report)
        self.assertNotIn("FAKE_COMPLETION", report)

        # Same receipt, a second queue-only DONE elsewhere in history.
        text = env.queue.read_text(encoding="utf-8")
        row = recovery.queue_row(text, OTHER)
        env.queue.write_text(text.replace(row, recovery.flipped_row(row, "TODO", "DONE")),
                             encoding="utf-8", newline="")
        git(env, "add", env.queue_rel)
        git(env, "commit", "-q", "-m", f"{OTHER} done too")
        code, report = self.audit(env)
        self.assertEqual(code, 1)
        self.assertIn("FAKE_COMPLETION", report)


class F10BudgetTests(RecoveredRunTests):
    """Finding 10: recovery handed back a fresh attempt budget."""

    def test_prior_attempts_are_kept_and_one_revalidation_is_granted(self) -> None:
        env = build(self.root, journal=stopped_journal(attempts=2), max_attempts_per_task=2)
        receipt = self.recover(env)
        budget = recovery.revalidation_budget(env.repo, self.cfg(env), TASK, receipt)
        self.assertEqual((budget["prior_attempts"], budget["allowed"], budget["used"]), (2, 1, 0))
        self.commit_row(env)

        prompts: list[str] = []
        code, report = self.run_gate(env, prompts, close=False)
        self.assertEqual(code, 3)
        self.assertEqual(len(prompts), 1, "exactly one revalidation dispatch")
        self.assertIn("2 prior attempt(s) kept", report)
        dispatches = [e for e in recovery.journal_events(env.repo)
                      if e["event"] == "revalidation_dispatch"]
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(dispatches[0]["prior_attempts"], 2)

    def test_a_second_run_does_not_renew_the_revalidation(self) -> None:
        env = build(self.root, max_attempts_per_task=2)
        self.recover(env)
        self.commit_row(env)
        first, _ = self.run_gate(env, [], close=False)
        self.assertEqual(first, 3)

        prompts: list[str] = []
        code, report = self.run_gate(env, prompts, close=False)
        self.assertEqual(code, 3)
        self.assertEqual(prompts, [], "no second revalidation dispatch")
        self.assertIn(f"revalidation_exhausted:{TASK}", report)

    def test_no_progress_and_other_outcomes_are_not_recoverable(self) -> None:
        for outcome in ("no_progress", "timeout", "worker_incomplete", "blocked"):
            with self.subTest(outcome):
                env = build(self.root / outcome,
                            journal=stopped_journal(outcome=outcome, stop=f"{outcome}:{TASK}"))
                with self.assertRaisesRegex(recovery.RecoveryError, "never re-opens the budget"):
                    self.recover(env, reason=f"{outcome}:{TASK}")

    def test_the_prior_failure_signature_carries_forward(self) -> None:
        env = build(self.root, max_attempts_per_task=3)
        receipt = self.recover(env)
        budget = recovery.revalidation_budget(env.repo, self.cfg(env), TASK, receipt)
        self.assertEqual(budget["prior_signature"], "0:worker stopped without closing")
        self.assertEqual(budget["prior_outcome"], "not_done")



def supervisor_log(env, pid=DEAD_PID, closed=True):
    return (f"2026-09-11T12:56:07  supervisor start: repo={env.repo.as_posix()} max_tasks=12 max_restarts=3 pid={pid}\n"
            "2026-09-11T12:56:07  launching runner (attempt 1); DONE before = 84\n"
            f"GATE run {RUN_ID}\n"
            + (f"2026-09-11T13:32:26  runner exited code=3 DONE=84->85 fresh_report=True stop={REASON}\n"
               "2026-09-11T13:32:26  supervisor exit\n" if closed else ""))


class Review2RegressionTests(RecoveredRunTests):
    """Second-review attacks plus supported positive dependency/transaction paths."""

    def commit_all(self, env, message):
        git(env, "add", "-A")
        git(env, "commit", "-qm", message)

    def test_unrelated_dead_pid_is_not_legacy_ownership(self):
        env = build(self.root, journal=stopped_journal(pid=None))
        with self.assertRaisesRegex(recovery.RecoveryError, "supervisor.log"):
            self.plan(env, stopped_pid=DEAD_PID)
        write(env.repo / ".gate/supervisor.log", supervisor_log(env, pid=DEAD_PID + 1))
        with self.assertRaisesRegex(recovery.RecoveryError, "original supervisor"):
            self.plan(env, stopped_pid=DEAD_PID)

    def test_legacy_exit_and_immutable_session_are_required(self):
        env = build(self.root, journal=stopped_journal(pid=None))
        path = env.repo / ".gate/supervisor.log"
        write(path, supervisor_log(env, closed=False))
        with self.assertRaisesRegex(recovery.RecoveryError, "exit"):
            self.plan(env, stopped_pid=DEAD_PID)
        write(path, supervisor_log(env))
        receipt = self.recover(env, stopped_pid=DEAD_PID)
        write(path, supervisor_log(env) + "later unrelated log append\n")
        self.assertIsNotNone(recovery.receipt_for(env.repo, self.cfg(env), TASK))
        write(path, supervisor_log(env).replace("12:56:07", "12:56:08"))
        self.assertIsNone(recovery.receipt_for(env.repo, self.cfg(env), TASK))

    def test_nested_acceptance_is_bound(self):
        env = build(self.root)
        text = plan_text().replace("Acceptance:", "### Acceptance", 1)
        write(env.proj / PLAN, text); self.commit_all(env, "nested requirements")
        write(env.proj / SOURCE, IMPLEMENTATION + "# actual implementation\n")
        self.commit_all(env, "implementation"); env.implementation = git(env, "rev-parse", "HEAD")
        write(env.proj / PLAN, text.replace("returns 2", "returns 999"))
        self.commit_all(env, "drift")
        with self.assertRaisesRegex(recovery.RecoveryError, "requirement"):
            self.plan(env)

    def test_staged_and_unstaged_acceptance_cannot_drift(self):
        env = build(self.root); self.recovered_and_verified(env)
        write(env.proj / PLAN, plan_text(acceptance="returns 999"))
        self.assertFalse(recovery.done_allowance(env.repo, self.cfg(env), [TASK], [env.queue_rel])["allowed"])
        git(env, "add", PLAN)
        self.assertFalse(recovery.done_allowance(env.repo, self.cfg(env), [TASK], [env.queue_rel, PLAN])["allowed"])
        self.assertIn("requirement", self.blocked(env))

    def test_non_ascii_and_quoted_paths_are_bound(self):
        env = build(self.root)
        path = env.proj / "src" / "验收 space.py"
        write(path, "value=1\n")
        self.recovered_and_verified(env)
        before = recovery.acceptance_inputs(env.repo, self.cfg(env))
        write(path, "value=999\n")
        self.assertIn("src/验收 space.py", recovery.inputs_differences(before, recovery.acceptance_inputs(env.repo, self.cfg(env))))
        self.assertFalse(recovery.done_allowance(env.repo, self.cfg(env), [TASK], [env.queue_rel])["allowed"])

    def test_ignored_input_contract_binds_actual_oracle(self):
        command = f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'runtime/settings.txt\').read_text().strip() == \'1\'; print(\'2 passed\')"'
        env = build(self.root, verify_cmd=command, recovery_inputs={"include": ["runtime/settings.txt"]})
        write(env.repo / ".gitignore", "runtime/\n.gate/\n"); self.commit_all(env, "ignore runtime")
        path = env.repo / "runtime/settings.txt"; write(path, "1\n")
        self.recovered_and_verified(env); write(path, "999\n")
        self.assertFalse(recovery.done_allowance(env.repo, self.cfg(env), [TASK], [env.queue_rel])["allowed"])
        self.assertEqual(self.verify(env)["result"], "FAIL")

    def test_unknown_ignored_data_is_reported_never_read_and_refused_at_gate(self):
        env = build(self.root)
        write(env.repo / ".gitignore", "private/\n.gate/\n"); self.commit_all(env, "ignore data")
        path = env.repo / "private/production.sqlite"; write(path, "synthetic never production")
        original = Path.read_bytes

        def read(p):
            self.assertNotEqual(p, path, "must not read unclassified data")
            return original(p)

        with mock.patch.object(Path, "read_bytes", read):
            inputs = recovery.acceptance_inputs(env.repo, self.cfg(env))
        # Capture reports it and moves on: an ordinary task's verify still runs.
        self.assertEqual(inputs["unclassified"], ["private/"])
        self.assertNotIn("private/production.sqlite", inputs["worktree"])
        # A recovered completion does not: every ignored input is bound or excluded.
        self.recovered_and_verified(env)
        detail = self.blocked(env)
        self.assertIn("neither bound nor excluded: private/", detail)

    def test_ignored_directories_classify_as_one_entry(self):
        env = build(self.root, recovery_inputs={"include": ["vendor/"], "exclude": ["deps/"]})
        write(env.repo / ".gitignore", "deps/\nvendor/\n.gate/\n"); self.commit_all(env, "ignore trees")
        for i in range(300):
            write(env.repo / "deps" / f"pkg{i}" / "index.js", f"module.exports = {i};\n")
        write(env.repo / "vendor" / "lib" / "a.txt", "a\n")
        write(env.repo / "vendor" / "b.txt", "b\n")
        paths, unclassified = recovery.classify_inputs(env.repo, self.cfg(env))
        self.assertEqual(unclassified, [])
        self.assertFalse(any(p.startswith("deps/") for p in paths), "an excluded tree is one decision")
        self.assertIn("vendor/lib/a.txt", paths)
        self.assertIn("vendor/b.txt", paths)
        self.assertEqual([e for e in recovery.ignored_entries(env.repo)
                          if not recovery.is_gate_artifact(e)], ["deps/", "vendor/"])

    def test_write_restore_inside_oracle_and_restored_mtime_is_rejected(self):
        env = build(self.root); self.recover(env); self.commit_row(env)
        path = env.proj / SOURCE; st = path.stat(); real = subprocess.run
        def oracle(*a, **kw):
            if kw.get("shell"):
                write(path, "return 999\n"); write(path, IMPLEMENTATION)
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
                return SimpleNamespace(returncode=0, stdout="2 passed\n", stderr="")
            return real(*a, **kw)
        with mock.patch.object(self.gate.subprocess, "run", side_effect=oracle):
            verdict = self.verify(env)
        self.assertEqual(verdict["result"], "INPUTS_CHANGED")
        self.assertIn(SOURCE, verdict["inputs_drift"])

    def test_precapture_failure_invalidates_old_pass(self):
        env = build(self.root); self.recovered_and_verified(env)
        with mock.patch.object(recovery, "acceptance_inputs", side_effect=OSError("snapshot failed")):
            with self.assertRaises(OSError): self.verify(env)
        self.assertEqual(self.gate.read_verdict(env.repo)["result"], "RUNNING")
        self.assertFalse(recovery.done_allowance(env.repo, self.cfg(env), [TASK], [env.queue_rel])["allowed"])

    def test_index_failure_blocks_dispatch_and_original_command_resumes(self):
        env = build(self.root); original = recovery._append
        def append(path, payload):
            if path == recovery.index_path(env.repo): raise OSError("index disk failure")
            return original(path, payload)
        with mock.patch.object(recovery, "_append", side_effect=append):
            with self.assertRaises(OSError): self.recover(env)
        self.assertIn(TASK, recovery.pending_transactions(env.repo))
        prompts = []; code, report = self.run_gate(env, prompts, close=False)
        self.assertEqual(prompts, []); self.assertIn("incomplete recovery", report)
        receipt = self.recover(env)
        self.assertFalse(recovery.pending_transactions(env.repo))
        self.assertEqual(recovery.receipt_for(env.repo, self.cfg(env), TASK)["sha256"], receipt["sha256"])

    def test_hook_outside_runner_owes_review_and_audit_requires_affirmative_pass(self):
        env = build(self.root); self.close_recovered(env)
        classification = recovery.audit_recovered(env.repo, self.cfg(env), git(env, "rev-parse", "HEAD"), [TASK])
        self.assertFalse(classification[1]); self.assertIn(TASK, recovery.open_barriers(env.repo))
        prompts = []; self.run_gate(env, prompts, close=False); self.assertEqual(prompts, [])
        with self.assertRaisesRegex(recovery.RecoveryError, "affirmative"):
            recovery.clear_barrier(env.repo, TASK, "judge", "invented")

    def test_done_plus_tool_error_retains_obligation(self):
        env = build(self.root); self.recover(env); self.commit_row(env)
        ordinary = self.worker(env, [])
        def spawn(*a, **kw):
            ordinary(*a, **kw)
            return self.gate.WorkerResult(1, "429 rate limit exceeded"), None
        with mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n:n), mock.patch.object(self.gate, "spawn_worker", side_effect=spawn), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit): self.gate.cmd_run(self.args(env))
        self.assertIn(TASK, recovery.open_barriers(env.repo))
        prompts=[]; self.run_gate(env, prompts, close=False); self.assertEqual(prompts, [])

    def test_receipt_renewal_cannot_renew_lineage_allowance(self):
        env = build(self.root); receipt = self.recover(env); self.commit_row(env)
        recovery.record_revalidation_dispatch(env.repo, TASK, receipt, recovery.revalidation_budget(env.repo, self.cfg(env), TASK, receipt))
        write(env.queue, env.queue.read_text().replace("`TODO`", "`IN_PROGRESS`", 1)); self.commit_row(env)
        run_id = "20260911-080000"
        for event in stopped_journal(run_id=run_id): self.gate.journal(env.repo, event)
        with self.assertRaisesRegex(recovery.RecoveryError, "allowance exhausted"):
            self.recover(env, run_id=run_id)

    def test_outage_rotation_cannot_invoke_second_worker(self):
        env = build(self.root, crlf=True, workers=["claude", "codex"], execution={"defaults":{"workers":{
            "claude":{"model":"claude-test", "reasoning_effort":"medium"},
            "codex":{"model":"codex-test", "reasoning_effort":"medium"}}}})
        receipt=self.recover(env); self.commit_row(env); calls=[]
        def spawn(*a, **kw):
            calls.append(a[2]); return self.gate.WorkerResult(1, "429 rate limit exceeded"), None
        with mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n:n), mock.patch.object(self.gate, "spawn_worker", side_effect=spawn), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit): self.gate.cmd_run(self.args(env))
        self.assertEqual(len(calls), 1)
        self.assertEqual(recovery.revalidation_budget(env.repo, self.cfg(env), TASK, receipt)["used"], 1)

    def test_lock_refusal_does_not_write_run_or_review_state(self):
        env=build(self.root); before=recovery.journal_events(env.repo)
        with recovery.EngineLock(env.repo, "holder"):
            code, _ = self.run_gate(env, [], close=False); self.assertEqual(code, 3)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.gate.cmd_review_task(SimpleNamespace(repo=str(env.repo), task=TASK))
        self.assertEqual(recovery.journal_events(env.repo), before)
        self.plan(env)

    def test_repeated_review_preserves_revision_cap(self):
        env=build(self.root, judge={**self.JUDGE_ON, "max_revisions":1})
        self.recover(env); self.commit_row(env)
        ordinary=self.worker(env, [])
        def spawn(*a, **kw):
            if "-rev" in a[4].name: return self.gate.WorkerResult(0,"no change"),None
            return ordinary(*a, **kw)
        revise={"score":20,"verdict":"revise","findings":[],"revision_brief":"fix"}
        with mock.patch.object(self.gate,"resolve_tool",side_effect=lambda n:n), mock.patch.object(self.gate,"spawn_worker",side_effect=spawn), mock.patch.object(self.gate,"judge_once",return_value=(revise,"codex")), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit): self.gate.cmd_run(self.args(env))
            for _ in range(2):
                with self.assertRaises(SystemExit): self.gate.cmd_review_task(SimpleNamespace(repo=str(env.repo),task=TASK))
        events=recovery.journal_events(env.repo)
        self.assertEqual([e["revision"] for e in events if e["event"]=="revision_start"], [1])
        self.assertIn(TASK,recovery.open_barriers(env.repo))


ALLTOM_HEADER = "| # | ID | task | status | worker | passed-before | passed-after |"


def alltom_row(status: str, before: str, after: str) -> str:
    return (f"{ALLTOM_HEADER}\n|---|---|---|---|---|---|---|\n"
            f"| 72 | {TASK} | Correct annual-bonus boundary taxes with official oracle "
            f"(spec {TASK}) | `{status}` | claude | {before} | {after} |\n")


class ClosureRefusalRegressionTests(RecoveredRunTests):
    """What the first real run produced: a passing oracle whose closure the
    gate refused for engine reasons, and a dispatch that ignored the pin."""

    def test_baseline_placeholders_are_bookkeeping_not_requirement(self) -> None:
        cfg = {**self.gate.DEFAULT_CONFIG, **config()}
        placeholder = recovery.requirement_cells(
            cfg, alltom_row("IN_PROGRESS", "measured at dispatch", "pending"), TASK)
        filled = recovery.requirement_cells(
            cfg, alltom_row("DONE", "12563 passed (prior full receipt)", "12563 passed"), TASK)
        self.assertEqual(placeholder, filled)
        self.assertEqual(placeholder, [TASK, f"Correct annual-bonus boundary taxes with "
                                              f"official oracle (spec {TASK})"])
        # The task text itself is still requirement.
        renamed = recovery.requirement_cells(
            cfg, alltom_row("DONE", "12563 passed", "12563 passed").replace(
                "boundary taxes", "boundary rates"), TASK)
        self.assertNotEqual(placeholder, renamed)
        # Without a header the content heuristics still drop counts and dashes.
        headerless = (f"| 72 | {TASK} | feature returns two | `TODO` | 1 passed | — |\n")
        self.assertEqual(recovery.requirement_cells(cfg, headerless, TASK),
                         [TASK, "feature returns two"])

    def test_revalidation_dispatch_goes_to_the_preferred_worker(self) -> None:
        defaults = config()["execution"]["defaults"]
        defaults["workers"]["codex"] = {"model": "codex-test-model", "reasoning_effort": "medium"}
        env = build(self.root, journal=stopped_journal(attempts=3), max_attempts_per_task=3,
                    workers=["claude", "codex"], execution={"defaults": defaults},
                    judge=self.JUDGE_ON)
        self.recover(env)
        self.commit_row(env)
        tools: list[str] = []
        code, report = self.run_gate(env, [], tools=tools)
        self.assertEqual(code, 0, report)
        self.assertEqual(tools[:1], ["claude"], "three carried attempts must not rotate the pin away")

    def test_a_refused_passing_closure_can_be_renewed_exactly_once(self) -> None:
        env = build(self.root, judge=self.JUDGE_ON)
        self.recover(env)
        self.commit_row(env)
        code, _ = self.run_gate(env, [], verify_only=True)
        self.assertEqual(code, 3)
        # Nothing closed, so the dispatch barrier is retired and a later run
        # can start; a DONE row would still be reconstructed as an obligation.
        self.assertEqual(recovery.open_barriers(env.repo), {})
        cfg = self.cfg(env)
        with self.assertRaisesRegex(recovery.RecoveryError, "needs --reason"):
            recovery.renew_revalidation(env.repo, cfg, TASK, "")
        grant = recovery.renew_revalidation(env.repo, cfg, TASK, "gate refused a passing closure")
        self.assertEqual(grant["event"], "revalidation_renewed")
        with self.assertRaisesRegex(recovery.RecoveryError, "granted once"):
            recovery.renew_revalidation(env.repo, cfg, TASK, "again")

        # The claim left by that dispatch is recoverable again, from its run.
        run_id = [e["run"] for e in recovery.journal_events(env.repo) if e["event"] == "run_end"][-1]
        with mock.patch.object(recovery, "ownership_evidence", return_value={
            "owner_pid": DEAD_PID, "owner_source": "journal", "supervisor": None,
            "census_size": 3, "census_at": "test", "matched_writers": []}):
            receipt = self.recover(env, run_id=run_id)
        budget = recovery.revalidation_budget(env.repo, cfg, TASK, receipt)
        self.assertEqual((budget["allowed"], budget["used"]), (2, 1))
        self.commit_row(env)
        prompts: list[str] = []
        code, report = self.run_gate(env, prompts)
        self.assertEqual(code, 0, report)
        self.assertEqual(len([p for p in prompts if "acceptance judge" not in p]), 1)
        self.assertEqual(
            self.gate.parse_queue(env.queue.read_text(encoding="utf-8"))[TASK]["status"], "DONE")

    def test_renewal_survives_a_receipt_invalidated_by_a_contract_change(self) -> None:
        """The exact shape a fixed defect produces: the receipt no longer
        re-derives, but the journal still proves the passing dispatch."""
        env = build(self.root, judge=self.JUDGE_ON)
        self.recover(env)
        self.commit_row(env)
        code, _ = self.run_gate(env, [], verify_only=True)
        self.assertEqual(code, 3)
        config_file = env.repo / ".gate" / "config.json"
        config_file.write_text(json.dumps({**json.loads(config_file.read_text(encoding="utf-8")),
                                           "verdict_max_age_s": 7200}), encoding="utf-8")
        cfg = self.cfg(env)
        self.assertIsNone(recovery.receipt_for(env.repo, cfg, TASK))
        grant = recovery.renew_revalidation(env.repo, cfg, TASK, "contract changed after the fix")
        self.assertEqual(grant["implementation"], env.implementation)
        run_id = [e["run"] for e in recovery.journal_events(env.repo) if e["event"] == "run_end"][-1]
        with mock.patch.object(recovery, "ownership_evidence", return_value={
            "owner_pid": DEAD_PID, "owner_source": "journal", "supervisor": None,
            "census_size": 3, "census_at": "test", "matched_writers": []}):
            receipt = self.recover(env, run_id=run_id)
        self.assertIsNotNone(recovery.receipt_for(env.repo, cfg, TASK))
        self.assertEqual(recovery.revalidation_budget(env.repo, cfg, TASK, receipt)["allowed"], 2)

    def test_renewal_needs_a_passing_oracle_and_an_unclosed_task(self) -> None:
        env = build(self.root)
        self.recover(env)
        self.commit_row(env)
        cfg = self.cfg(env)
        with self.assertRaisesRegex(recovery.RecoveryError, "granted once .* 0 dispatch"):
            recovery.renew_revalidation(env.repo, cfg, TASK, "nothing dispatched yet")
        code, _ = self.run_gate(env, [], close=False)     # no oracle ran
        self.assertEqual(code, 3)
        with self.assertRaisesRegex(recovery.RecoveryError, "no PASS from the canonical oracle"):
            recovery.renew_revalidation(env.repo, cfg, TASK, "worker gave up")

        closed = build(self.root / "closed", judge=self.JUDGE_ON)
        self.recover(closed)
        self.commit_row(closed)
        code, report = self.run_gate(closed, [])
        self.assertEqual(code, 0, report)
        with self.assertRaisesRegex(recovery.RecoveryError, "closed after"):
            recovery.renew_revalidation(closed.repo, self.cfg(closed), TASK, "already done")


if __name__ == "__main__":
    unittest.main()
