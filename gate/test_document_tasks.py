"""Offline lifecycle and regression tests for opt-in `kind: document` tasks."""

from __future__ import annotations

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

from test_compat import GATE, committed_runner_repo, load_gate_module, run


DOC_VERIFY_CMD = (
    "python -c \"from pathlib import Path; "
    "files = ['docs/a.md', 'docs/b.md', 'docs/c.md']; "
    "assert all(Path(f).is_file() and '## Summary' in Path(f).read_text(encoding='utf-8') for f in files); "
    "print('3 docs checked')\""
)


def doc_repo(root: Path, *, doc_globs: list[str] | None = None, verify_cmd: str | None = None) -> Path:
    """Create a committed git repo with hook installed and a document task."""
    repo = committed_runner_repo(root, ["codex"])
    cfg_path = repo / ".gate" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["doc_only_globs"] = doc_globs if doc_globs is not None else [
        "TASK_QUEUE.md",
        "CONTEXT.md",
        "NEXT_SESSION.md",
        "docs/*.md",
        "docs/**/*.md",
    ]
    if verify_cmd is not None:
        cfg["verify_cmd"] = verify_cmd
    cfg["delivery"] = {"stage": "module"}
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    queue = (
        "# TASK_QUEUE\n\n"
        "| # | ID | name | status | start baseline | end baseline |\n"
        "|---|---|---|---|---|---|\n"
        "| 1 | DOC-1 | architecture notes | `TODO` | — | — |\n"
        "| 2 | CODE-1 | loader | `TODO` | 1 passed | — |\n\n"
        "<!-- task:DOC-1 kind: document -->\n"
        "<!-- task:DOC-1 files: docs/a.md, docs/b.md, docs/c.md -->\n"
        "<!-- task:DOC-1 impact: local -->\n"
        f"<!-- task:DOC-1 verify: {json.dumps({'cmd': DOC_VERIFY_CMD, 'timeout_s': 60})} -->\n"
        "<!-- task:CODE-1 files: src/loader.py -->\n"
        "<!-- task:CODE-1 impact: local -->\n"
        "<!-- task:CODE-1 verify: {\"cmd\": \"python -c \\\"print('1 passed')\\\"\", \"timeout_s\": 60} -->\n"
    )
    (repo / "TASK_QUEUE.md").write_text(queue, encoding="utf-8")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "loader.py").write_text("x = 1\n", encoding="utf-8")
    res = run("git", "-C", str(repo), "add", "TASK_QUEUE.md", ".gate/config.json", "src/loader.py")
    if res.returncode:
        raise AssertionError(res.stderr)
    res = run("git", "-C", str(repo), "commit", "-q", "-m", "admit doc and code tasks")
    if res.returncode:
        raise AssertionError(res.stderr)
    res = run(sys.executable, str(GATE), "install-hook", "--repo", str(repo))
    if res.returncode:
        raise AssertionError(res.stderr)
    return repo


def write_docs(repo: Path, text_by_name: dict[str, str] | None = None) -> None:
    defaults = {
        "docs/a.md": "# Doc A\n\n## Summary\nVerified content A.\n",
        "docs/b.md": "# Doc B\n\n## Summary\nVerified content B.\n",
        "docs/c.md": "# Doc C\n\n## Summary\nVerified content C.\n",
    }
    if text_by_name:
        defaults.update(text_by_name)
    for rel, content in defaults.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def flip_row(repo: Path, task_id: str, new_status: str = "DONE") -> None:
    qpath = repo / "TASK_QUEUE.md"
    lines = qpath.read_text(encoding="utf-8").splitlines(keepends=True)
    out = []
    for line in lines:
        if f"| {task_id} |" in line:
            line = line.replace("`TODO`", f"`{new_status}`").replace("`IN_PROGRESS`", f"`{new_status}`")
        out.append(line)
    qpath.write_text("".join(out), encoding="utf-8")


def commit(repo: Path, msg: str, *, no_verify: bool = False) -> subprocess.CompletedProcess[str]:
    args = ["git", "-C", str(repo), "commit", "-q", "-m", msg]
    if no_verify:
        args.append("--no-verify")
    return run(*args)


class DocumentTaskLifecycleTests(unittest.TestCase):
    def _assert_run_refuses_without_worker(self, gate, repo: Path, msg: str | None = None) -> None:
        calls: list[str] = []

        def unexpected_spawn(repo_arg, cfg_arg, worker, prompt, pf, **kwargs):
            calls.append(worker)
            raise AssertionError(f"unexpected worker launch: {worker}")

        with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), mock.patch.object(
            gate, "resolve_tool", side_effect=lambda name: name
        ), mock.patch.object(gate, "spawn_worker", side_effect=unexpected_spawn):
            with self.assertRaises(SystemExit) as ctx:
                gate.cmd_run(SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=False, force=True))
        self.assertEqual(ctx.exception.code, 3, msg)
        self.assertEqual(calls, [], msg)

    def test_admission_validates_kind_deliverables_and_verify_while_preserving_code_tasks(self) -> None:
        gate = load_gate_module()
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            admit_res = run(sys.executable, str(GATE), "admit", "--plan", "--repo", str(repo))
            self.assertEqual(admit_res.returncode, 0, admit_res.stdout + admit_res.stderr)
            self.assertIn("task-owned", admit_res.stdout)

            # A regular code task declaring a .md file keeps legacy admission behavior.
            qpath = repo / "TASK_QUEUE.md"
            base_q = qpath.read_text(encoding="utf-8")
            code_md_q = base_q + "| 3 | CODE-MD | readme | `TODO` | 1 passed | — |\n<!-- task:CODE-MD files: README.md -->\n"
            cfg = gate.load_config(repo)
            verdicts = gate.admit_verdicts(
                cfg, repo, gate.parse_queue(code_md_q), gate.parse_task_files(code_md_q),
                {}, gate.parse_task_after(code_md_q), gate.parse_task_approved(code_md_q),
                queue_text=code_md_q,
            )
            self.assertEqual(verdicts["CODE-MD"]["verdict"], "admit")

            bad_cases = [
                ("unknown_kind", base_q.replace("kind: document", "kind: prose")),
                ("empty_kind", base_q.replace("kind: document", "kind: ")),
                ("unclosed_kind", base_q.replace("<!-- task:DOC-1 kind: document -->", "<!-- task:DOC-1 kind: document")),
                ("duplicate_kind", base_q + "<!-- task:DOC-1 kind: document -->\n"),
                ("conflicting_kind", base_q + "<!-- task:DOC-1 kind: code -->\n"),
                ("status_only_paths", base_q.replace("docs/a.md, docs/b.md, docs/c.md", "TASK_QUEUE.md, CONTEXT.md, NEXT_SESSION.md, docs/handoff/x.md")),
                ("glob_path", base_q.replace("docs/a.md, docs/b.md, docs/c.md", "docs/*.md")),
                ("missing_verify", "\n".join(ln for ln in base_q.splitlines() if "task:DOC-1 verify:" not in ln) + "\n"),
                ("trivial_verify", base_q.replace(json.dumps(DOC_VERIFY_CMD), json.dumps("true"))),
            ]
            for label, bad_q in bad_cases:
                with self.subTest(case=label):
                    qpath.write_text(bad_q, encoding="utf-8")
                    # Advisory mode (strict_admit=False) must still stop on invalid kind/doc contract.
                    cfg_adv = dict(cfg, strict_admit=False)
                    with open(cfg_path := repo / ".gate" / "config.json", "w", encoding="utf-8") as fh:
                        json.dump(cfg_adv, fh)
                    v = gate.admit_verdicts(
                        cfg_adv, repo, gate.parse_queue(bad_q), gate.parse_task_files(bad_q),
                        {}, gate.parse_task_after(bad_q), gate.parse_task_approved(bad_q),
                        queue_text=bad_q,
                    )
                    self.assertNotEqual(v["DOC-1"]["verdict"], "admit", label)
                    self.assertTrue(v["DOC-1"]["invalid_kind"] or v["DOC-1"]["invalid_contract"], label)
                    self._assert_run_refuses_without_worker(gate, repo, label)

    def test_document_task_full_lifecycle_admit_verify_commit_audit_and_history_immutability(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            write_docs(repo)

            v_res = run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "DOC-1")
            self.assertEqual(v_res.returncode, 0, v_res.stdout + v_res.stderr)
            verdict = json.loads((repo / ".gate" / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual(verdict["kind"], "document")
            self.assertEqual(verdict["scope"], "task")

            flip_row(repo, "DOC-1", "DONE")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "docs/a.md", "docs/b.md", "docs/c.md")
            c_res = commit(repo, "close DOC-1")
            self.assertEqual(c_res.returncode, 0, c_res.stdout + c_res.stderr)

            a_res = run(sys.executable, str(GATE), "audit", "--repo", str(repo), "--last", "5")
            self.assertEqual(a_res.returncode, 0, a_res.stdout + a_res.stderr)
            self.assertIn("3 doc deliverable(s)", a_res.stdout)

            # Later mutable queue edits (e.g. changing files/kind or removing the row) must not break past audit.
            qpath = repo / "TASK_QUEUE.md"
            qpath.write_text("# TASK_QUEUE\n\n| # | ID | name | status | start baseline | end baseline |\n|---|---|---|---|---|---|\n", encoding="utf-8")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md")
            self.assertEqual(commit(repo, "archive queue").returncode, 0)

            a_later = run(sys.executable, str(GATE), "audit", "--repo", str(repo), "--last", "10")
            self.assertEqual(a_later.returncode, 0, a_later.stdout + a_later.stderr)
            self.assertIn("3 doc deliverable(s)", a_later.stdout)

    def test_code_task_still_rejects_docs_only_closure_and_legitimate_code_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            # Flip DOC-1 to DONE first via non-DONE removal or skip to test CODE-1 directly.
            (repo / "docs").mkdir(exist_ok=True)
            (repo / "docs" / "only_doc.md").write_text("# Note\nNot code.\n", encoding="utf-8")
            v_res = run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "CODE-1")
            self.assertEqual(v_res.returncode, 0, v_res.stdout + v_res.stderr)
            flip_row(repo, "CODE-1", "DONE")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "docs/only_doc.md")
            bad_c = commit(repo, "fake code closure")
            self.assertNotEqual(bad_c.returncode, 0)
            self.assertIn("touches only documentation", bad_c.stderr)

            # Legitimate code change succeeds.
            run("git", "-C", str(repo), "reset", "HEAD", "docs/only_doc.md")
            (repo / "src" / "loader.py").write_text("x = 2\n", encoding="utf-8")
            v_res = run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "CODE-1")
            self.assertEqual(v_res.returncode, 0)
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "src/loader.py")
            ok_c = commit(repo, "real code closure")
            self.assertEqual(ok_c.returncode, 0, ok_c.stdout + ok_c.stderr)

    def test_status_only_empty_missing_stale_and_staged_worktree_mismatch_blocked(self) -> None:
        # 1. Status-only commit (deliverables pre-committed earlier, DONE commit only touches queue).
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            write_docs(repo)
            run("git", "-C", str(repo), "add", "docs/a.md", "docs/b.md", "docs/c.md")
            self.assertEqual(commit(repo, "pre-commit docs without DONE").returncode, 0)
            self.assertEqual(run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "DOC-1").returncode, 0)
            flip_row(repo, "DOC-1", "DONE")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md")
            res = commit(repo, "status-only DONE")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("without staging any substantive declared deliverable", res.stderr)

        # 2. Missing or empty deliverable.
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            write_docs(repo, {"docs/c.md": "   \n"})
            # Use a permissive verify command to prove check-commit still enforces non-empty deliverable.
            run("git", "-C", str(repo), "add", "docs/a.md", "docs/b.md", "docs/c.md")
            self.assertEqual(commit(repo, "pre-stage empty").returncode, 0)
            # Re-verify with real docs? Instead use a separate repo where verify passes on empty c.md.
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            qpath = repo / "TASK_QUEUE.md"
            qpath.write_text(
                qpath.read_text(encoding="utf-8").replace(
                    json.dumps(DOC_VERIFY_CMD), json.dumps("python -c \"print('ok')\"")
                ),
                encoding="utf-8",
            )
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md")
            self.assertEqual(commit(repo, "set simple check").returncode, 0)
            write_docs(repo, {"docs/c.md": "  \n"})
            self.assertEqual(run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "DOC-1").returncode, 0)
            flip_row(repo, "DOC-1", "DONE")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "docs/a.md", "docs/b.md", "docs/c.md")
            res = commit(repo, "empty deliverable")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("empty", res.stderr)

            # Missing third deliverable from index/worktree.
            run("git", "-C", str(repo), "reset", "HEAD", "docs/c.md")
            (repo / "docs" / "c.md").unlink()
            self.assertEqual(run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "DOC-1").returncode, 0)
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "docs/a.md", "docs/b.md")
            res = commit(repo, "missing deliverable")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("missing", res.stderr)

        # 3. Post-verification modification blocked.
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            write_docs(repo)
            self.assertEqual(run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "DOC-1").returncode, 0)
            (repo / "docs" / "b.md").write_text("# Doc B\n\n## Summary\nEdited after verify.\n", encoding="utf-8")
            flip_row(repo, "DOC-1", "DONE")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "docs/a.md", "docs/b.md", "docs/c.md")
            res = commit(repo, "edited after verify")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("changed after its local check", res.stderr)

        # 4. Staged-vs-working-tree mismatch blocked so committed bytes equal verified bytes.
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            write_docs(repo, {"docs/b.md": "placeholder staged only\n"})
            run("git", "-C", str(repo), "add", "docs/a.md", "docs/b.md", "docs/c.md")
            # Now overwrite working tree with verified bytes without re-staging!
            write_docs(repo)
            self.assertEqual(run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "DOC-1").returncode, 0)
            flip_row(repo, "DOC-1", "DONE")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md")
            res = commit(repo, "staged differs from verified worktree")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("staged bytes differ from working-tree bytes", res.stderr)

    def test_wrong_stage_full_expired_and_cmd_override_verdicts_blocked(self) -> None:
        gate = load_gate_module()
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            write_docs(repo)
            flip_row(repo, "DOC-1", "DONE")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "docs/a.md", "docs/b.md", "docs/c.md")

            # Wrong task PASS (CODE-1).
            self.assertEqual(run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "CODE-1").returncode, 0)
            self.assertNotEqual(commit(repo, "wrong task").returncode, 0)

            # Full queue PASS cannot close a document task.
            self.assertEqual(run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--queue").returncode, 0)
            res_q = commit(repo, "full queue pass")
            self.assertNotEqual(res_q.returncode, 0)
            self.assertIn("requires a matching task-local PASS", res_q.stderr)

            # Stage PASS cannot close a document task.
            cfg = gate.load_config(repo)
            gate.run_acceptance(repo, cfg, DOC_VERIFY_CMD, scope="stage")
            res_s = commit(repo, "stage pass")
            self.assertNotEqual(res_s.returncode, 0)
            self.assertIn("stage checkpoint cannot close", res_s.stderr)

            # Ad-hoc --cmd override on verify --task DOC-1 is refused.
            res_cmd = run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "DOC-1", "--cmd", "python -c \"print('bypass')\"")
            self.assertNotEqual(res_cmd.returncode, 0)

            # Expired task PASS is rejected.
            self.assertEqual(run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "DOC-1").returncode, 0)
            vp = repo / ".gate" / "verdict.json"
            vdata = json.loads(vp.read_text(encoding="utf-8"))
            vdata["at_epoch"] = time.time() - 999999
            vp.write_text(json.dumps(vdata), encoding="utf-8")
            res_exp = commit(repo, "expired verdict")
            self.assertNotEqual(res_exp.returncode, 0)
            self.assertIn("old", res_exp.stderr)

    def test_contract_freeze_blocks_worker_reclassification_or_oracle_tampering_and_allows_host_reconcile(self) -> None:
        # 1. Worker tries to self-classify CODE-1 as document in DONE commit.
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            write_docs(repo)
            qpath = repo / "TASK_QUEUE.md"
            qpath.write_text(qpath.read_text(encoding="utf-8") + "<!-- task:CODE-1 kind: document -->\n", encoding="utf-8")
            flip_row(repo, "CODE-1", "DONE")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "docs/a.md")
            res = commit(repo, "self-reclassify code as doc")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("cannot change kind", res.stderr)
            self.assertIn("only host/planner before dispatch may reconcile contracts", res.stderr)
            self.assertNotIn("separate non-DONE planning commit", res.stderr)

        # 2. Worker tries to change verify command or files of DOC-1 in DONE commit.
        for tamper_label, repl in (
            ("change_verify", (json.dumps(DOC_VERIFY_CMD), json.dumps("python -c \"print('fake')\""))),
            ("change_files", ("docs/a.md, docs/b.md, docs/c.md", "docs/a.md")),
            ("change_kind", ("kind: document", "kind: code")),
        ):
            with self.subTest(tamper=tamper_label), tempfile.TemporaryDirectory() as td:
                repo = doc_repo(Path(td))
                write_docs(repo)
                self.assertEqual(run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "DOC-1").returncode, 0)
                qpath = repo / "TASK_QUEUE.md"
                qpath.write_text(qpath.read_text(encoding="utf-8").replace(*repl), encoding="utf-8")
                flip_row(repo, "DOC-1", "DONE")
                run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "docs/a.md", "docs/b.md", "docs/c.md")
                res = commit(repo, f"tamper {tamper_label}")
                self.assertNotEqual(res.returncode, 0, tamper_label)
                # Also verify audit catches it if bypassed with --no-verify.
                bypass = commit(repo, f"bypass {tamper_label}", no_verify=True)
                self.assertEqual(bypass.returncode, 0, bypass.stderr)
                aud = run(sys.executable, str(GATE), "audit", "--repo", str(repo), "--last", "3")
                self.assertNotEqual(aud.returncode, 0, tamper_label)
                self.assertIn("FAKE_COMPLETION", aud.stdout)

        # 3. Host can reconcile a stopped task in a separate non-DONE planning commit, then close cleanly.
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            qpath = repo / "TASK_QUEUE.md"
            new_cmd = "python -c \"from pathlib import Path; assert Path('docs/a.md').read_text().strip(); print('1 doc ok')\""
            qpath.write_text(
                qpath.read_text(encoding="utf-8")
                .replace("docs/a.md, docs/b.md, docs/c.md", "docs/a.md")
                .replace(json.dumps(DOC_VERIFY_CMD), json.dumps(new_cmd)),
                encoding="utf-8",
            )
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md")
            self.assertEqual(commit(repo, "host reconcile DOC-1 scope").returncode, 0)
            write_docs(repo)
            self.assertEqual(run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "DOC-1").returncode, 0)
            flip_row(repo, "DOC-1", "DONE")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "docs/a.md")
            self.assertEqual(commit(repo, "close reconciled DOC-1").returncode, 0)
            self.assertEqual(run(sys.executable, str(GATE), "audit", "--repo", str(repo), "--last", "5").returncode, 0)

        # 4. Brand-new document row added straight to DONE without preexisting HEAD declaration is blocked.
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            write_docs(repo, {"docs/new.md": "# New\n\n## Summary\nBody.\n"})
            qpath = repo / "TASK_QUEUE.md"
            cmd_new = "python -c \"from pathlib import Path; assert Path('docs/new.md').read_text().strip(); print('1 doc ok')\""
            qpath.write_text(
                qpath.read_text(encoding="utf-8")
                + "| 3 | DOC-NEW | instant doc | `DONE` | — | — |\n"
                + "<!-- task:DOC-NEW kind: document -->\n"
                + "<!-- task:DOC-NEW files: docs/new.md -->\n"
                + f"<!-- task:DOC-NEW verify: {json.dumps({'cmd': cmd_new, 'timeout_s': 60})} -->\n",
                encoding="utf-8",
            )
            self.assertEqual(run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "DOC-NEW").returncode, 0)
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "docs/new.md")
            res = commit(repo, "brand new DONE doc")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("must be declared in preexisting HEAD", res.stderr)
            self.assertEqual(commit(repo, "bypass new DONE doc", no_verify=True).returncode, 0)
            aud = run(sys.executable, str(GATE), "audit", "--repo", str(repo), "--last", "3")
            self.assertNotEqual(aud.returncode, 0)
            self.assertIn("FAKE_COMPLETION", aud.stdout)

    def test_document_task_preserves_frozen_globs_and_project_prefix_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            cfg_path = repo / ".gate" / "config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["frozen_globs"] = ["docs/frozen.md"]
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            (repo / "docs").mkdir(exist_ok=True)
            (repo / "docs" / "frozen.md").write_text("do not touch\n", encoding="utf-8")
            run("git", "-C", str(repo), "add", ".gate/config.json", "docs/frozen.md")
            self.assertEqual(commit(repo, "add frozen doc").returncode, 0)

            write_docs(repo)
            self.assertEqual(run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "DOC-1").returncode, 0)
            (repo / "docs" / "frozen.md").write_text("tampered\n", encoding="utf-8")
            flip_row(repo, "DOC-1", "DONE")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "docs/a.md", "docs/b.md", "docs/c.md", "docs/frozen.md")
            res = commit(repo, "doc closure touching frozen path")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("frozen path modified", res.stderr)

    def test_runner_receipt_records_document_kind_and_settles_with_hook(self) -> None:
        gate = load_gate_module()
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            # Remove CODE-1 so queue closes after DOC-1.
            qpath = repo / "TASK_QUEUE.md"
            lines = [ln for ln in qpath.read_text(encoding="utf-8").splitlines(keepends=True) if "CODE-1" not in ln]
            qpath.write_text("".join(lines), encoding="utf-8")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md")
            self.assertEqual(commit(repo, "single doc task queue").returncode, 0)

            def fake_worker(repo_arg, cfg_arg, worker, prompt, pf, **kwargs):
                write_docs(repo_arg)
                v = run(sys.executable, str(GATE), "verify", "--repo", str(repo_arg), "--task", "DOC-1")
                self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
                flip_row(repo_arg, "DOC-1", "DONE")
                run("git", "-C", str(repo_arg), "add", "TASK_QUEUE.md", "docs/a.md", "docs/b.md", "docs/c.md")
                c = commit(repo_arg, "worker close DOC-1")
                self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
                return gate.WorkerResult(0, "done"), ""

            with mock.patch.object(gate, "resolve_tool", return_value="codex"), \
                 mock.patch.object(gate, "spawn_worker", side_effect=fake_worker):
                gate.cmd_run(SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=True, force=False))

            events = list(gate.iter_journal(repo))
            done_events = [e for e in events if e.get("event") == "task_done"]
            self.assertEqual(len(done_events), 1)
            self.assertEqual(done_events[0]["task"], "DOC-1")
            self.assertEqual(done_events[0]["kind"], "document")
            settled = [e for e in events if e.get("event") == "package_settled"]
            self.assertEqual(len(settled), 1)
            self.assertEqual(settled[0]["tasks"], ["DOC-1"])
            self.assertEqual(run(sys.executable, str(GATE), "audit", "--repo", str(repo), "--last", "5").returncode, 0)

    def test_is_status_path_and_gate_only_admission_refusal_including_project_prefix(self) -> None:
        gate = load_gate_module()
        self.assertTrue(gate.is_status_path(gate.DEFAULT_CONFIG, ".gate/verdict.json"))
        self.assertTrue(gate.is_status_path(gate.DEFAULT_CONFIG, ".gate"))
        self.assertTrue(gate.is_status_path(gate.DEFAULT_CONFIG, "./.gate/verdict.json"))
        self.assertTrue(gate.is_status_path(gate.DEFAULT_CONFIG, "./TASK_QUEUE.md"))
        self.assertFalse(gate.is_status_path(gate.DEFAULT_CONFIG, ".github/workflows/doc.md"))
        self.assertFalse(gate.is_status_path(gate.DEFAULT_CONFIG, "docs/a.md"))

        prefixed_cfg = dict(gate.DEFAULT_CONFIG, project_prefix="subproj")
        self.assertTrue(gate.is_status_path(prefixed_cfg, ".gate/verdict.json"))
        self.assertTrue(gate.is_status_path(prefixed_cfg, "subproj/.gate/verdict.json"))
        self.assertTrue(gate.is_status_path(prefixed_cfg, "./subproj/.gate/verdict.json"))
        self.assertTrue(gate.is_status_path(prefixed_cfg, "subproj/TASK_QUEUE.md"))
        self.assertTrue(gate.is_status_path(prefixed_cfg, "TASK_QUEUE.md"))
        self.assertFalse(gate.is_status_path(prefixed_cfg, "subproj/docs/a.md"))
        self.assertFalse(gate.is_status_path(prefixed_cfg, "docs/a.md"))
        self.assertFalse(gate.is_status_path(prefixed_cfg, "subproj/.github/guide.md"))

        # Unprefixed repo: .gate-only deliverables refused; legitimate substantive docs admitted.
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            cfg = gate.load_config(repo)
            qpath = repo / "TASK_QUEUE.md"
            base_q = qpath.read_text(encoding="utf-8")
            bad_q = base_q.replace("docs/a.md, docs/b.md, docs/c.md", ".gate/verdict.json, .gate/journal.ndjson")
            qpath.write_text(bad_q, encoding="utf-8")
            verdicts = gate.admit_verdicts(
                cfg, repo, gate.parse_queue(bad_q), gate.parse_task_files(bad_q),
                {}, gate.parse_task_after(bad_q), gate.parse_task_approved(bad_q),
                queue_text=bad_q,
            )
            self.assertEqual(verdicts["DOC-1"]["verdict"], "refuse")
            self.assertTrue(verdicts["DOC-1"]["invalid_contract"])
            self._assert_run_refuses_without_worker(gate, repo)

            good_q = base_q.replace("docs/a.md, docs/b.md, docs/c.md", "docs/a.md, .github/guide.md")
            verdicts_ok = gate.admit_verdicts(
                cfg, repo, gate.parse_queue(good_q), gate.parse_task_files(good_q),
                {}, gate.parse_task_after(good_q), gate.parse_task_approved(good_q),
                queue_text=good_q,
            )
            self.assertEqual(verdicts_ok["DOC-1"]["verdict"], "admit")

        # Prefixed repo: both .gate/... and <prefix>/.gate/... refused; project-relative docs admitted.
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            cfg_path = repo / ".gate" / "config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["project_prefix"] = "subproj"
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            (repo / "subproj").mkdir(exist_ok=True)
            base_q = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")
            sub_q = repo / "subproj" / "TASK_QUEUE.md"
            for bad_files in (".gate/verdict.json", "subproj/.gate/verdict.json"):
                with self.subTest(bad_files=bad_files):
                    bad_q = base_q.replace("docs/a.md, docs/b.md, docs/c.md", bad_files)
                    sub_q.write_text(bad_q, encoding="utf-8")
                    loaded = gate.load_config(repo)
                    verdicts = gate.admit_verdicts(
                        loaded, repo, gate.parse_queue(bad_q), gate.parse_task_files(bad_q),
                        {}, gate.parse_task_after(bad_q), gate.parse_task_approved(bad_q),
                        queue_text=bad_q,
                    )
                    self.assertEqual(verdicts["DOC-1"]["verdict"], "refuse")
                    self.assertTrue(verdicts["DOC-1"]["invalid_contract"])
                    self._assert_run_refuses_without_worker(gate, repo, bad_files)

            good_q = base_q.replace("docs/a.md, docs/b.md, docs/c.md", "docs/a.md, .github/guide.md")
            sub_q.write_text(good_q, encoding="utf-8")
            loaded = gate.load_config(repo)
            verdicts_ok = gate.admit_verdicts(
                loaded, repo, gate.parse_queue(good_q), gate.parse_task_files(good_q),
                {}, gate.parse_task_after(good_q), gate.parse_task_approved(good_q),
                queue_text=good_q,
            )
            self.assertEqual(verdicts_ok["DOC-1"]["verdict"], "admit")

    def test_runner_blocks_two_commit_code_to_document_reclassification(self) -> None:
        gate = load_gate_module()
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            qpath = repo / "TASK_QUEUE.md"
            lines = [ln for ln in qpath.read_text(encoding="utf-8").splitlines(keepends=True) if "DOC-1" not in ln]
            qtext = "".join(lines).replace(
                "<!-- task:CODE-1 files: src/loader.py -->",
                "<!-- task:CODE-1 files: src/loader.py, docs/guide.md -->",
            )
            qpath.write_text(qtext, encoding="utf-8")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md")
            self.assertEqual(commit(repo, "single code task with doc file").returncode, 0)
            base_sha = run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
            commit_shas: list[str] = []

            def fake_worker(repo_arg, cfg_arg, worker, prompt, pf, **kwargs):
                doc_check = "python -c \"from pathlib import Path; assert Path('docs/guide.md').read_text().strip(); print('1 doc ok')\""
                q_cur = (repo_arg / "TASK_QUEUE.md").read_text(encoding="utf-8")
                q_c1 = (
                    q_cur.replace(
                        json.dumps("python -c \"print('1 passed')\""),
                        json.dumps(doc_check),
                    )
                    + "<!-- task:CODE-1 kind: document -->\n"
                )
                (repo_arg / "TASK_QUEUE.md").write_text(q_c1, encoding="utf-8")
                run("git", "-C", str(repo_arg), "add", "TASK_QUEUE.md")
                c1 = commit(repo_arg, "C1 worker reclassifies CODE-1 to document")
                self.assertEqual(c1.returncode, 0, c1.stdout + c1.stderr)
                commit_shas.append(run("git", "-C", str(repo_arg), "rev-parse", "HEAD").stdout.strip())

                write_docs(repo_arg, {"docs/guide.md": "# Guide\n\n## Summary\nDocs only.\n"})
                v = run(sys.executable, str(GATE), "verify", "--repo", str(repo_arg), "--task", "CODE-1")
                self.assertEqual(v.returncode, 0, v.stdout + v.stderr)

                flip_row(repo_arg, "CODE-1", "DONE")
                run("git", "-C", str(repo_arg), "add", "TASK_QUEUE.md", "docs/guide.md")
                c2 = commit(repo_arg, "C2 worker closes reclassified CODE-1 as document")
                self.assertEqual(c2.returncode, 0, c2.stdout + c2.stderr)
                commit_shas.append(run("git", "-C", str(repo_arg), "rev-parse", "HEAD").stdout.strip())
                return gate.WorkerResult(0, "done"), None

            with mock.patch.object(gate, "resolve_tool", return_value="codex"), \
                 mock.patch.object(gate, "spawn_worker", side_effect=fake_worker):
                with self.assertRaises(SystemExit) as ctx:
                    gate.cmd_run(SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=True, force=False))
                self.assertEqual(ctx.exception.code, 3)

            events = list(gate.iter_journal(repo))
            self.assertFalse(any(e.get("event") in ("task_done", "stage_acceptance", "full_acceptance", "package_settled") for e in events))
            proto = [e for e in events if e.get("event") == "worker_protocol_error"]
            self.assertEqual(len(proto), 1)
            self.assertEqual(proto[0]["task"], "CODE-1")
            self.assertIn("only host/planner before dispatch may reconcile contracts", proto[0]["reason"])
            report = (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")
            self.assertIn("worker_protocol_error:CODE-1", report)
            # Worker commits, files, queue, and failed receipts must all be preserved for host recovery.
            self.assertEqual(len(commit_shas), 2)
            self.assertEqual(run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip(), commit_shas[-1])
            self.assertEqual(run("git", "-C", str(repo), "rev-list", "--count", f"{base_sha}..HEAD").stdout.strip(), "2")
            self.assertTrue((repo / "docs" / "guide.md").is_file())
            self.assertTrue(any(e.get("event") == "attempt_start" for e in events))

    def test_runner_blocks_two_commit_document_verify_and_files_weakening(self) -> None:
        gate = load_gate_module()
        weak_verify = "python -c \"print('weak ok')\""
        cases = (
            ("weaken_verify", json.dumps(DOC_VERIFY_CMD), json.dumps(weak_verify), ["docs/a.md", "docs/b.md", "docs/c.md"]),
            ("weaken_files", "docs/a.md, docs/b.md, docs/c.md", "docs/a.md", ["docs/a.md"]),
        )
        for label, old_tok, new_tok, staged_docs in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as td:
                repo = doc_repo(Path(td))
                qpath = repo / "TASK_QUEUE.md"
                lines = [ln for ln in qpath.read_text(encoding="utf-8").splitlines(keepends=True) if "CODE-1" not in ln]
                qpath.write_text("".join(lines), encoding="utf-8")
                run("git", "-C", str(repo), "add", "TASK_QUEUE.md")
                self.assertEqual(commit(repo, "single doc task queue").returncode, 0)
                base_sha = run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
                commit_shas: list[str] = []

                def fake_worker(repo_arg, cfg_arg, worker, prompt, pf, **kwargs):
                    qp = repo_arg / "TASK_QUEUE.md"
                    qp.write_text(qp.read_text(encoding="utf-8").replace(old_tok, new_tok), encoding="utf-8")
                    run("git", "-C", str(repo_arg), "add", "TASK_QUEUE.md")
                    c1 = commit(repo_arg, f"C1 {label}")
                    self.assertEqual(c1.returncode, 0, c1.stdout + c1.stderr)
                    commit_shas.append(run("git", "-C", str(repo_arg), "rev-parse", "HEAD").stdout.strip())

                    # Write non-empty trivial docs (for weaken_verify) or un-staged b/c (for weaken_files)
                    # so ordinary hooked C2 succeeds against C1 while violating the original contract!
                    if label == "weaken_verify":
                        write_docs(repo_arg, {"docs/a.md": "trivial\n", "docs/b.md": "trivial\n", "docs/c.md": "trivial\n"})
                    else:
                        write_docs(repo_arg)
                    v = run(sys.executable, str(GATE), "verify", "--repo", str(repo_arg), "--task", "DOC-1")
                    self.assertEqual(v.returncode, 0, v.stdout + v.stderr)

                    flip_row(repo_arg, "DOC-1", "DONE")
                    run("git", "-C", str(repo_arg), "add", "TASK_QUEUE.md", *staged_docs)
                    c2 = commit(repo_arg, f"C2 {label}")
                    self.assertEqual(c2.returncode, 0, c2.stdout + c2.stderr)
                    commit_shas.append(run("git", "-C", str(repo_arg), "rev-parse", "HEAD").stdout.strip())
                    return gate.WorkerResult(0, "done"), None

                with mock.patch.object(gate, "resolve_tool", return_value="codex"), \
                     mock.patch.object(gate, "spawn_worker", side_effect=fake_worker):
                    with self.assertRaises(SystemExit) as ctx:
                        gate.cmd_run(SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=True, force=False))
                    self.assertEqual(ctx.exception.code, 3, label)

                events = list(gate.iter_journal(repo))
                self.assertFalse(any(e.get("event") in ("task_done", "stage_acceptance", "full_acceptance", "package_settled") for e in events), label)
                proto = [e for e in events if e.get("event") == "worker_protocol_error"]
                self.assertEqual(len(proto), 1, label)
                self.assertEqual(proto[0]["task"], "DOC-1")
                self.assertEqual(run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip(), commit_shas[-1])
                self.assertEqual(run("git", "-C", str(repo), "rev-list", "--count", f"{base_sha}..HEAD").stdout.strip(), "2")

    def test_runner_blocks_c1_or_uncommitted_contract_mutation_without_retry_or_fallback(self) -> None:
        gate = load_gate_module()
        for case in ("c1_failed_exit", "c1_timed_out", "c1_timeout_exc", "uncommitted_outage", "uncommitted_not_done"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                repo = doc_repo(Path(td))
                cfg_path = repo / ".gate" / "config.json"
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg["workers"] = ["codex", "claude"]
                cfg["max_attempts_per_task"] = 3
                cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
                qpath = repo / "TASK_QUEUE.md"
                lines = [ln for ln in qpath.read_text(encoding="utf-8").splitlines(keepends=True) if "DOC-1" not in ln]
                qpath.write_text("".join(lines), encoding="utf-8")
                run("git", "-C", str(repo), "add", ".gate/config.json", "TASK_QUEUE.md")
                self.assertEqual(commit(repo, "setup multi-attempt code queue").returncode, 0)
                base_sha = run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
                dispatches = 0

                def fake_worker(repo_arg, cfg_arg, worker, prompt, pf, **kwargs):
                    nonlocal dispatches
                    dispatches += 1
                    qp = repo_arg / "TASK_QUEUE.md"
                    qp.write_text(qp.read_text(encoding="utf-8") + "<!-- task:CODE-1 kind: document -->\n", encoding="utf-8")
                    (repo_arg / "src" / "partial.py").write_text("# partial\n", encoding="utf-8")
                    if case.startswith("c1_"):
                        run("git", "-C", str(repo_arg), "add", "TASK_QUEUE.md")
                        c1 = commit(repo_arg, "C1 mutate kind without DONE")
                        self.assertEqual(c1.returncode, 0, c1.stdout + c1.stderr)
                    if case == "c1_timed_out":
                        return gate.WorkerResult(124, "timed out", timed_out=True), None
                    if case == "c1_timeout_exc":
                        raise subprocess.TimeoutExpired("codex", 10)
                    if case == "uncommitted_outage":
                        return gate.WorkerResult(1, "429 rate limit exceeded"), None
                    return gate.WorkerResult(1, "did not finish"), None

                with mock.patch.object(gate, "resolve_tool", side_effect=lambda name: name), \
                     mock.patch.object(gate, "spawn_worker", side_effect=fake_worker):
                    with self.assertRaises(SystemExit) as ctx:
                        gate.cmd_run(SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=True, force=False))
                    self.assertEqual(ctx.exception.code, 3, case)

                self.assertEqual(dispatches, 1, f"{case} must not retry or fall back")
                events = list(gate.iter_journal(repo))
                self.assertFalse(any(e.get("event") in ("task_done", "stage_acceptance", "full_acceptance", "package_settled") for e in events), case)
                proto = [e for e in events if e.get("event") == "worker_protocol_error"]
                self.assertEqual(len(proto), 1, case)
                self.assertEqual(proto[0]["task"], "CODE-1")
                if case == "c1_timed_out":
                    self.assertTrue(any(e.get("event") == "attempt_timeout" for e in events), case)
                if case.startswith("c1_"):
                    self.assertNotEqual(run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip(), base_sha)
                else:
                    self.assertEqual(run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip(), base_sha)
                    self.assertIn("kind: document", qpath.read_text(encoding="utf-8"))
                self.assertTrue((repo / "src" / "partial.py").is_file(), case)

    def test_runner_legitimate_code_task_with_doc_file_closes_as_code(self) -> None:
        gate = load_gate_module()
        with tempfile.TemporaryDirectory() as td:
            repo = doc_repo(Path(td))
            qpath = repo / "TASK_QUEUE.md"
            lines = [ln for ln in qpath.read_text(encoding="utf-8").splitlines(keepends=True) if "DOC-1" not in ln]
            qtext = "".join(lines).replace(
                "<!-- task:CODE-1 files: src/loader.py -->",
                "<!-- task:CODE-1 files: src/loader.py, docs/guide.md -->",
            )
            qpath.write_text(qtext, encoding="utf-8")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md")
            self.assertEqual(commit(repo, "single code task queue").returncode, 0)

            def fake_worker(repo_arg, cfg_arg, worker, prompt, pf, **kwargs):
                (repo_arg / "src" / "loader.py").write_text("x = 2\n", encoding="utf-8")
                write_docs(repo_arg, {"docs/guide.md": "# Guide\n\n## Summary\nCode docs.\n"})
                v = run(sys.executable, str(GATE), "verify", "--repo", str(repo_arg), "--task", "CODE-1")
                self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
                flip_row(repo_arg, "CODE-1", "DONE")
                run("git", "-C", str(repo_arg), "add", "TASK_QUEUE.md", "src/loader.py", "docs/guide.md")
                c = commit(repo_arg, "worker close CODE-1")
                self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
                return gate.WorkerResult(0, "done"), None

            with mock.patch.object(gate, "resolve_tool", return_value="codex"), \
                 mock.patch.object(gate, "spawn_worker", side_effect=fake_worker):
                gate.cmd_run(SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=True, force=False))

            events = list(gate.iter_journal(repo))
            done_events = [e for e in events if e.get("event") == "task_done"]
            self.assertEqual(len(done_events), 1)
            self.assertEqual(done_events[0]["task"], "CODE-1")
            self.assertEqual(done_events[0]["kind"], "code")
            settled = [e for e in events if e.get("event") == "package_settled"]
            self.assertEqual(len(settled), 1)
            self.assertEqual(settled[0]["tasks"], ["CODE-1"])
            self.assertEqual(run(sys.executable, str(GATE), "audit", "--repo", str(repo), "--last", "5").returncode, 0)

    def test_runner_blocks_three_commit_mutate_close_restore_loophole(self) -> None:
        gate = load_gate_module()
        doc_check = "python -c \"from pathlib import Path; assert Path('docs/guide.md').read_text().strip(); print('1 doc ok')\""
        weak_verify = "python -c \"print('weak ok')\""
        for case in ("code_to_doc_then_restore", "doc_verify_weaken_then_restore"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                repo = doc_repo(Path(td))
                qpath = repo / "TASK_QUEUE.md"
                if case == "code_to_doc_then_restore":
                    tid = "CODE-1"
                    lines = [ln for ln in qpath.read_text(encoding="utf-8").splitlines(keepends=True) if "DOC-1" not in ln]
                    qtext = "".join(lines).replace(
                        "<!-- task:CODE-1 files: src/loader.py -->",
                        "<!-- task:CODE-1 files: src/loader.py, docs/guide.md -->",
                    )
                else:
                    tid = "DOC-1"
                    lines = [ln for ln in qpath.read_text(encoding="utf-8").splitlines(keepends=True) if "CODE-1" not in ln]
                    qtext = "".join(lines)
                qpath.write_text(qtext, encoding="utf-8")
                run("git", "-C", str(repo), "add", "TASK_QUEUE.md")
                self.assertEqual(commit(repo, f"setup {case}").returncode, 0)
                base_sha = run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
                commit_shas: list[str] = []

                def fake_worker(repo_arg, cfg_arg, worker, prompt, pf, **kwargs):
                    qp = repo_arg / "TASK_QUEUE.md"
                    orig_q = qp.read_text(encoding="utf-8")
                    if case == "code_to_doc_then_restore":
                        q_c1 = (
                            orig_q.replace(
                                json.dumps("python -c \"print('1 passed')\""),
                                json.dumps(doc_check),
                            )
                            + "<!-- task:CODE-1 kind: document -->\n"
                        )
                        staged_paths = ["TASK_QUEUE.md", "docs/guide.md"]
                    else:
                        q_c1 = orig_q.replace(json.dumps(DOC_VERIFY_CMD), json.dumps(weak_verify))
                        staged_paths = ["TASK_QUEUE.md", "docs/a.md", "docs/b.md", "docs/c.md"]

                    # C1: mutate contract (non-DONE).
                    qp.write_text(q_c1, encoding="utf-8")
                    run("git", "-C", str(repo_arg), "add", "TASK_QUEUE.md")
                    c1 = commit(repo_arg, f"C1 {case}")
                    self.assertEqual(c1.returncode, 0, c1.stdout + c1.stderr)
                    commit_shas.append(run("git", "-C", str(repo_arg), "rev-parse", "HEAD").stdout.strip())

                    # Verify against mutated contract, then C2: close DONE.
                    if case == "code_to_doc_then_restore":
                        write_docs(repo_arg, {"docs/guide.md": "# Guide\n\n## Summary\nDocs only.\n"})
                    else:
                        write_docs(repo_arg, {"docs/a.md": "trivial\n", "docs/b.md": "trivial\n", "docs/c.md": "trivial\n"})
                    v = run(sys.executable, str(GATE), "verify", "--repo", str(repo_arg), "--task", tid)
                    self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
                    flip_row(repo_arg, tid, "DONE")
                    run("git", "-C", str(repo_arg), "add", *staged_paths)
                    c2 = commit(repo_arg, f"C2 {case}")
                    self.assertEqual(c2.returncode, 0, c2.stdout + c2.stderr)
                    commit_shas.append(run("git", "-C", str(repo_arg), "rev-parse", "HEAD").stdout.strip())

                    # C3: restore original contract metadata (keeping DONE) so HEAD matches admitted_qtext.
                    qp.write_text(orig_q, encoding="utf-8")
                    flip_row(repo_arg, tid, "DONE")
                    run("git", "-C", str(repo_arg), "add", "TASK_QUEUE.md")
                    c3 = commit(repo_arg, f"C3 {case} restore contract")
                    self.assertEqual(c3.returncode, 0, c3.stdout + c3.stderr)
                    commit_shas.append(run("git", "-C", str(repo_arg), "rev-parse", "HEAD").stdout.strip())
                    return gate.WorkerResult(0, "done"), None

                with mock.patch.object(gate, "resolve_tool", return_value="codex"), \
                     mock.patch.object(gate, "spawn_worker", side_effect=fake_worker):
                    with self.assertRaises(SystemExit) as ctx:
                        gate.cmd_run(SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=True, force=False))
                    self.assertEqual(ctx.exception.code, 3, case)

                events = list(gate.iter_journal(repo))
                self.assertFalse(
                    any(e.get("event") in ("task_done", "stage_acceptance", "full_acceptance", "package_settled") for e in events),
                    case,
                )
                proto = [e for e in events if e.get("event") == "worker_protocol_error"]
                self.assertEqual(len(proto), 1, case)
                self.assertEqual(proto[0]["task"], tid)
                self.assertIn("only host/planner before dispatch may reconcile contracts", proto[0]["reason"])
                # Preserve C1..C3 commits, queue, partial docs, and failed receipts.
                self.assertEqual(len(commit_shas), 3)
                self.assertEqual(run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip(), commit_shas[-1])
                self.assertEqual(run("git", "-C", str(repo), "rev-list", "--count", f"{base_sha}..HEAD").stdout.strip(), "3")
                self.assertTrue(any(e.get("event") == "attempt_start" for e in events))


if __name__ == "__main__":
    unittest.main()
