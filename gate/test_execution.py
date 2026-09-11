"""No CLI inference before explicit task profiles; later global changes cannot drift them."""
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

import execution
from test_compat import committed_runner_repo, load_gate_module, run


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = committed_runner_repo(Path(self.tmp.name), ["codex", "claude"])
        self.gate = load_gate_module()
        self.cfg = self.gate.load_config(self.repo)
        self.text = (self.repo / "TASK_QUEUE.md").read_text(encoding="utf-8")

    def resolve(self, text=None, cfg=None):
        text = self.text if text is None else text
        return execution.resolve(cfg or self.cfg, text, self.gate.parse_queue(text),
                                 self.gate.parse_task_workers(text))

    def test_override_materializes_revisions_without_mutating_defaults(self):
        text = self.text + '<!-- task:EAV-3 execution: {"workers":{"codex":{"reasoning_effort":"high"}}} -->\n'
        policies = self.resolve(text)
        self.assertEqual(policies["EAV-2"]["profiles"]["workers"]["codex"]["reasoning_effort"], "medium")
        self.assertEqual(policies["EAV-3"]["profiles"]["revisions"]["codex"]["reasoning_effort"], "high")
        self.assertEqual(self.cfg["execution"]["defaults"]["workers"]["codex"]["reasoning_effort"], "medium")

    def test_missing_fallback_and_judge_profile_refused(self):
        for role, member in (("workers", "claude"), ("judges", "codex")):
            cfg = deepcopy(self.cfg)
            cfg["judge"] = {"enabled": True}
            del cfg["execution"]["defaults"][role][member]
            with self.subTest(role=role), self.assertRaises(execution.PolicyError):
                self.resolve(cfg=cfg)

    def test_bad_queue_metadata_and_alias_refused(self):
        for marker in ('{"workers":', '{"typo":{}}', '{"workers":{"codex":{"model":"default"}}}'):
            with self.subTest(marker=marker), self.assertRaises(execution.PolicyError):
                self.resolve(self.text + f'<!-- task:EAV-2 execution: {marker} -->')
        marker = '<!-- task:EAV-2 execution: {} -->'
        with self.assertRaises(execution.PolicyError):
            self.resolve(self.text + marker + marker)

    def test_persistent_lock_requires_recorded_change_and_preserves_started_task(self):
        first = execution.freeze(self.repo, self.resolve())
        cfg = deepcopy(self.cfg)
        cfg["execution"]["defaults"]["workers"]["codex"]["reasoning_effort"] = "high"
        changed = self.resolve(cfg=cfg)
        with self.assertRaises(execution.PolicyError):
            execution.freeze(self.repo, changed)
        with self.assertRaises(execution.PolicyError):
            execution.freeze(self.repo, changed, "explicit user change", {"EAV-3"})
        second = execution.freeze(self.repo, {"EAV-3": changed["EAV-3"]}, "explicit user change", {"EAV-3"})
        self.assertEqual(second["tasks"]["EAV-2"], first["tasks"]["EAV-2"])
        self.assertEqual(second["previous_sha256"], first["sha256"])
        self.assertTrue((self.repo / ".gate/execution-locks" / (first["sha256"] + ".json")).exists())

    def test_tampered_lock_is_refused(self):
        lock = execution.freeze(self.repo, self.resolve())
        lock["tasks"]["EAV-2"]["pin"] = "claude"
        execution.lock_path(self.repo).write_text(json.dumps(lock))
        with self.assertRaises(execution.PolicyError):
            execution.read_lock(self.repo)

    def test_native_flags_override_all_old_selection_forms_and_keep_resume(self):
        requested = {"model": "codex-test-model", "reasoning_effort": "medium"}
        argv = ["codex", "exec", "--model=old", "-mold", "-c", 'model_reasoning_effort="xhigh"',
                "--config=model=old", '-cmodel_reasoning_effort="high"', "--sandbox", "read-only",
                "-C", "{projdir}", "--json", "resume", "session-id", "-"]
        result = execution.arguments(argv, "codex", requested)
        self.assertEqual(result[-3:], ["resume", "session-id", "-"])
        self.assertEqual(result.count("--model"), 1)
        self.assertNotIn("old", " ".join(result))
        self.assertNotIn("xhigh", " ".join(result))
        self.assertIn('model_reasoning_effort="medium"', result)
        self.assertIn("read-only", result)

    def test_hidden_fallback_or_unimplemented_effort_is_refused(self):
        with self.assertRaises(execution.PolicyError):
            execution.arguments(["claude", "--fallback-model", "other"], "claude",
                                {"model": "claude-test", "reasoning_effort": "medium"})
        with self.assertRaises(execution.PolicyError):
            execution.profile({"model": "kimi-test", "reasoning_effort": "medium"}, "kimi")

    def test_global_change_does_not_change_frozen_launch_arguments(self):
        lock = execution.freeze(self.repo, self.resolve())
        cfg = execution.bind(self.cfg, lock["tasks"]["EAV-2"], "EAV-2")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.repo / "other-session")}):
            cli_home = Path(os.environ["CODEX_HOME"])
            cli_home.mkdir()
            (cli_home / "config.toml").write_text('model = "different-model"\nmodel_reasoning_effort = "xhigh"')
            actual = execution.arguments(self.gate.WORKER_CMDS["codex"], "codex", execution.selected(cfg, "codex"))
        self.assertIn("codex-test-model", actual)
        self.assertIn('model_reasoning_effort="medium"', actual)

    def test_missing_policy_stops_run_before_probe_or_worker_even_when_forced(self):
        cfg = deepcopy(self.cfg)
        cfg.pop("execution")
        (self.repo / ".gate/config.json").write_text(json.dumps(cfg))
        with mock.patch.object(self.gate, "spawn_worker") as spawn:
            with self.assertRaises(SystemExit):
                self.gate.cmd_run(SimpleNamespace(repo=str(self.repo), max_tasks=1, force=True, strict_admit=False))
        spawn.assert_not_called()

    def test_worker_judge_and_revision_choose_separate_profiles(self):
        self.cfg["judge"] = {"enabled": True, "chain": ["codex"]}
        self.cfg["execution"]["defaults"]["judges"]["codex"]["reasoning_effort"] = "high"
        self.cfg["execution"]["defaults"]["revisions"] = deepcopy(self.cfg["execution"]["defaults"]["workers"])
        self.cfg["execution"]["defaults"]["revisions"]["codex"]["model"] = "codex-revision-model"
        cfg = execution.bind(self.cfg, self.resolve()["EAV-2"], "EAV-2")
        self.assertEqual(execution.selected(cfg, "codex")["reasoning_effort"], "medium")
        cfg["_execution_role"] = "judges"
        self.assertEqual(execution.selected(cfg, "codex")["reasoning_effort"], "high")
        cfg["_execution_role"] = "revisions"
        self.assertEqual(execution.selected(cfg, "codex")["model"], "codex-revision-model")

    def test_observation_is_not_inferred_from_requested_or_task_text(self):
        expected = {"model": "codex-test", "reasoning_effort": "medium"}
        unknown = execution.observed('no model metadata\nuser\nmodel: codex-test', expected)
        self.assertEqual(unknown["model"], "unknown")
        actual = execution.observed('OpenAI Codex\nmodel: codex-test\nreasoning effort: xhigh\nuser\n', expected)
        self.assertTrue(actual["mismatch"])

    def test_actual_spawn_pins_worker_judge_revision_and_records_runtime(self):
        import sys
        # Python runs this extensionless local stand-in as `python exec ...`.
        # It observes the real argv delivered by Popen, without any AI/network.
        (self.repo / "exec").write_text(
            "import sys\nargs=sys.argv[1:]\nsys.stdin.read()\n"
            "model=args[args.index('--model')+1]\n"
            "effort=args[args.index('-c')+1].split('=',1)[1].strip(chr(34))\n"
            "print('OpenAI Codex fake\\nmodel: '+model+'\\nreasoning effort: '+effort+'\\nuser\\nOK')\n",
            encoding="utf-8")
        self.cfg["judge"] = {"enabled": True, "chain": ["codex"]}
        cfg = execution.bind(self.cfg, self.resolve()["EAV-2"], "EAV-2")
        for role in ("workers", "judges", "revisions"):
            cfg["_execution_role"] = role
            with self.subTest(role=role), mock.patch.object(self.gate, "resolve_tool", return_value=sys.executable):
                result, error = self.gate.spawn_worker(self.repo, cfg, "codex", "OK", self.repo / "prompt.md",
                                                       template=self.gate.WORKER_CMDS["codex"])
            self.assertIsNone(error)
            self.assertEqual(result.returncode, 0)
            self.assertIn("reasoning effort: medium", result.stdout)
            self.assertFalse(result.execution_mismatch)
        events = [json.loads(line) for line in (self.repo / ".gate/journal.ndjson").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({e["role"] for e in events if e["event"] == "execution_observed"},
                         {"workers", "judges", "revisions"})

    def test_running_queue_applies_recorded_change_only_at_next_task_boundary(self):
        seen = []

        def worker(repo, cfg, name, prompt, prompt_file, **kwargs):
            task = cfg["_execution_task"]
            seen.append((task, execution.selected(cfg, name)["reasoning_effort"]))
            queue = repo / "TASK_QUEUE.md"
            text = queue.read_text(encoding="utf-8")
            source = "eav2.txt" if task == "EAV-2" else "eav3.txt"
            (repo / source).write_text("done", encoding="utf-8")
            rows = text.splitlines()
            text = "\n".join(row.replace('`TODO`', '`DONE`') if f'| {task} |' in row else row for row in rows) + "\n"
            queue.write_text(text, encoding="utf-8")
            self.assertEqual(run("git", "-C", str(repo), "add", "TASK_QUEUE.md", source).returncode, 0)
            self.assertEqual(run("git", "-C", str(repo), "commit", "-qm", "complete test task").returncode, 0)
            if task == "EAV-2":
                future = deepcopy(execution.read_lock(repo)["tasks"]["EAV-3"])
                future["profiles"]["workers"]["codex"]["reasoning_effort"] = "high"
                execution.freeze(repo, {"EAV-3": future}, "user explicitly changed next task", {"EAV-3"})
            return self.gate.WorkerResult(0, "done"), None

        with mock.patch.object(self.gate, "resolve_tool", side_effect=lambda name: name), \
                mock.patch.object(self.gate, "spawn_worker", side_effect=worker):
            self.gate.cmd_run(SimpleNamespace(repo=str(self.repo), max_tasks=2, force=True, strict_admit=True))
        self.assertEqual(seen, [("EAV-2", "medium"), ("EAV-3", "high")])
        snapshots = list((self.repo / ".gate/runs").glob("*/EAV-2.execution.json"))
        self.assertEqual(len(snapshots), 1)
        original = json.loads(snapshots[0].read_text(encoding="utf-8"))
        self.assertEqual(original["profiles"]["workers"]["codex"]["reasoning_effort"], "medium")


if __name__ == "__main__":
    unittest.main()
