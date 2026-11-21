from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "flow-run" / "scripts" / "route.py",
    Path(__file__).resolve().with_name("route.py"),
)
ROUTE_PY = next((p for p in _CANDIDATES if p.is_file()), _CANDIDATES[0])


class RoutingCLITest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True)
        self.policy_file = self.home / ".config" / "mini-model-routing" / "policy.json"
        self.write_policy(("gem-a", "medium"), ("cdx-a", "high"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_policy(self, gemini: tuple[str, str], codex: tuple[str, str]) -> None:
        self.policy_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "coding": {
                "gemini": {"model": gemini[0], "effort": gemini[1]},
                "codex": {"model": codex[0], "effort": codex[1]},
            }
        }
        self.policy_file.write_text(json.dumps(payload), encoding="utf-8")

    def run_cli(self, *args: str, direct_exec: bool = False) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env.pop("MINI_MODEL_ROUTING_POLICY", None)
        cmd = [str(ROUTE_PY), *args] if direct_exec else [sys.executable, str(ROUTE_PY), *args]
        return subprocess.run(cmd, text=True, capture_output=True, env=env, check=False)

    def recommend(self, task_class: str = "routine", repo: Path | None = None) -> dict:
        target = repo or self.repo
        proc = self.run_cli("recommend", "--repo", str(target), "--class", task_class)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertTrue(isinstance(data["reason"], str) and data["reason"] and "\n" not in data["reason"])
        return data

    @unittest.skipIf(os.name == 'nt', 'POSIX installer')
    def test_installer_copies_shared_module_for_skill_without_shim(self):
        source = ROUTE_PY.parents[3]
        env = dict(os.environ, HOME=str(self.home))
        result = subprocess.run(['bash', str(source / 'install.sh'), '--agent', 'codex',
                                 '--skill', 'flow-run', '--no-shim'], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        copied = self.home / '.codex/skills/flow-run/scripts'
        self.assertEqual((copied / 'model_routes.py').read_bytes(),
                         (source / 'gate/model_routes.py').read_bytes())
        result = subprocess.run([sys.executable, str(copied / 'route.py'), 'recommend',
                                 '--repo', str(self.repo), '--class', 'routine'],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['primary']['role'], 'gemini')

    def test_account_outage_filters_routes_without_changing_quality_history(self):
        import time
        cache = self.home / '.local/share/flow/model-inventory.json'
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({'observations': [dict(
            route='agy', model='gem-a', status='cooldown', authority='account',
            observed_at=time.time() - 1, until=time.time() + 1000)]}))
        data = self.recommend()
        self.assertEqual(data['primary']['role'], 'codex')
        self.assertIsNone(data['backup'])
        self.assertEqual(data['excluded'][0]['eligibility'], 'account_cooldown')
        self.assertFalse((self.repo / '.gate/routing.sqlite3').exists())
        cache.write_text(json.dumps({'observations': [dict(
            route='agy', model='gem-a', status='cooldown', authority='account',
            observed_at=1, until=2)]}))
        self.assertEqual(self.recommend()['primary']['role'], 'gemini')

    @unittest.skipIf(os.name == 'nt', 'POSIX installer shim contract')
    def test_installed_skill_resolves_shared_runtime_without_running_shim(self):
        skill = self.root / 'copied-skill/scripts/route.py'
        skill.parent.mkdir(parents=True)
        skill.write_text(ROUTE_PY.read_text())
        runtime = self.root / 'release/runtime'
        (runtime / 'gate').mkdir(parents=True)
        (runtime / 'gate/model_routes.py').write_text(
            (ROUTE_PY.parents[3] / 'gate/model_routes.py').read_text())
        binary = self.home / '.local/bin/flow'
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\nexec python3 "' + str(runtime) + '/flow/flow.py" "$@"\n')
        binary.chmod(0o755)
        # There is no flow.py in this fixture: executing the shim must fail.
        env = dict(os.environ, HOME=str(self.home), PATH=str(binary.parent) + os.pathsep + os.environ.get('PATH', ''))
        result = subprocess.run([sys.executable, str(skill), 'recommend', '--repo', str(self.repo),
                                 '--class', 'routine'], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['primary']['role'], 'gemini')

    def record(
        self,
        run_id: str,
        task_id: str,
        *,
        task_class: str = "routine",
        primary: tuple[str, str, str] = ("gemini", "gem-a", "medium"),
        primary_outcome: str = "pass",
        backup: tuple[str, str, str, str] | None = None,
        repo: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        target = repo or self.repo
        cmd = [
            "record",
            "--repo",
            str(target),
            "--class",
            task_class,
            "--run-id",
            run_id,
            "--task-id",
            task_id,
            "--primary-role",
            primary[0],
            "--primary-model",
            primary[1],
            "--primary-effort",
            primary[2],
            "--primary-outcome",
            primary_outcome,
        ]
        if backup is not None:
            cmd.extend(
                [
                    "--backup-role",
                    backup[0],
                    "--backup-model",
                    backup[1],
                    "--backup-effort",
                    backup[2],
                    "--backup-outcome",
                    backup[3],
                ]
            )
        return self.run_cli(*cmd)

    def test_defaults_legacy_repo_and_absolute_callable(self) -> None:
        self.assertTrue(ROUTE_PY.is_absolute() and ROUTE_PY.is_file())
        r_routine = self.recommend("routine")
        self.assertEqual([x["role"] for x in r_routine["ranked"]], ["gemini", "codex"])
        self.assertEqual(r_routine["primary"]["model"], "gem-a")
        self.assertEqual(r_routine["primary"]["effort"], "medium")
        self.assertEqual(r_routine["backup"]["model"], "cdx-a")
        self.assertFalse((self.repo / ".gate" / "routing.sqlite3").exists())

        r_complex = self.recommend("complex")
        self.assertEqual([x["role"] for x in r_complex["ranked"]], ["codex", "gemini"])

        legacy = self.root / "legacy"
        (legacy / ".gate").mkdir(parents=True)
        sqlite3.connect(str(legacy / ".gate" / "routing.sqlite3")).close()
        r_legacy = self.recommend("routine", repo=legacy)
        self.assertEqual([x["role"] for x in r_legacy["ranked"]], ["gemini", "codex"])

        exec_copy = self.root / "route_exec.py"
        exec_copy.write_text(ROUTE_PY.read_text(encoding="utf-8"), encoding="utf-8")
        (self.root / "model_routes.py").write_text(
            (ROUTE_PY.parents[3] / "gate" / "model_routes.py").read_text(), encoding="utf-8")
        exec_copy.chmod(exec_copy.stat().st_mode | stat.S_IXUSR)
        env = dict(os.environ, HOME=str(self.home))
        env.pop("MINI_MODEL_ROUTING_POLICY", None)
        proc = subprocess.run(
            [str(exec_copy.resolve()), "recommend", "--repo", str(self.repo), "--class", "routine"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["primary"]["role"], "gemini")

        alt_policy = self.root / "alt_policy.json"
        alt_policy.write_text(
            json.dumps({"coding": {"gemini": {"model": "rogue", "effort": "low"}, "codex": {"model": "rogue", "effort": "low"}}}),
            encoding="utf-8",
        )
        env_with_override = dict(env, MINI_MODEL_ROUTING_POLICY=str(alt_policy))
        proc_env = subprocess.run(
            [sys.executable, str(ROUTE_PY), "recommend", "--repo", str(self.repo), "--class", "routine"],
            text=True,
            capture_output=True,
            env=env_with_override,
            check=False,
        )
        self.assertEqual(proc_env.returncode, 0, proc_env.stderr)
        self.assertEqual(json.loads(proc_env.stdout)["primary"]["model"], "gem-a")
        self.assertNotEqual(
            self.run_cli("recommend", "--repo", str(self.repo), "--class", "routine", "--policy", str(alt_policy)).returncode,
            0,
        )
        self.policy_file.write_text(
            json.dumps({"coding": {"gemini": {"model": "gem-a", "reasoning_effort": "medium"}, "codex": {"model": "cdx-a", "effort": "high"}}}),
            encoding="utf-8",
        )
        self.assertNotEqual(self.run_cli("recommend", "--repo", str(self.repo), "--class", "routine").returncode, 0)
        self.assertNotEqual(self.record("re-bad", "re-task").returncode, 0)
        self.write_policy(("gem-a", "medium"), ("cdx-a", "high"))

    def test_learning_on_rescues_and_backup_failure_guard(self) -> None:
        for idx in (1, 2):
            res = self.record(
                f"r{idx}",
                f"t{idx}",
                primary_outcome="quality_fail",
                backup=("codex", "cdx-a", "high", "pass"),
            )
            self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.recommend("routine")["primary"]["role"], "gemini")

        self.assertEqual(self.record("r3", "t3", primary_outcome="pass").returncode, 0)
        res4 = self.record(
            "r4",
            "t4",
            primary_outcome="quality_fail",
            backup=("codex", "cdx-a", "high", "pass"),
        )
        self.assertEqual(res4.returncode, 0, res4.stderr)
        rec = self.recommend("routine")
        self.assertEqual([x["role"] for x in rec["ranked"]], ["codex", "gemini"])

        repo2 = self.root / "repo_bfail"
        repo2.mkdir()
        self.assertEqual(
            self.record(
                "r1",
                "t1",
                repo=repo2,
                primary_outcome="quality_fail",
                backup=("codex", "cdx-a", "high", "fail"),
            ).returncode,
            0,
        )
        for idx in (2, 3, 4):
            self.assertEqual(
                self.record(
                    f"r{idx}",
                    f"t{idx}",
                    repo=repo2,
                    primary_outcome="quality_fail",
                    backup=("codex", "cdx-a", "high", "pass"),
                ).returncode,
                0,
            )
        self.assertEqual(self.recommend("routine", repo=repo2)["primary"]["role"], "gemini")

        self.assertEqual(self.record("r5", "t5", repo=repo2, primary_outcome="pass").returncode, 0)
        self.assertEqual(
            self.record(
                "r6",
                "t6",
                repo=repo2,
                primary_outcome="quality_fail",
                backup=("codex", "cdx-a", "high", "pass"),
            ).returncode,
            0,
        )
        self.assertEqual(self.recommend("routine", repo=repo2)["primary"]["role"], "codex")

    def test_no_idle_credit_and_invalid_evidence_rejected(self) -> None:
        for idx in range(1, 6):
            self.assertEqual(
                self.record(f"r{idx}", f"t{idx}", primary_outcome="quality_fail").returncode,
                0,
            )
        self.assertEqual(self.recommend("routine")["primary"]["role"], "gemini")

        bad_cases = [
            self.record(
                "bad1",
                "tb1",
                primary_outcome="pass",
                backup=("codex", "cdx-a", "high", "pass"),
            ),
            self.run_cli(
                "record",
                "--repo",
                str(self.repo),
                "--class",
                "routine",
                "--run-id",
                "bad2",
                "--task-id",
                "tb2",
                "--primary-role",
                "gemini",
                "--primary-model",
                "gem-a",
                "--primary-effort",
                "medium",
                "--primary-outcome",
                "quality_fail",
                "--backup-role",
                "codex",
                "--backup-outcome",
                "pass",
            ),
            self.record(
                "bad3",
                "tb3",
                primary_outcome="quality_fail",
                backup=("gemini", "gem-a", "medium", "pass"),
            ),
            self.record("bad4", "tb4", primary_outcome="infra"),
            self.record(
                "bad5",
                "tb5",
                primary_outcome="quality_fail",
                backup=("codex", "unknown", "high", "pass"),
            ),
            self.record("bad6", "   ", primary_outcome="pass"),
        ]
        for proc in bad_cases:
            self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.recommend("routine")["primary"]["role"], "gemini")

    def test_duplicate_task_pin_single_eligible_and_package_qualified_tasks(self) -> None:
        for i in range(50):
            self.record(
                f"dup-run-{i:02d}",
                "pkg-1/T01",
                primary_outcome="quality_fail",
                backup=("codex", "cdx-a", "high", "pass"),
            )
        self.assertEqual(self.recommend("routine")["primary"]["role"], "gemini")

        self.record("pkg2-run-1", "pkg-2/T01", primary_outcome="pass")
        self.record("pkg3-run-1", "pkg-3/T01", primary_outcome="pass")

        db_path = self.repo / ".gate" / "routing.sqlite3"

        conn = sqlite3.connect(str(db_path))
        try:
            total_pkg1, elig_pkg1 = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(eligible), 0) FROM events WHERE task_id = ?",
                ("pkg-1/T01",),
            ).fetchone()
            self.assertEqual(total_pkg1, 50)
            self.assertEqual(elig_pkg1, 1)

            per_pkg = dict(
                conn.execute(
                    """
                    SELECT task_id, SUM(eligible) FROM events
                    WHERE task_id IN ('pkg-1/T01', 'pkg-2/T01', 'pkg-3/T01')
                    GROUP BY task_id
                    """
                ).fetchall()
            )
            self.assertEqual(
                per_pkg,
                {"pkg-1/T01": 1, "pkg-2/T01": 1, "pkg-3/T01": 1},
            )
        finally:
            conn.close()

    def test_scope_recency_distinct_tasks_and_replay_idempotency(self) -> None:
        for idx in (1, 2):
            self.assertEqual(
                self.record(
                    f"r{idx}",
                    f"t{idx}",
                    primary_outcome="quality_fail",
                    backup=("codex", "cdx-a", "high", "pass"),
                ).returncode,
                0,
            )
        for idx in (3, 4, 5, 6):
            self.assertEqual(self.record(f"r{idx}", f"t{idx}", primary_outcome="pass").returncode, 0)
        self.assertEqual(
            self.record(
                "r7",
                "t7",
                primary_outcome="quality_fail",
                backup=("codex", "cdx-a", "high", "pass"),
            ).returncode,
            0,
        )
        self.assertEqual(self.recommend("routine")["primary"]["role"], "gemini")

        repo_dup = self.root / "repo_dup"
        repo_dup.mkdir()
        for idx in (1, 2, 3):
            self.assertEqual(
                self.record(
                    f"run-{idx}",
                    "same-task",
                    repo=repo_dup,
                    primary_outcome="quality_fail",
                    backup=("codex", "cdx-a", "high", "pass"),
                ).returncode,
                0,
            )
        self.assertEqual(self.recommend("routine", repo=repo_dup)["primary"]["role"], "gemini")

        dup_ok = self.record(
            "run-1",
            "same-task",
            repo=repo_dup,
            primary_outcome="quality_fail",
            backup=("codex", "cdx-a", "high", "pass"),
        )
        self.assertEqual(dup_ok.returncode, 0, dup_ok.stderr)
        self.assertTrue(json.loads(dup_ok.stdout)["idempotent"])

        conflict = self.record("run-1", "same-task", repo=repo_dup, primary_outcome="pass")
        self.assertNotEqual(conflict.returncode, 0)

        self.write_policy(("gem-b", "high"), ("cdx-b", "xhigh"))
        dup_after_policy = self.record(
            "run-1",
            "same-task",
            repo=repo_dup,
            primary_outcome="quality_fail",
            backup=("codex", "cdx-a", "high", "pass"),
        )
        self.assertEqual(dup_after_policy.returncode, 0, dup_after_policy.stderr)
        self.assertTrue(json.loads(dup_after_policy.stdout)["idempotent"])
        off_pol = self.record(
            "run-off",
            "task-off",
            repo=repo_dup,
            primary=("gemini", "off-policy", "low"),
            primary_outcome="quality_fail",
            backup=("codex", "cdx-b", "xhigh", "pass"),
        )
        self.assertEqual(off_pol.returncode, 0, off_pol.stderr)
        self.assertEqual(self.recommend("routine", repo=repo_dup)["primary"]["model"], "gem-b")
        self.write_policy(("gem-a", "medium"), ("cdx-a", "high"))

        for idx in (8, 9):
            self.assertEqual(
                self.record(
                    f"r{idx}",
                    f"t{idx}",
                    primary_outcome="quality_fail",
                    backup=("codex", "cdx-a", "high", "pass"),
                ).returncode,
                0,
            )
        self.assertEqual(self.recommend("routine")["primary"]["role"], "codex")
        self.assertEqual(self.recommend("complex")["primary"]["role"], "codex")
        for idx in (1, 2, 3):
            self.assertEqual(
                self.record(
                    f"cx{idx}",
                    f"ct{idx}",
                    task_class="complex",
                    primary=("codex", "cdx-a", "high"),
                    primary_outcome="quality_fail",
                    backup=("gemini", "gem-a", "medium", "pass"),
                ).returncode,
                0,
            )
        self.assertEqual(self.recommend("complex")["primary"]["role"], "gemini")
        self.assertEqual(self.recommend("routine")["primary"]["role"], "codex")

    def test_successor_inheritance_zero_transfer_rollback_and_backup_guard(self) -> None:
        repo_b = self.root / "repo_backup_zero"
        repo_b.mkdir()
        for idx in (1, 2):
            self.assertEqual(
                self.record(
                    f"r{idx}",
                    f"t{idx}",
                    repo=repo_b,
                    primary_outcome="quality_fail",
                    backup=("codex", "cdx-a", "high", "pass"),
                ).returncode,
                0,
            )
        self.write_policy(("gem-a", "medium"), ("cdx-b", "xhigh"))
        self.assertEqual(
            self.record(
                "r3",
                "t3",
                repo=repo_b,
                primary_outcome="quality_fail",
                backup=("codex", "cdx-b", "xhigh", "pass"),
            ).returncode,
            0,
        )
        self.assertEqual(self.recommend("routine", repo=repo_b)["primary"]["role"], "gemini")

        self.write_policy(("gem-a", "medium"), ("cdx-a", "high"))
        for idx in (1, 2, 3):
            self.assertEqual(
                self.record(
                    f"s{idx}",
                    f"st{idx}",
                    primary_outcome="quality_fail",
                    backup=("codex", "cdx-a", "high", "pass"),
                ).returncode,
                0,
            )
        self.assertEqual(self.recommend("routine")["primary"]["role"], "codex")

        self.write_policy(("gem-b", "high"), ("cdx-b", "xhigh"))
        rec_succ = self.recommend("routine")
        self.assertEqual(rec_succ["primary"]["role"], "codex")
        self.assertEqual(rec_succ["primary"]["model"], "cdx-b")
        self.assertTrue(rec_succ["provisional"])

        repo_guard = self.root / "repo_guard"
        repo_guard.mkdir()
        self.write_policy(("gem-a", "medium"), ("cdx-a", "high"))
        for idx in (1, 2, 3):
            self.assertEqual(
                self.record(
                    f"g{idx}",
                    f"gt{idx}",
                    repo=repo_guard,
                    primary_outcome="quality_fail",
                    backup=("codex", "cdx-a", "high", "pass"),
                ).returncode,
                0,
            )
        self.write_policy(("gem-b", "high"), ("cdx-b", "xhigh"))
        for idx in (4, 5):
            self.assertEqual(
                self.record(
                    f"g{idx}",
                    f"gt{idx}",
                    repo=repo_guard,
                    primary=("codex", "cdx-b", "xhigh"),
                    primary_outcome="quality_fail",
                    backup=("gemini", "gem-b", "high", "fail"),
                ).returncode,
                0,
            )
        guard_rec = self.recommend("routine", repo=repo_guard)
        self.assertEqual(guard_rec["primary"]["role"], "codex")
        self.assertTrue(guard_rec["provisional"])
        for idx in (6, 7, 8):
            self.assertEqual(
                self.record(
                    f"g{idx}",
                    f"gt{idx}",
                    repo=repo_guard,
                    primary=("codex", "cdx-b", "xhigh"),
                    primary_outcome="pass",
                ).returncode,
                0,
            )
        self.assertTrue(self.recommend("routine", repo=repo_guard)["provisional"])
        self.assertEqual(
            self.record(
                "g9",
                "gt9",
                repo=repo_guard,
                primary=("codex", "cdx-b", "xhigh"),
                primary_outcome="pass",
            ).returncode,
            0,
        )
        self.assertFalse(self.recommend("routine", repo=repo_guard)["provisional"])

        for idx in (4, 5):
            self.assertEqual(
                self.record(
                    f"s{idx}",
                    f"st{idx}",
                    primary=("codex", "cdx-b", "xhigh"),
                    primary_outcome="quality_fail",
                ).returncode,
                0,
            )
        rolled = self.recommend("routine")
        self.assertEqual([x["role"] for x in rolled["ranked"]], ["gemini", "codex"])
        self.assertFalse(rolled["provisional"])

        self.assertEqual(
            self.record(
                "stale-p",
                "st-stale-p",
                primary=("codex", "cdx-b", "xhigh"),
                primary_outcome="pass",
            ).returncode,
            0,
        )
        for idx in (1, 2, 3):
            self.assertEqual(
                self.record(
                    f"stale-r{idx}",
                    f"st-stale-r{idx}",
                    primary=("gemini", "gem-a", "medium"),
                    primary_outcome="quality_fail",
                    backup=("codex", "cdx-b", "xhigh", "pass"),
                ).returncode,
                0,
            )
        after_stale = self.recommend("routine")
        self.assertEqual(after_stale["primary"]["role"], "gemini")
        self.assertEqual(after_stale["epoch"], rolled["epoch"])

        self.write_policy(("gem-c", "high"), ("cdx-c", "xhigh"))
        self.assertTrue(self.recommend("routine")["provisional"])
        for idx in (6, 7):
            self.assertEqual(
                self.record(
                    f"s{idx}",
                    f"st{idx}",
                    primary=("gemini", "gem-c", "high"),
                    primary_outcome="quality_fail",
                ).returncode,
                0,
            )
        after_gem_c = self.recommend("routine")
        self.assertEqual(after_gem_c["primary"]["role"], "codex")
        self.assertFalse(after_gem_c["provisional"])

        self.write_policy(("gem-b", "high"), ("cdx-b", "xhigh"))
        suppressed = self.recommend("routine")
        self.assertEqual(suppressed["primary"]["role"], "gemini")
        self.assertFalse(suppressed["provisional"])

        self.write_policy(("gem-b", "high"), ("cdx-d", "xhigh"))
        next_succ = self.recommend("routine")
        self.assertEqual(next_succ["primary"]["role"], "codex")
        self.assertEqual(next_succ["primary"]["model"], "cdx-d")
        self.assertTrue(next_succ["provisional"])
        for idx in (8, 9, 10, 11):
            self.assertEqual(
                self.record(
                    f"s{idx}",
                    f"st{idx}",
                    primary=("codex", "cdx-d", "xhigh"),
                    primary_outcome="pass",
                ).returncode,
                0,
            )
        confirmed = self.recommend("routine")
        self.assertEqual(confirmed["primary"]["role"], "codex")
        self.assertFalse(confirmed["provisional"])

    def test_no_stale_flip_flop_and_concurrent_runs(self) -> None:
        for idx in (1, 2, 3):
            self.assertEqual(
                self.record(
                    f"r{idx}",
                    f"t{idx}",
                    primary_outcome="quality_fail",
                    backup=("codex", "cdx-a", "high", "pass"),
                ).returncode,
                0,
            )
        self.assertEqual(self.recommend("routine")["primary"]["role"], "codex")

        for idx in (4, 5):
            self.assertEqual(
                self.record(
                    f"r{idx}",
                    f"t{idx}",
                    primary=("codex", "cdx-a", "high"),
                    primary_outcome="quality_fail",
                    backup=("gemini", "gem-a", "medium", "pass"),
                ).returncode,
                0,
            )
        self.assertEqual(self.recommend("routine")["primary"]["role"], "codex")
        self.assertEqual(
            self.record(
                "r6",
                "t6",
                primary=("codex", "cdx-a", "high"),
                primary_outcome="quality_fail",
                backup=("gemini", "gem-a", "medium", "pass"),
            ).returncode,
            0,
        )
        self.assertEqual(self.recommend("routine")["primary"]["role"], "gemini")

        conc_repo = self.root / "conc_repo"
        conc_repo.mkdir()

        def _worker(i: int) -> int:
            proc = self.record(
                f"crun-{i}",
                f"ctask-{i}",
                repo=conc_repo,
                primary_outcome="pass",
            )
            return proc.returncode

        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(_worker, range(16)))
        self.assertEqual(codes, [0] * 16)
        conn = sqlite3.connect(str(conc_repo / ".gate" / "routing.sqlite3"))
        try:
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 16)


if __name__ == "__main__":
    unittest.main()
