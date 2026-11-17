"""Offline deterministic acceptance tests for IL-16 exact remote publication verification."""
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
import uuid

GATE_DIR = Path(__file__).resolve().parent
if str(GATE_DIR) not in sys.path:
    sys.path.insert(0, str(GATE_DIR))

from test_compat import FLOW, committed_runner_repo, load_gate_module, run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PUBLICATION_PATH = ROOT / "flow" / "publication.py"
CONTINUITY_PATH = ROOT / "flow" / "continuity.py"


def load_publication():
    spec = importlib.util.spec_from_file_location("flow_publication_under_test", PUBLICATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def flow(*args: str) -> subprocess.CompletedProcess[str]:
    return run(sys.executable, str(FLOW), *args)


def sha256_bytes(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state_snapshot(repo: Path) -> dict[str, str | None]:
    """Capture acceptance, journal, config, queue, and context digests to prove non-mutation."""
    return {
        "verdict": sha256_bytes(repo / ".gate" / "verdict.json"),
        "journal": sha256_bytes(repo / ".gate" / "journal.ndjson"),
        "config": sha256_bytes(repo / ".gate" / "config.json"),
        "queue": sha256_bytes(repo / "TASK_QUEUE.md"),
        "context": sha256_bytes(repo / "CONTEXT.md"),
    }


def bare_refs_snapshot(bare: Path) -> str:
    res = run("git", "-C", str(bare), "for-each-ref", "--format=%(objectname) %(refname)")
    assert res.returncode == 0, res.stderr
    return res.stdout


class _OfflineSocketGuard:
    """Guarantee tests never open any real network socket."""

    def __enter__(self):
        self._p1 = mock.patch.object(
            socket,
            "socket",
            side_effect=AssertionError("live network socket forbidden in offline publication tests"),
        )
        self._p2 = mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=AssertionError("live urllib forbidden in offline publication tests"),
        )
        self._p1.start()
        self._p2.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._p2.stop()
        self._p1.stop()


class PublicationVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pub = load_publication()
        self.gate = load_gate_module()

    def _setup_repo_and_bare_remote(self, label: str = "case") -> tuple[Path, Path, str]:
        sub = self.root / label
        sub.mkdir(parents=True, exist_ok=True)
        repo = committed_runner_repo(sub, ["codex"])
        (repo / "CONTEXT.md").write_text(
            "# CONTEXT - fixture\n\n"
            "## 1. Goal & red lines\nNever push without explicit owner instruction.\n\n"
            "## 2. Now\nImplementing IL-16.\n\n"
            "## 3. Last stable checkpoint\nInitial accepted baseline.\n\n"
            "## 4. Next step\nVerify exact remote branch SHA.\n\n"
            "## 5. Waiting on a human\nNone.\n",
            encoding="utf-8",
        )
        bare = sub / "origin.git"
        res = run("git", "init", "--bare", "-q", str(bare))
        self.assertEqual(res.returncode, 0, res.stderr)
        res = run("git", "-C", str(repo), "remote", "add", "origin", str(bare))
        self.assertEqual(res.returncode, 0, res.stderr)
        head = run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
        return repo, bare, head

    def _seed_bare_ref(self, repo: Path, bare: Path, ref: str, commit_sha: str | None = None) -> str:
        sha = commit_sha or run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
        fetched = run("git", "-C", str(bare), "fetch", "--update-head-ok", "-q", str(repo), f"+{sha}:{ref}")
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        return sha

    def _write_full_delivery_verdict(self, repo: Path, head_sha: str | None = None) -> dict:
        cfg = self.gate.load_config(repo)
        head = head_sha or run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
        payload = {
            "result": "PASS",
            "exit_code": 0,
            "count": 12,
            "cmd": cfg["verify_cmd"],
            "scope": "queue",
            "stage": "delivery",
            "full_suite": True,
            "unverified": "",
            "task": None,
            "head": head,
            "candidate_fingerprint": self.gate.candidate_fingerprint(repo, cfg),
            "at": "2026-11-17T04:00:00Z",
            "at_epoch": 1794888000.0,
        }
        (repo / ".gate" / "verdict.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        (repo / ".gate" / "journal.ndjson").write_text(
            json.dumps({"at": "2026-11-17T04:00:00Z", "event": "full_acceptance", "result": "PASS", "head": head})
            + "\n",
            encoding="utf-8",
        )
        return payload

    def _write_task_verdict(self, repo: Path, task_id: str = "EAV-2", head_sha: str | None = None) -> dict:
        cfg = self.gate.load_config(repo)
        head = head_sha or run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
        payload = {
            "result": "PASS",
            "exit_code": 0,
            "count": 1,
            "cmd": "python -c \"print('1 passed')\"",
            "scope": "task",
            "task": task_id,
            "files": ["eav2.txt"],
            "fingerprint": self.gate.files_fingerprint(repo, cfg, ["eav2.txt"]),
            "candidate_fingerprint": "",
            "head": head,
            "at": "2026-11-17T04:00:00Z",
            "at_epoch": 1794888000.0,
        }
        (repo / ".gate" / "verdict.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _make_commit(self, repo: Path, filename: str, content: str, msg: str) -> str:
        (repo / filename).write_text(content, encoding="utf-8")
        self.assertEqual(run("git", "-C", str(repo), "add", filename).returncode, 0)
        self.assertEqual(run("git", "-C", str(repo), "commit", "-q", "-m", msg).returncode, 0)
        return run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()

    def test_bare_remote_exact_match_mismatch_advance_absent_and_tag_decoy_suffix(self) -> None:
        repo, bare, head1 = self._setup_repo_and_bare_remote("bare-core")
        self._write_full_delivery_verdict(repo, head1)
        snap_before = state_snapshot(repo)

        with _OfflineSocketGuard():
            # 1. Absent ref on empty bare remote -> exit 4, status 'absent'.
            absent_res = flow(
                "publish-verify",
                "--repo",
                str(repo),
                "--remote",
                "origin",
                "--branch",
                "maintenance/independent-learning-20261116",
                "--expect",
                head1,
                "--json",
            )
            self.assertEqual(absent_res.returncode, 4, absent_res.stderr)
            absent_json = json.loads(absent_res.stdout)
            self.assertEqual(absent_json["status"], "absent")
            self.assertIsNone(absent_json["observed_sha"])
            self.assertTrue(absent_json["receipt_written"])
            self.assertEqual(state_snapshot(repo), snap_before)

            # 2. Seed tag with exact branch name AND decoy suffix/prefix branches (no exact refs/heads/<branch>).
            branch = "release-v1"
            self._seed_bare_ref(repo, bare, f"refs/tags/{branch}", head1)
            self._seed_bare_ref(repo, bare, f"refs/heads/{branch}-decoy", head1)
            self._seed_bare_ref(repo, bare, f"refs/heads/decoy/{branch}", head1)
            refs_before = bare_refs_snapshot(bare)

            decoy_res = flow(
                "publish-verify",
                "--repo",
                str(repo),
                "--remote",
                "origin",
                "--branch",
                branch,
                "--expect",
                head1,
                "--json",
            )
            self.assertEqual(decoy_res.returncode, 4, decoy_res.stderr)
            decoy_json = json.loads(decoy_res.stdout)
            self.assertEqual(decoy_json["status"], "absent")
            self.assertIsNone(decoy_json["observed_sha"])
            self.assertEqual(bare_refs_snapshot(bare), refs_before, "publish-verify must never mutate remote refs")
            self.assertEqual(state_snapshot(repo), snap_before)

            # 3. Seed exact refs/heads/release-v1 -> exact match (exit 0).
            self._seed_bare_ref(repo, bare, f"refs/heads/{branch}", head1)
            match_res = flow(
                "publish-verify",
                "--repo",
                str(repo),
                "--remote",
                "origin",
                "--branch",
                branch,
                "--expect",
                head1.upper(),  # uppercase hex input normalized cleanly
                "--json",
            )
            self.assertEqual(match_res.returncode, 0, match_res.stderr)
            match_json = json.loads(match_res.stdout)
            self.assertEqual(match_json["status"], "match")
            self.assertEqual(match_json["observed_sha"], head1)
            self.assertEqual(match_json["expected_sha"], head1)
            self.assertEqual(match_json["head_sha"], head1)
            self.assertIsNotNone(uuid.UUID(match_json["id"]))
            self.assertTrue(match_json["at"].endswith("Z"))
            self.assertTrue(match_json["acceptance"]["fresh_delivery"])
            self.assertIn("never grant push", match_json["authority_note"])

            # Human CLI output also works cleanly without --json.
            human_ok = flow(
                "publish-verify",
                "--repo",
                str(repo),
                "--remote",
                "origin",
                "--branch",
                branch,
                "--expect",
                head1,
            )
            self.assertEqual(human_ok.returncode, 0, human_ok.stderr)
            self.assertIn("MATCH (exit 0)", human_ok.stdout)
            self.assertIn("fresh delivery PASS", human_ok.stdout)

            # 4. Advance remote branch in a separate clone while expecting head1 -> mismatch (exit 3).
            other = self.root / "advancer"
            self.assertEqual(run("git", "clone", "-q", str(repo), str(other)).returncode, 0)
            run("git", "-C", str(other), "config", "user.name", "Other")
            run("git", "-C", str(other), "config", "user.email", "other@example.invalid")
            head2 = self._make_commit(other, "advance.txt", "remote moved ahead\n", "remote advance")
            self._seed_bare_ref(other, bare, f"refs/heads/{branch}", head2)

            mismatch_res = flow(
                "publish-verify",
                "--repo",
                str(repo),
                "--remote",
                "origin",
                "--branch",
                branch,
                "--expect",
                head1,
                "--json",
            )
            self.assertEqual(mismatch_res.returncode, 3, mismatch_res.stderr)
            mismatch_json = json.loads(mismatch_res.stdout)
            self.assertEqual(mismatch_json["status"], "mismatch")
            self.assertEqual(mismatch_json["observed_sha"], head2)
            self.assertEqual(mismatch_json["expected_sha"], head1)
            self.assertEqual(state_snapshot(repo), snap_before, "prior acceptance/journal untouched on mismatch")

            # 5. Inaccessible configured remote target -> unavailable (exit 5), never match or invalid.
            missing_bare = self.root / "missing-remote.git"
            self.assertEqual(
                run("git", "-C", str(repo), "remote", "add", "dead", str(missing_bare)).returncode,
                0,
            )
            inacc_res = flow(
                "publish-verify",
                "--repo",
                str(repo),
                "--remote",
                "dead",
                "--branch",
                branch,
                "--expect",
                head1,
                "--json",
            )
            self.assertEqual(inacc_res.returncode, 5, inacc_res.stderr)
            inacc_json = json.loads(inacc_res.stdout)
            self.assertEqual(inacc_json["status"], "unavailable")
            self.assertEqual(inacc_json["error_code"], "remote_inaccessible")
            self.assertEqual(state_snapshot(repo), snap_before)

    def test_fake_runner_malformed_duplicate_decoy_sha64_timeout_auth_and_no_prompts_retry_push(self) -> None:
        repo, _, head1 = self._setup_repo_and_bare_remote("fake-runner")
        # Reconfigure origin to a normal HTTPS URL; fake runner intercepts transport offline.
        self.assertEqual(
            run("git", "-C", str(repo), "remote", "set-url", "origin", "https://example.invalid/org/flow.git").returncode,
            0,
        )
        self._write_full_delivery_verdict(repo, head1)
        snap_before = state_snapshot(repo)

        calls: list[tuple[list[str], Path, dict[str, str], float]] = []

        def run_with_response(response_or_exc, *, branch: str = "main", expect: str = head1, timeout_s: float = 7.5) -> dict:
            calls.clear()

            def fake_runner(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float):
                calls.append((list(cmd), cwd, dict(env), timeout))
                if isinstance(response_or_exc, BaseException):
                    raise response_or_exc
                return response_or_exc

            with _OfflineSocketGuard():
                return self.pub.verify_publication(
                    repo,
                    remote="origin",
                    branch=branch,
                    expect=expect,
                    timeout_s=timeout_s,
                    transport_runner=fake_runner,
                )

        # Verify argv-only, no prompts, no retry, no push, exact timeout forwarding, and SHA-64 support.
        sha64 = "a" * 64
        res_64 = run_with_response(
            subprocess.CompletedProcess(["git"], 0, f"{sha64}\trefs/heads/main\n", ""),
            expect=sha64,
            timeout_s=9.0,
        )
        self.assertEqual(res_64["status"], "match")
        self.assertEqual(res_64["exit_code"], 0)
        self.assertEqual(res_64["observed_sha"], sha64)
        self.assertEqual(len(calls), 1, "must invoke transport exactly once")
        cmd_argv, cwd_seen, env_seen, timeout_seen = calls[0]
        self.assertIsInstance(cmd_argv, list)
        self.assertEqual(cwd_seen, repo.resolve())
        self.assertEqual(timeout_seen, 9.0)
        self.assertIn("ls-remote", cmd_argv)
        self.assertNotIn("push", cmd_argv)
        self.assertNotIn("fetch", cmd_argv)
        self.assertIn("protocol.ext.allow=never", cmd_argv)
        self.assertIn("credential.interactive=never", cmd_argv)
        self.assertIn("core.askPass=", cmd_argv)
        self.assertEqual(cmd_argv[-3:], ["--", "origin", "refs/heads/main"])
        self.assertEqual(env_seen.get("GIT_TERMINAL_PROMPT"), "0")
        self.assertEqual(env_seen.get("GCM_INTERACTIVE"), "never")
        self.assertEqual(env_seen.get("GIT_ASKPASS"), "")
        self.assertEqual(env_seen.get("SSH_ASKPASS"), "")
        self.assertEqual(env_seen.get("SSH_ASKPASS_REQUIRE"), "never")

        # Decoy refs in multi-line output without exact refs/heads/main -> absent (exit 4).
        decoy_stdout = (
            f"{head1}\trefs/tags/main\n"
            f"{head1}\trefs/heads/main-decoy\n"
            f"{head1}\trefs/heads/prefix/main\n"
        )
        res_decoy = run_with_response(subprocess.CompletedProcess(["git"], 0, decoy_stdout, ""))
        self.assertEqual(res_decoy["status"], "absent")
        self.assertEqual(res_decoy["exit_code"], 4)
        self.assertEqual(len(calls), 1)

        # Duplicate exact refs/heads/main rows (identical SHA and conflicting SHA) -> unavailable (exit 5).
        for dup_stdout in (
            f"{head1}\trefs/heads/main\n{head1}\trefs/heads/main\n",
            f"{head1}\trefs/heads/main\n{'b' * 40}\trefs/heads/main\n",
            f"{head1}\trefs/heads/main\n{head1}\trefs/heads/main^{{}}\n",
        ):
            res_dup = run_with_response(subprocess.CompletedProcess(["git"], 0, dup_stdout, ""))
            self.assertEqual(res_dup["status"], "unavailable")
            self.assertEqual(res_dup["exit_code"], 5)
            self.assertIn(res_dup["error_code"], ("duplicate_ref", "ambiguous_ref"))
            self.assertIsNone(res_dup["observed_sha"])
            self.assertEqual(len(calls), 1, "no retry on ambiguous response")

        # Malformed responses -> unavailable (exit 5), never match or absent.
        for bad_stdout in (
            f"{head1} refs/heads/main\n",  # space instead of tab
            "shortsha\trefs/heads/main\n",
            f"warning: extra line\n{head1}\trefs/heads/main\n",
            f"{head1}\trefs/heads/other\n\n{head1}\trefs/heads/main\n",
            f"{'0' * 40}\trefs/heads/main\n",
            f"{head1}\trefs/heads/main\x00extra\n",
        ):
            res_bad = run_with_response(subprocess.CompletedProcess(["git"], 0, bad_stdout, ""))
            self.assertEqual(res_bad["status"], "unavailable")
            self.assertEqual(res_bad["exit_code"], 5)
            self.assertEqual(res_bad["error_code"], "malformed_output")
            self.assertIsNone(res_bad["observed_sha"])
            self.assertEqual(len(calls), 1, "no retry on malformed response")

        # Timeout -> unavailable (exit 5), 1 call only, no retry.
        res_to = run_with_response(subprocess.TimeoutExpired(["git", "ls-remote"], 7.5))
        self.assertEqual(res_to["status"], "unavailable")
        self.assertEqual(res_to["exit_code"], 5)
        self.assertEqual(res_to["error_code"], "timeout")
        self.assertEqual(len(calls), 1, "timeout must never retry")

        # Auth unavailable with sensitive stderr -> unavailable (exit 5), 1 call, stderr secret never persisted/emitted.
        stderr_secret = "STDERR_BEARER_SECRET_99182"
        res_auth = run_with_response(
            subprocess.CompletedProcess(
                ["git"],
                128,
                "",
                f"fatal: Authentication failed for 'https://oauth2:{stderr_secret}@example.invalid/org/flow.git': terminal prompts disabled\n",
            )
        )
        self.assertEqual(res_auth["status"], "unavailable")
        self.assertEqual(res_auth["exit_code"], 5)
        self.assertEqual(res_auth["error_code"], "auth_unavailable")
        self.assertEqual(len(calls), 1, "auth failure must never retry")
        serialized_res = json.dumps(res_auth)
        receipts_raw = (repo / ".gate" / "publication.ndjson").read_text(encoding="utf-8")
        self.assertNotIn(stderr_secret, serialized_res)
        self.assertNotIn(stderr_secret, receipts_raw)
        self.assertEqual(state_snapshot(repo), snap_before, "prior acceptance/journal untouched across all failures")

    def test_unsafe_configured_transport_credentials_and_invalid_options_rejected_without_secret_persistence(self) -> None:
        repo, _, head1 = self._setup_repo_and_bare_remote("unsafe-config")
        self._write_full_delivery_verdict(repo, head1)
        snap_before = state_snapshot(repo)

        secrets = [
            "SECRET_EXT_PAYLOAD_101",
            "SECRET_HTTP_BASIC_202",
            "SECRET_TOKEN_USER_303",
            "SECRET_QUERY_PARAM_404",
            "SECRET_SSH_PASS_505",
            "SECRET_SCP_PASS_606",
            "SECRET_CLI_REMOTE_707",
            "SECRET_CLI_BRANCH_808",
            "SECRET_INSTEADOF_909",
        ]

        transport_calls = 0

        def forbidden_runner(cmd, cwd, env, timeout):
            nonlocal transport_calls
            transport_calls += 1
            raise AssertionError(f"transport must never run on invalid/unsafe config: {cmd}")

        unsafe_urls = [
            f"ext::sh -c 'echo {secrets[0]}'",
            "fd::3",
            f"http::https://alice:{secrets[1]}@example.invalid/org/repo.git",
            f"https://alice:{secrets[1]}@example.invalid/org/repo.git",
            f"https://{secrets[2]}@example.invalid/org/repo.git",
            f"http://alice:{secrets[1]}@example.invalid/org/repo.git",
            "http://example.invalid/org/repo.git",
            "git://example.invalid/org/repo.git",
            f"https://example.invalid/org/repo.git?access_token={secrets[3]}",
            f"ssh://alice:{secrets[4]}@example.invalid/org/repo.git",
            "ssh://-oProxyCommand=evil/org/repo.git",
            "ssh://-oProxyCommand=evil@example.invalid/org/repo.git",
            "-oProxyCommand=evil:org/repo.git",
            f"alice:{secrets[5]}@example.invalid:org/repo.git",
            "-upload-pack=/bin/sh",
        ]

        with _OfflineSocketGuard():
            for bad_url in unsafe_urls:
                self.assertEqual(
                    run("git", "-C", str(repo), "remote", "set-url", "--", "origin", bad_url).returncode,
                    0,
                )
                out = self.pub.verify_publication(
                    repo,
                    remote="origin",
                    branch="main",
                    expect=head1,
                    transport_runner=forbidden_runner,
                )
                self.assertEqual(out["status"], "invalid", bad_url)
                self.assertEqual(out["exit_code"], 2, bad_url)
                self.assertIsNone(out["target"]["remote_url_hash"], bad_url)

            # Test url.<base>.insteadOf rewriting a safe URL into a credentialed URL.
            run("git", "-C", str(repo), "remote", "set-url", "origin", "https://safe.example.invalid/org/repo.git")
            run(
                "git",
                "-C",
                str(repo),
                "config",
                f"url.https://user:{secrets[8]}@evil.example.invalid/.insteadOf",
                "https://safe.example.invalid/",
            )
            out_instead = self.pub.verify_publication(
                repo,
                remote="origin",
                branch="main",
                expect=head1,
                transport_runner=forbidden_runner,
            )
            self.assertEqual(out_instead["status"], "invalid")
            self.assertEqual(out_instead["error_code"], "embedded_credentials")
            run(
                "git",
                "-C",
                str(repo),
                "config",
                "--unset",
                f"url.https://user:{secrets[8]}@evil.example.invalid/.insteadOf",
            )

            # Test dangerous remote.<name>.uploadpack / remote.<name>.vcs / local core.sshCommand overrides.
            for cfg_key, cfg_val in (
                ("remote.origin.uploadpack", "/tmp/evil-upload-pack"),
                ("remote.origin.vcs", "custom-helper"),
                ("core.sshCommand", "ssh -oProxyCommand=evil"),
            ):
                run("git", "-C", str(repo), "config", cfg_key, cfg_val)
                out_cfg = self.pub.verify_publication(
                    repo,
                    remote="origin",
                    branch="main",
                    expect=head1,
                    transport_runner=forbidden_runner,
                )
                self.assertEqual(out_cfg["status"], "invalid", cfg_key)
                self.assertEqual(out_cfg["exit_code"], 2, cfg_key)
                run("git", "-C", str(repo), "config", "--unset", cfg_key)

            # Test CLI invalid options, URL/path --remote, glob/option --branch, bad --expect, and --push.
            cli_cases = [
                ["publish-verify", "--repo", str(repo), "--remote", f"https://u:{secrets[6]}@example.invalid/r.git", "--branch", "main", "--expect", head1, "--json"],
                ["publish-verify", "--repo", str(repo), "--remote", "/tmp/local.git", "--branch", "main", "--expect", head1, "--json"],
                ["publish-verify", "--repo", str(repo), "--remote", "-oProxyCommand=evil", "--branch", "main", "--expect", head1, "--json"],
                ["publish-verify", "--repo", str(repo), "--remote", "unconfigured", "--branch", "main", "--expect", head1, "--json"],
                ["publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", f"-oSecret={secrets[7]}", "--expect", head1, "--json"],
                ["publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "main*", "--expect", head1, "--json"],
                ["publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "refs/heads/main", "--expect", head1, "--json"],
                ["publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "HEAD", "--expect", head1, "--json"],
                ["publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "a..b", "--expect", head1, "--json"],
                ["publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "main", "--expect", head1[:12], "--json"],
                ["publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "main", "--expect", "0" * 40, "--json"],
                ["publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "main", "--expect", head1, "--push", "--json"],
            ]
            for argv in cli_cases:
                cli_res = flow(*argv)
                self.assertEqual(cli_res.returncode, 2, f"{argv}: {cli_res.stdout} {cli_res.stderr}")
                parsed_cli = json.loads(cli_res.stdout)
                self.assertEqual(parsed_cli["status"], "invalid")
                for sec in secrets:
                    self.assertNotIn(sec, cli_res.stdout)
                    self.assertNotIn(sec, cli_res.stderr)

        self.assertEqual(transport_calls, 0, "transport must never execute for rejected URLs/options")
        persisted = (repo / ".gate" / "publication.ndjson").read_text(encoding="utf-8")
        for sec in secrets:
            self.assertNotIn(sec, persisted, f"secret {sec} must never persist in publication.ndjson")
        self.assertEqual(state_snapshot(repo), snap_before)

        # Confirm supported normal SSH and SCP URL forms validate cleanly.
        for valid_url, expected_kind in (
            ("https://github.com/org/repo.git", "https"),
            ("ssh://git@github.com:2222/org/repo.git", "ssh"),
            ("git@github.com:org/repo.git", "scp"),
        ):
            info, err_code, err_reason = self.pub.validate_and_sanitize_remote_url(valid_url, repo)
            self.assertIsNone(err_code, err_reason)
            assert info is not None
            self.assertEqual(info["kind"], expected_kind)

    def test_append_history_integrity_and_symlink_refusal(self) -> None:
        repo, bare, head1 = self._setup_repo_and_bare_remote("append-and-symlink")
        self._write_full_delivery_verdict(repo, head1)
        self._seed_bare_ref(repo, bare, "refs/heads/main", head1)

        pub_file = repo / ".gate" / "publication.ndjson"
        with _OfflineSocketGuard():
            # Step 1: exact match
            r1 = flow("publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "main", "--expect", head1, "--json")
            self.assertEqual(r1.returncode, 0)
            bytes_1 = pub_file.read_bytes()
            lines_1 = bytes_1.splitlines()
            self.assertEqual(len(lines_1), 1)

            # Step 2: absent branch -> appends strictly after bytes_1
            r2 = flow("publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "missing", "--expect", head1, "--json")
            self.assertEqual(r2.returncode, 4)
            bytes_2 = pub_file.read_bytes()
            self.assertTrue(bytes_2.startswith(bytes_1), "existing receipt bytes must remain untouched prefix")
            lines_2 = bytes_2.splitlines()
            self.assertEqual(len(lines_2), 2)

            # Step 3: invalid expect -> appends strictly after bytes_2
            r3 = flow("publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "main", "--expect", "bad", "--json")
            self.assertEqual(r3.returncode, 2)
            bytes_3 = pub_file.read_bytes()
            self.assertTrue(bytes_3.startswith(bytes_2), "existing receipt bytes must remain untouched prefix")
            lines_3 = bytes_3.splitlines()
            self.assertEqual(len(lines_3), 3)

            ids = [json.loads(ln.decode("utf-8"))["id"] for ln in lines_3]
            self.assertEqual(len(set(ids)), 3)

            # Symlink refusal 1: publication.ndjson symlinked to existing outside file.
            outside_dir = self.root / "outside-secret-dir"
            outside_dir.mkdir()
            outside_target = outside_dir / "target.ndjson"
            outside_target.write_text("DO-NOT-TOUCH\n", encoding="utf-8")
            pub_file.unlink()
            os.symlink(outside_target, pub_file)

            sym_res = flow("publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "main", "--expect", head1, "--json")
            self.assertEqual(sym_res.returncode, 2)
            sym_json = json.loads(sym_res.stdout)
            self.assertEqual(sym_json["status"], "invalid")
            self.assertEqual(sym_json["error_code"], "unsafe_storage")
            self.assertFalse(sym_json["receipt_written"])
            self.assertEqual(outside_target.read_text(encoding="utf-8"), "DO-NOT-TOUCH\n")

            # Resume must ALSO refuse reading the symlinked publication.ndjson.
            resume_sym = flow("resume", "--repo", str(repo), "--json")
            self.assertEqual(resume_sym.returncode, 0)
            resume_sym_json = json.loads(resume_sym.stdout)
            self.assertTrue(resume_sym_json["publication"]["integrity"]["unsafe_storage"])
            self.assertNotIn("DO-NOT-TOUCH", resume_sym.stdout)

            # Symlink refusal 2: dangling symlink to a non-existent outside path.
            pub_file.unlink()
            dangling_target = outside_dir / "never-create.ndjson"
            os.symlink(dangling_target, pub_file)
            dang_res = flow("publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "main", "--expect", head1, "--json")
            self.assertEqual(dang_res.returncode, 2)
            self.assertFalse(dangling_target.exists(), "dangling symlink target must never be created")
            pub_file.unlink()

            # Symlink refusal 3: in-repo symlink pointing to .gate/verdict.json.
            verdict_before = (repo / ".gate" / "verdict.json").read_bytes()
            os.symlink(repo / ".gate" / "verdict.json", pub_file)
            inrepo_res = flow("publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "main", "--expect", head1, "--json")
            self.assertEqual(inrepo_res.returncode, 2)
            self.assertEqual((repo / ".gate" / "verdict.json").read_bytes(), verdict_before)
            pub_file.unlink()

            # Symlink refusal 4: .gate directory itself replaced with a symlink.
            gate_backup = self.root / "gate-backup"
            (repo / ".gate").rename(gate_backup)
            os.symlink(outside_dir, repo / ".gate", target_is_directory=True)
            gatedir_res = flow("publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "main", "--expect", head1, "--json")
            self.assertEqual(gatedir_res.returncode, 2)
            self.assertFalse((outside_dir / "publication.ndjson").exists())
            (repo / ".gate").unlink()
            gate_backup.rename(repo / ".gate")

    def test_resume_separates_observed_remote_from_local_full_vs_task_acceptance_and_detects_drift(self) -> None:
        repo, bare, head1 = self._setup_repo_and_bare_remote("resume-drift")
        self._seed_bare_ref(repo, bare, "refs/heads/main", head1)

        with _OfflineSocketGuard():
            # 1. Task-scoped PASS only (not full delivery acceptance) + remote exact match.
            self._write_task_verdict(repo, "EAV-2", head1)
            pv_task = json.loads(
                flow("publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "main", "--expect", head1, "--json").stdout
            )
            self.assertEqual(pv_task["status"], "match")
            self.assertFalse(pv_task["acceptance"]["covers_delivery"])
            self.assertFalse(pv_task["acceptance"]["fresh_delivery"])
            self.assertTrue(any("task-scoped" in msg for msg in pv_task["acceptance"]["issues"]))

            res_task_json = json.loads(flow("resume", "--repo", str(repo), "--json").stdout)
            latest_task = res_task_json["publication"]["latest"]
            self.assertEqual(latest_task["observed"]["status"], "match")
            self.assertFalse(latest_task["stale"])
            self.assertFalse(latest_task["local_acceptance"]["covers_delivery"])
            self.assertFalse(latest_task["local_acceptance"]["fresh_delivery"])
            self.assertFalse(latest_task["current_delivery_verified"])
            res_task_txt = flow("resume", "--repo", str(repo)).stdout
            self.assertIn("observed match", res_task_txt)
            self.assertIn("task-scoped acceptance", res_task_txt)
            self.assertIn("current delivery: NOT verified", res_task_txt)

            # 2. Partial stage PASS (module stage, full_suite=False) also does NOT satisfy delivery coverage.
            cfg = self.gate.load_config(repo)
            (repo / ".gate" / "verdict.json").write_text(
                json.dumps({
                    "result": "PASS",
                    "scope": "stage",
                    "stage": "module",
                    "full_suite": False,
                    "unverified": "unrelated modules; full delivery acceptance not established",
                    "head": head1,
                    "candidate_fingerprint": self.gate.candidate_fingerprint(repo, cfg),
                }),
                encoding="utf-8",
            )
            res_stage_json = json.loads(flow("resume", "--repo", str(repo), "--json").stdout)
            self.assertFalse(res_stage_json["publication"]["latest"]["local_acceptance"]["covers_delivery"])
            self.assertFalse(res_stage_json["publication"]["latest"]["current_delivery_verified"])

            # 3. Full delivery PASS at head1 -> current_delivery_verified becomes True (still no push authority).
            self._write_full_delivery_verdict(repo, head1)
            pv_full = json.loads(
                flow("publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "main", "--expect", head1, "--json").stdout
            )
            self.assertEqual(pv_full["status"], "match")
            self.assertTrue(pv_full["acceptance"]["fresh_delivery"])

            res_ok = json.loads(flow("resume", "--repo", str(repo), "--json").stdout)
            self.assertTrue(res_ok["publication"]["latest"]["current_delivery_verified"])
            self.assertFalse(res_ok["publication"]["latest"]["stale"])
            res_ok_txt = flow("resume", "--repo", str(repo)).stdout
            self.assertIn("verified at receipt time (observed only; no push authority)", res_ok_txt)

            # 4. Dirty candidate in working tree -> never implies current delivery.
            (repo / "eav2.txt").write_text("dirty edit\n", encoding="utf-8")
            res_dirty = json.loads(flow("resume", "--repo", str(repo), "--json").stdout)
            self.assertTrue(res_dirty["publication"]["latest"]["stale"])
            self.assertTrue(res_dirty["publication"]["latest"]["dirty_candidate"])
            self.assertFalse(res_dirty["publication"]["latest"]["current_delivery_verified"])
            (repo / "eav2.txt").unlink()

            # 5. Remote-target drift: changing origin URL invalidates reuse of old success.
            bare2 = self.root / "other-origin.git"
            self.assertEqual(run("git", "init", "--bare", "-q", str(bare2)).returncode, 0)
            self.assertEqual(run("git", "-C", str(repo), "remote", "set-url", "origin", str(bare2)).returncode, 0)
            res_rem_drift = json.loads(flow("resume", "--repo", str(repo), "--json").stdout)
            self.assertTrue(res_rem_drift["publication"]["latest"]["stale"])
            self.assertTrue(res_rem_drift["publication"]["latest"]["remote_drift"])
            self.assertFalse(res_rem_drift["publication"]["latest"]["current_delivery_verified"])
            self.assertIn("configured URL for remote 'origin' changed", flow("resume", "--repo", str(repo)).stdout)

            # Restore original bare remote URL -> back to verified head1.
            self.assertEqual(run("git", "-C", str(repo), "remote", "set-url", "origin", str(bare)).returncode, 0)
            self.assertTrue(json.loads(flow("resume", "--repo", str(repo), "--json").stdout)["publication"]["latest"]["current_delivery_verified"])

            # 6. Latest target attempt wins: remote advances to another commit, new publish-verify records mismatch.
            other = self.root / "remote-mover"
            self.assertEqual(run("git", "clone", "-q", str(repo), str(other)).returncode, 0)
            run("git", "-C", str(other), "config", "user.name", "Mover")
            run("git", "-C", str(other), "config", "user.email", "mover@example.invalid")
            remote_head = self._make_commit(other, "moved.txt", "ahead\n", "ahead")
            self._seed_bare_ref(other, bare, "refs/heads/main", remote_head)

            pv_mis = flow("publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "main", "--expect", head1, "--json")
            self.assertEqual(pv_mis.returncode, 3)
            res_latest_wins = json.loads(flow("resume", "--repo", str(repo), "--json").stdout)
            self.assertEqual(len(res_latest_wins["publication"]["targets"]), 1)
            self.assertEqual(res_latest_wins["publication"]["targets"][0]["observed"]["status"], "mismatch")
            self.assertFalse(res_latest_wins["publication"]["targets"][0]["current_delivery_verified"])

            # Re-align remote to head1 and re-verify -> match again, then advance local HEAD -> HEAD drift!
            self._seed_bare_ref(repo, bare, "refs/heads/main", head1)
            self.assertEqual(
                flow("publish-verify", "--repo", str(repo), "--remote", "origin", "--branch", "main", "--expect", head1, "--json").returncode,
                0,
            )
            head2 = self._make_commit(repo, "eav2.txt", "new local commit\n", "local advance")
            self._write_full_delivery_verdict(repo, head2)
            res_head_drift = json.loads(flow("resume", "--repo", str(repo), "--json").stdout)
            self.assertTrue(res_head_drift["publication"]["latest"]["stale"])
            self.assertTrue(res_head_drift["publication"]["latest"]["head_drift"])
            self.assertFalse(res_head_drift["publication"]["latest"]["current_delivery_verified"])
            self.assertIn("HEAD moved", flow("resume", "--repo", str(repo)).stdout)

    def test_bug_533704c36aa2_push_endpoint_binding_disclosure_drift_and_legacy_receipts(self) -> None:
        repo, bare_a, head1 = self._setup_repo_and_bare_remote("bug-533704c36aa2")
        bare_b = self.root / "bug-533704c36aa2" / "other-push.git"
        bare_c = self.root / "bug-533704c36aa2" / "third-push.git"
        self.assertEqual(run("git", "init", "--bare", "-q", str(bare_b)).returncode, 0)
        self.assertEqual(run("git", "init", "--bare", "-q", str(bare_c)).returncode, 0)

        branch = "check"
        self._seed_bare_ref(repo, bare_a, f"refs/heads/{branch}", head1)
        self._write_full_delivery_verdict(repo, head1)
        snap_before = state_snapshot(repo)
        refs_a_before = bare_refs_snapshot(bare_a)

        hash_a, err_a, _ = self.pub.validate_and_sanitize_remote_url(str(bare_a), repo)
        hash_b, err_b, _ = self.pub.validate_and_sanitize_remote_url(str(bare_b), repo)
        hash_c, err_c, _ = self.pub.validate_and_sanitize_remote_url(str(bare_c), repo)
        assert err_a is None and hash_a is not None
        assert err_b is None and hash_b is not None
        assert err_c is None and hash_c is not None

        with _OfflineSocketGuard():
            # 1. Ordinary single-URL remote (fetch == push == A): verified normally.
            pv_same = json.loads(
                flow(
                    "publish-verify",
                    "--repo",
                    str(repo),
                    "--remote",
                    "origin",
                    "--branch",
                    branch,
                    "--expect",
                    head1,
                    "--json",
                ).stdout
            )
            self.assertEqual(pv_same["status"], "match")
            res_same = json.loads(flow("resume", "--repo", str(repo), "--json").stdout)
            self.assertTrue(res_same["publication"]["latest"]["current_delivery_verified"])

            # 2. pushurl change AFTER a verified single-URL receipt invalidates current_delivery_verified via remote_drift.
            self.assertEqual(
                run("git", "-C", str(repo), "remote", "set-url", "--push", "origin", str(bare_b)).returncode,
                0,
            )
            res_push_drift = json.loads(flow("resume", "--repo", str(repo), "--json").stdout)
            self.assertFalse(res_push_drift["publication"]["latest"]["current_delivery_verified"])
            self.assertTrue(res_push_drift["publication"]["latest"]["remote_drift"])
            self.assertTrue(res_push_drift["publication"]["latest"]["stale"])
            self.assertIn("push URL", flow("resume", "--repo", str(repo)).stdout)

            # Verify single-URL receipt target fields recorded in step 1 as well.
            self.assertEqual(pv_same["target"]["observed_endpoint"], "fetch")
            self.assertEqual(pv_same["target"]["remote_url_hash"], hash_a["url_hash"])
            self.assertEqual(pv_same["target"]["push_binding"], "same")
            self.assertFalse(pv_same["target"]["push_differs"])
            self.assertEqual(pv_same["target"]["push_url_hashes"], [hash_a["url_hash"]])

            # 3. Host-reproduced failure: fetch=A (branch present), pushurl=B (empty), fresh delivery verdict.
            # Fetch observation remains 'match', but push differs -> clearly disclosed and NEVER current-delivery-verified.
            pv_diff_cli = flow(
                "publish-verify",
                "--repo",
                str(repo),
                "--remote",
                "origin",
                "--branch",
                branch,
                "--expect",
                head1,
                "--json",
            )
            self.assertEqual(pv_diff_cli.returncode, 0, pv_diff_cli.stderr)
            pv_diff = json.loads(pv_diff_cli.stdout)
            self.assertEqual(pv_diff["status"], "match")
            self.assertEqual(pv_diff["observed_sha"], head1)
            self.assertEqual(pv_diff["target"]["observed_endpoint"], "fetch")
            self.assertEqual(pv_diff["target"]["remote_url_hash"], hash_a["url_hash"])
            self.assertEqual(pv_diff["target"]["push_binding"], "different")
            self.assertTrue(pv_diff["target"]["push_differs"])
            self.assertEqual(pv_diff["target"]["push_url_hashes"], [hash_b["url_hash"]])
            self.assertIn("observed fetch endpoint only", pv_diff["reason"])
            self.assertIn(hash_b["url_hash"][:12], pv_diff["reason"])

            human_diff = flow(
                "publish-verify",
                "--repo",
                str(repo),
                "--remote",
                "origin",
                "--branch",
                branch,
                "--expect",
                head1,
            )
            self.assertEqual(human_diff.returncode, 0)
            self.assertIn("observed=fetch", human_diff.stdout)
            self.assertIn("NOT verified", human_diff.stdout)

            res_diff_json = json.loads(flow("resume", "--repo", str(repo), "--json").stdout)
            latest_diff = res_diff_json["publication"]["latest"]
            self.assertEqual(latest_diff["observed"]["status"], "match")
            self.assertTrue(latest_diff["local_acceptance"]["fresh_delivery"])
            self.assertEqual(latest_diff["target"]["push_binding"], "different")
            self.assertFalse(latest_diff["current_delivery_verified"])
            res_diff_txt = flow("resume", "--repo", str(repo)).stdout
            self.assertIn("push=different", res_diff_txt)
            self.assertIn("current delivery: NOT verified", res_diff_txt)
            self.assertNotIn("verified at receipt time", res_diff_txt)
            self.assertEqual(bare_refs_snapshot(bare_b), "", "push target B must never be pushed or mutated")
            self.assertEqual(bare_refs_snapshot(bare_a), refs_a_before)
            self.assertEqual(state_snapshot(repo)["verdict"], snap_before["verdict"])
            self.assertEqual(state_snapshot(repo)["journal"], snap_before["journal"])

            # 4. Multiple effective push endpoints (pushurl=B and pushurl=C): disclosed and never current-delivery-verified.
            self.assertEqual(
                run("git", "-C", str(repo), "remote", "set-url", "--add", "--push", "origin", str(bare_c)).returncode,
                0,
            )
            pv_multi = json.loads(
                flow(
                    "publish-verify",
                    "--repo",
                    str(repo),
                    "--remote",
                    "origin",
                    "--branch",
                    branch,
                    "--expect",
                    head1,
                    "--json",
                ).stdout
            )
            self.assertEqual(pv_multi["status"], "match")
            self.assertEqual(pv_multi["target"]["push_binding"], "multiple")
            self.assertTrue(pv_multi["target"]["push_differs"])
            self.assertEqual(pv_multi["target"]["push_url_hashes"], [hash_b["url_hash"], hash_c["url_hash"]])
            res_multi_json = json.loads(flow("resume", "--repo", str(repo), "--json").stdout)
            self.assertFalse(res_multi_json["publication"]["latest"]["current_delivery_verified"])
            res_multi_txt = flow("resume", "--repo", str(repo)).stdout
            self.assertIn("push=multiple", res_multi_txt)
            self.assertIn("current delivery: NOT verified", res_multi_txt)

            # 5. url.<base>.pushInsteadOf expansion without remote.origin.pushurl.
            self.assertEqual(
                run("git", "-C", str(repo), "config", "--unset-all", "remote.origin.pushurl").returncode,
                0,
            )
            self.assertEqual(
                run(
                    "git",
                    "-C",
                    str(repo),
                    "config",
                    "--local",
                    f"url.{bare_b.as_posix()}.pushInsteadOf",
                    bare_a.as_posix(),
                ).returncode,
                0,
            )
            pv_pio = json.loads(
                flow(
                    "publish-verify",
                    "--repo",
                    str(repo),
                    "--remote",
                    "origin",
                    "--branch",
                    branch,
                    "--expect",
                    head1,
                    "--json",
                ).stdout
            )
            self.assertEqual(pv_pio["status"], "match")
            self.assertEqual(pv_pio["target"]["remote_url_hash"], hash_a["url_hash"])
            self.assertEqual(pv_pio["target"]["push_binding"], "different")
            self.assertEqual(pv_pio["target"]["push_url_hashes"], [hash_b["url_hash"]])
            res_pio = json.loads(flow("resume", "--repo", str(repo), "--json").stdout)
            self.assertFalse(res_pio["publication"]["latest"]["current_delivery_verified"])
            self.assertEqual(
                run(
                    "git",
                    "-C",
                    str(repo),
                    "config",
                    "--local",
                    "--unset",
                    f"url.{bare_b.as_posix()}.pushInsteadOf",
                ).returncode,
                0,
            )

            # 6. Errors introduced by push-resolution path become unverified binding rather than claiming success.
            real_local_git = self.pub._local_git

            def fail_push_git(cwd, *args, **kwargs):
                if "get-url" in args and "--push" in args:
                    raise OSError("simulated push get-url failure")
                return real_local_git(cwd, *args, **kwargs)

            with mock.patch.object(self.pub, "_local_git", side_effect=fail_push_git):
                pv_err = self.pub.verify_publication(
                    repo,
                    remote="origin",
                    branch=branch,
                    expect=head1,
                )
                self.assertEqual(pv_err["status"], "match")
                self.assertEqual(pv_err["target"]["push_binding"], "unverified")
                self.assertTrue(pv_err["target"]["push_differs"])
                self.assertIn("unverified", pv_err["reason"])
                sum_err = self.pub.build_publication_summary(repo)
                self.assertFalse(sum_err["latest"]["current_delivery_verified"])

            # 7. Old historical receipt lacking push binding is treated as 'unknown' (not retroactively verified),
            # and historical receipt bytes are preserved verbatim.
            repo_leg, bare_leg, head_leg = self._setup_repo_and_bare_remote("bug-533704c36aa2-legacy")
            self._seed_bare_ref(repo_leg, bare_leg, f"refs/heads/{branch}", head_leg)
            v_leg = self._write_full_delivery_verdict(repo_leg, head_leg)
            leg_info, leg_err, _ = self.pub.validate_and_sanitize_remote_url(str(bare_leg), repo_leg)
            assert leg_err is None and leg_info is not None
            legacy_tid = hashlib.sha256(
                f"origin\n{leg_info['url_hash']}\nrefs/heads/{branch}".encode("utf-8")
            ).hexdigest()
            legacy_receipt = {
                "version": 1,
                "id": "00000000-0000-4000-8000-000000000001",
                "at": "2026-11-17T04:50:00Z",
                "status": "match",
                "exit_code": 0,
                "error_code": None,
                "reason": f"remote refs/heads/{branch} matches expected commit {head_leg[:12]}",
                "target": {
                    "remote": "origin",
                    "branch": branch,
                    "ref": f"refs/heads/{branch}",
                    "remote_kind": "local",
                    "remote_url_hash": leg_info["url_hash"],
                    "target_id": legacy_tid,
                },
                "expected_sha": head_leg,
                "observed_sha": head_leg,
                "head_sha": head_leg,
                "tree_clean": True,
                "candidate_fingerprint": v_leg["candidate_fingerprint"],
                "verdict": {"exists": True, "readable": True, "result": "PASS", "head": head_leg},
                "acceptance": {
                    "onboarded": True,
                    "covers_delivery": True,
                    "fresh_for_head": True,
                    "matches_expected": True,
                    "fresh_delivery": True,
                    "issues": [],
                },
                "authority_note": self.pub.AUTHORITY_NOTE,
            }
            pub_path_leg = repo_leg / ".gate" / "publication.ndjson"
            legacy_bytes = (json.dumps(legacy_receipt, separators=(",", ":")) + "\n").encode("utf-8")
            pub_path_leg.write_bytes(legacy_bytes)

            res_leg_json = json.loads(flow("resume", "--repo", str(repo_leg), "--json").stdout)
            self.assertEqual(pub_path_leg.read_bytes(), legacy_bytes, "historical receipt bytes must stay untouched")
            self.assertEqual(res_leg_json["publication"]["latest"]["observed"]["status"], "match")
            self.assertEqual(res_leg_json["publication"]["latest"]["target"]["push_binding"], "unknown")
            self.assertFalse(res_leg_json["publication"]["latest"]["current_delivery_verified"])
            res_leg_txt = flow("resume", "--repo", str(repo_leg)).stdout
            self.assertIn("push=unknown", res_leg_txt)
            self.assertIn("current delivery: NOT verified", res_leg_txt)
            self.assertNotIn("verified at receipt time", res_leg_txt)

            # Appending a new receipt preserves the historical legacy receipt prefix byte-for-byte.
            pv_after_leg = flow(
                "publish-verify",
                "--repo",
                str(repo_leg),
                "--remote",
                "origin",
                "--branch",
                branch,
                "--expect",
                head_leg,
                "--json",
            )
            self.assertEqual(pv_after_leg.returncode, 0)
            self.assertTrue(pub_path_leg.read_bytes().startswith(legacy_bytes))
            res_after_leg = json.loads(flow("resume", "--repo", str(repo_leg), "--json").stdout)
            self.assertTrue(res_after_leg["publication"]["latest"]["current_delivery_verified"])


if __name__ == "__main__":
    unittest.main()
