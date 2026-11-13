"""Isolated policy/engine boundary tests: no CLI, network or project access."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import gate
import model_policy
from model_policy import validate_worker

POLICY = Path(__file__).with_name('test_policy.json')
CODEX = ['codex', 'exec', '--sandbox', 'danger-full-access', '--skip-git-repo-check',
         '-C', '{projdir}', '-c', 'model="gpt-6.1-sol"',
         '-c', 'model_reasoning_effort="medium"', '-']
GEMINI = ['agy', '--model', 'gemini-4-hydrogen-high', '--effort', 'high',
          '--dangerously-skip-permissions', '--no-session-persistence', '-p', '{prompt}']
CLAUDE = ['claude', '--model', 'claude-sonnet-5-5', '--effort', 'medium', '-p', '{prompt}']
NATIVE_CODEX = ['codex', '-a', 'never', '-c', 'model="gpt-6.1-sol"',
                '-c', 'model_reasoning_effort="medium"',
                '-c', 'sandbox_workspace_write.network_access=true', 'exec',
                '--ephemeral', '--sandbox', 'workspace-write', '--add-dir',
                '{projdir}/.git', '-C', '{projdir}', '-']
AGY_FLOW = ['agy-flow', '--model', 'gemini-4-hydrogen-high', '--effort', 'high',
            '--mode', 'auto', '--sandbox', '--dangerously-skip-permissions',
            '--disable-slash-commands', '--output-format', 'json',
            '--print-timeout', '20m', '-p', '{prompt}']


class PolicyTests(unittest.TestCase):
    def check(self, argv, worker='codex', model='', cfg=None):
        return validate_worker(worker, argv, model, cfg or {}, POLICY)

    def blocked(self, argv, worker='codex', model='', cfg=None, contains=None):
        error = self.check(argv, worker, model, cfg)
        self.assertIsInstance(error, str)
        self.assertTrue(error.startswith('model_policy:'), error)
        self.assertIn('host must reconcile', error)
        if contains:
            self.assertIn(contains, error)

    def test_codex_config_and_model_forms(self):
        self.assertIsNone(self.check(CODEX))
        for model_flag in (['--model', 'gpt-6.1-sol'], ['-m', 'gpt-6.1-sol'],
                           ['--model=gpt-6.1-sol'], ['-m=gpt-6.1-sol'],
                           ['--config=model="gpt-6.1-sol"'], ['--config', "model='gpt-6.1-sol'"]):
            for effort_flag in (['--config=model_reasoning_effort="medium"'],
                                ['-c', 'model_reasoning_effort=medium']):
                with self.subTest(model_flag=model_flag, effort_flag=effort_flag):
                    self.assertIsNone(self.check(['codex', 'exec'] + model_flag + effort_flag))

    def test_model_placeholder(self):
        argv = ['codex', 'exec', '--model={model}', '-c', 'model_reasoning_effort="medium"']
        self.assertIsNone(self.check(argv, model='gpt-6.1-sol'))
        self.blocked(argv, contains='worker_models.codex')
        self.blocked(argv, model='gpt-6-astra')

    def test_gemini_high_native_permissions(self):
        for worker in ('agy', 'agy-flow'):
            argv = [worker] + GEMINI[1:] + ['--settings', '{"permissions":{"allow":["Read","Bash(*)"],"defaultMode":"acceptEdits"}}']
            self.assertIsNone(self.check(argv, worker))
        self.assertIsNone(self.check(['agy', '--model=gemini-4-hydrogen-high', '--effort=high'], 'agy'))

    def test_claude_fallback_only(self):
        self.blocked(CLAUDE, 'claude', contains='fallback_reason')
        self.blocked(CLAUDE, 'claude', cfg={'model_policy_fallback_reason': 'quota'})
        cfg = {'model_policy_fallback_reason': 'primary_coding_unavailable'}
        self.assertIsNone(self.check(CLAUDE, 'claude', cfg=cfg))
        self.assertIsNone(self.check(CLAUDE + ['--settings', gate.CLAUDE_HEADLESS_SETTINGS], 'claude', cfg=cfg))

    def test_old_coding_models_blocked(self):
        self.blocked([p.replace('gpt-6.1-sol', 'gpt-6-astra') for p in CODEX])
        self.blocked([p.replace('gemini-4-hydrogen-high', 'gemini-4-carbon-medium') for p in GEMINI], 'agy')

    def test_complete_native_contracts_and_route_restrictions(self):
        for argv, worker, old, stale in (
                (NATIVE_CODEX, 'codex', 'gpt-6.1-sol', 'gpt-6-astra'),
                (AGY_FLOW, 'agy-flow', 'gemini-4-hydrogen-high', 'gemini-4-carbon-medium'),
                (['agy'] + AGY_FLOW[1:], 'agy', 'gemini-4-hydrogen-high', 'gemini-4-carbon-medium')):
            with self.subTest(worker=worker):
                original = argv.copy()
                self.assertIsNone(self.check(argv, worker))
                self.assertEqual(argv, original)
                self.blocked([p.replace(old, stale) for p in argv], worker)
                for override in (['--provider=x'], ['--profile=x']):
                    self.blocked(argv + override, worker)

    def test_exact_dotted_config_and_worker_option_values(self):
        for key in ('sandbox_workspace_write.network_access.extra',
                    'sandbox_workspace_write.model_provider',
                    'sandbox_workspace_write.network_access_provider'):
            self.blocked(NATIVE_CODEX + ['-c', key + '=true'])
        self.blocked(NATIVE_CODEX + ['--sandbox'], contains='needs a value')
        for option in ('--mode', '--print-timeout'):
            self.blocked(AGY_FLOW + [option], 'agy-flow', contains='needs a value')
            self.assertIsNone(self.check(AGY_FLOW + [option, '--model=evil'], 'agy-flow'))
            self.blocked(NATIVE_CODEX + [option, 'auto'])
        self.blocked(AGY_FLOW + ['--sandbox=true'], 'agy-flow', contains='boolean')
        for settings in ('{"provider":"x"}', '{"profile":"x"}',
                         '{"permissions":{"provider":"x"}}',
                         '{"permissions":{"profile":"x"}}'):
            self.blocked(AGY_FLOW + ['--settings', settings], 'agy-flow')

    def test_missing_conflicting_duplicate_pins(self):
        for argv in (['codex', 'exec'], CODEX + ['--model=gpt-6-astra'],
                     CODEX + ['--model=gpt-6.1-sol'], CODEX + ['-c', 'model_reasoning_effort="high"'],
                     CODEX + ['-c', 'model_reasoning_effort="medium"'],
                     ['codex', '--model=gpt-6.1-sol'], CODEX + ['--model']):
            with self.subTest(argv=argv):
                self.blocked(argv)
        self.blocked(GEMINI + ['--effort=high'], 'agy')
        self.blocked(GEMINI[:-6], 'agy')

    def test_route_overrides_rejected(self):
        for override in (['--profile', 'other'], ['-p', 'other'], ['--model-provider=x'],
                         ['--config-file=x'], ['-c', 'model_provider="x"'],
                         ['--config=config_file="x"'], ['-c', 'profiles.foo.model="x"'],
                         ['--fallback-model=x'], ['--agent=x'], ['--unknown-route=x']):
            with self.subTest(override=override):
                self.blocked(CODEX + override)
        for override in (['--fallback-model', 'x'], ['--agent', 'x'], ['--agents={}'],
                         ['--settings-file=x'], ['--provider=x'], ['--setting-sources=user']):
            self.blocked(GEMINI + override, 'agy')

    def test_settings_restricted_to_scoped_permissions(self):
        for settings in ('/tmp/settings.json', '{}', '[]', '{bad',
                         '{"model":"x"}', '{"effort":"high"}', '{"provider":"x"}',
                         '{"permissions":{"model":"x"}}', '{"permissions":{"nested":{"modelProvider":"x"}}}',
                         '{"enabledPlugins":{"x":true}}', '{"permissions":{},"env":{"MODEL":"x"}}',
                         '{"permissions":{},"permissions":{"model":"x"}}'):
            with self.subTest(settings=settings):
                self.blocked(GEMINI + ['--settings', settings], 'agy')

    def test_prompts_are_opaque_and_separator_stops_parsing(self):
        for option in ('-p', '--prompt', '--print'):
            self.assertIsNone(self.check(GEMINI + [option, '--model=evil --effort low --provider=x'], 'agy'))
            self.assertIsNone(self.check(GEMINI + [option + '=--model=evil'], 'agy'))
        self.assertIsNone(self.check(CODEX + ['--', '--model=evil', '--profile=x']))
        self.blocked(['agy', '-p', '--model=gemini-4-hydrogen-high', '--effort=high'], 'agy', contains='model pin')
        self.blocked(['codex', 'exec', '--', '--model=gpt-6.1-sol', '-c', 'model_reasoning_effort=medium'])

    def test_unknown_and_wrapper_workers(self):
        for worker in ('helper', 'pi', 'grok', 'kimi'):
            self.blocked([worker] + CODEX[1:], worker)
        self.blocked(['wrapper'] + CODEX[1:], contains='wrapper')

    def test_missing_malformed_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'policy.json'
            self.assertTrue(validate_worker('codex', CODEX, '', {}, path).startswith('model_policy:'))
            for body in ('{', 'null', '[]', '{}', '{"version":1,"coding":[]}',
                         '{"version":1,"version":1}', '{"version":true,"coding":{}}'):
                path.write_text(body)
                self.assertTrue(validate_worker('codex', CODEX, '', {}, path).startswith('model_policy:'))
            policy = json.loads(POLICY.read_text())
            policy['coding']['codex']['model'] = None
            path.write_text(json.dumps(policy))
            self.assertTrue(validate_worker('codex', CODEX, '', {}, path).startswith('model_policy:'))

    def test_policy_is_authority_and_fixed_default(self):
        self.assertEqual(model_policy.POLICY_PATH, Path.home() / '.config/mini-model-routing/policy.json')
        with patch.object(model_policy, 'POLICY_PATH', POLICY):
            self.assertIsNone(validate_worker('codex', CODEX, '', {}))


class SpawnIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.budget = gate.RUN_BUDGET.copy()
        gate.RUN_BUDGET.update(max=10, calls=0)
        self.addCleanup(gate.RUN_BUDGET.update, self.budget)
        self.cfg = copy.deepcopy(gate.DEFAULT_CONFIG)
        self.cfg['worker_cmds'] = {'codex': CODEX}
        self.root = Path('/unused-disposable-test')

    def spawn(self, **kwargs):
        return gate.spawn_worker(self.root, self.cfg, kwargs.pop('worker', 'codex'), kwargs.pop('prompt', 'Implement task'),
                                 self.root / 'prompt.md', **kwargs)

    def test_actual_spawn_rejects_before_budget_journal_popen(self):
        self.cfg['worker_cmds']['codex'] = [p.replace('gpt-6.1-sol', 'gpt-6-astra') for p in CODEX]
        self.cfg['role'] = 'review'  # User config cannot select the bypass.
        with patch.object(gate._model_policy, 'POLICY_PATH', POLICY), \
             patch.object(gate, 'resolve_tool', return_value='/native/tls/codex'), \
             patch.object(gate.subprocess, 'Popen') as popen, patch.object(gate, 'journal') as journal:
            result, error = self.spawn()
            self.assertIsNone(result)
            self.assertTrue(error.startswith('model_policy:'), error)
            self.assertEqual(gate.RUN_BUDGET['calls'], 0)
            popen.assert_not_called()
            journal.assert_not_called()

    def test_spawn_resolves_worker_models_and_preserves_executable_args(self):
        self.cfg['worker_cmds']['codex'] = ['codex', 'exec', '--model', '{model}', '-c', 'model_reasoning_effort="medium"', '--sandbox', 'danger-full-access', '-']
        self.cfg['worker_models'] = {'codex': 'gpt-6.1-sol'}
        self._allowed_spawn(expected_role='worker')

    def test_complete_native_contract_arguments_preserved(self):
        for template in (NATIVE_CODEX, AGY_FLOW):
            worker = template[0]
            with self.subTest(worker=worker):
                gate.RUN_BUDGET['calls'] = 0
                self.cfg['worker_cmds'] = {worker: template.copy()}
                argv = self._allowed_spawn('worker', worker=worker)
                expected = [p.replace('{projdir}', str(self.root)).replace('{prompt}', 'Implement task')
                            for p in template[1:]]
                self.assertEqual(argv[1:], expected)
                self.assertEqual(self.cfg['worker_cmds'][worker], template)

    def _allowed_spawn(self, expected_role, **kwargs):
        proc = Mock(returncode=0, stdout=io.StringIO('OK\n'), stdin=io.StringIO())
        proc.poll.return_value = 0
        with patch.object(gate._model_policy, 'POLICY_PATH', POLICY), \
             patch.object(gate, 'resolve_tool', return_value='/native/tls/codex'), \
             patch.object(gate.subprocess, 'Popen', return_value=proc) as popen, \
             patch.object(gate, 'journal') as journal:
            result, error = self.spawn(**kwargs)
            self.assertIsNone(error)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(gate.RUN_BUDGET['calls'], 1)
            self.assertEqual(popen.call_args.args[0][0], '/native/tls/codex')
            self.assertEqual(journal.call_args_list[0].args[1]['role'], expected_role)
            return popen.call_args.args[0]

    def test_review_bypass_uses_original_template(self):
        old = [p.replace('gpt-6.1-sol', 'gpt-6-astra') for p in CODEX]
        with patch.object(gate, 'validate_worker', side_effect=AssertionError('review must bypass')):
            argv = self._allowed_spawn('review', template=old)
        self.assertIn('model="gpt-6-astra"', argv)

    def test_probe_bypass_unchanged(self):
        self.cfg['worker_cmds']['codex'] = ['codex', 'exec', '-']
        with patch.object(gate, 'validate_worker', side_effect=AssertionError('probe must bypass')):
            self._allowed_spawn('probe', prompt=gate.PROBE_PROMPT)

    def test_missing_policy_blocks_actual_spawn(self):
        with patch.object(gate._model_policy, 'POLICY_PATH', self.root / 'missing-policy.json'), \
             patch.object(gate, 'resolve_tool', return_value='/native/codex'), \
             patch.object(gate.subprocess, 'Popen') as popen:
            _, error = self.spawn()
            self.assertTrue(error.startswith('model_policy:'))
            self.assertEqual(gate.RUN_BUDGET['calls'], 0)
            popen.assert_not_called()

    def test_unknown_worker_and_missing_contract_use_policy_refusal(self):
        with patch.object(gate._model_policy, 'POLICY_PATH', POLICY), \
             patch.object(gate, 'resolve_tool', return_value='/native/helper'), \
             patch.object(gate.subprocess, 'Popen') as popen:
            for worker in ('helper', 'agy'):
                _, error = gate.spawn_worker(self.root, self.cfg, worker, 'Implement task', self.root / 'prompt.md')
                self.assertTrue(error.startswith('model_policy:'), error)
            self.assertEqual(gate.RUN_BUDGET['calls'], 0)
            popen.assert_not_called()


class AbsoluteImportTests(unittest.TestCase):
    def test_copied_gate_loads_exact_sibling_without_changing_sys_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            sibling = Path(tmp) / 'engine'
            sibling.mkdir()
            gate_path = sibling / 'gate.py'
            shutil.copyfile(gate.__file__, gate_path)
            shutil.copyfile(model_policy.__file__, sibling / 'model_policy.py')
            shutil.copyfile(Path(gate.__file__).with_name('usage.py'), sibling / 'usage.py')
            original_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                self.assertNotEqual(Path.cwd(), sibling)
                self.assertNotIn(sibling, [Path(p or os.getcwd()).resolve() for p in sys.path])
                original_sys_path = sys.path.copy()
                spec = importlib.util.spec_from_file_location('copied_gate', gate_path)
                copied_gate = importlib.util.module_from_spec(spec)
                with patch.dict(sys.modules, {'model_policy': Mock()}):
                    spec.loader.exec_module(copied_gate)
                self.assertEqual(sys.path, original_sys_path)
                self.assertEqual(Path(copied_gate._model_policy.__file__), sibling / 'model_policy.py')
                self.assertIs(copied_gate.validate_worker.__globals__, vars(copied_gate._model_policy))
                with patch.object(copied_gate._model_policy, 'POLICY_PATH', POLICY):
                    self.assertIsNone(copied_gate.validate_worker('codex', CODEX, '', {}))
                    stale = [p.replace('gpt-6.1-sol', 'gpt-6-astra') for p in CODEX]
                    self.assertTrue(copied_gate.validate_worker('codex', stale, '', {}).startswith('model_policy:'))
                (sibling / 'model_policy.py').unlink()
                missing_spec = importlib.util.spec_from_file_location('missing_sibling_gate', gate_path)
                with self.assertRaises(FileNotFoundError):
                    missing_spec.loader.exec_module(importlib.util.module_from_spec(missing_spec))
                self.assertEqual(sys.path, original_sys_path)
            finally:
                os.chdir(original_cwd)


if __name__ == '__main__':
    unittest.main()
