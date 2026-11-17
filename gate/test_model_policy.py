"""Isolated policy/engine boundary tests: no CLI, network or project access."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import gate
import model_policy
from model_policy import validate_worker

POLICY = Path(__file__).with_name('test_policy.json')
CODEX = ['codex', 'exec', '--sandbox', 'danger-full-access', '--skip-git-repo-check',
         '-C', '{projdir}', '-c', 'model="gpt-6.1-sol"',
         '-c', 'model_reasoning_effort="medium"', '-']
ASTRA = [p.replace('gpt-6.1-sol', 'gpt-6-astra') for p in CODEX]
GEMINI = ['agy', '--model', 'gemini-4-hydrogen-high', '--effort', 'high',
          '--dangerously-skip-permissions', '--no-session-persistence', '-p', '{prompt}']
CLAUDE = ['claude', '--model', 'claude-sonnet-5-5', '--effort', 'medium', '-p', '{prompt}']
OPUS = ['claude', '--model', 'claude-opus-5-5', '--effort', 'medium', '-p', '{prompt}']
FABLE = ['claude', '--model', 'claude-fable-5-2', '--effort', 'medium', '-p', '{prompt}']
NATIVE_CODEX = ['codex', '-a', 'never', '-c', 'model="gpt-6.1-sol"',
                '-c', 'model_reasoning_effort="medium"',
                '-c', 'sandbox_workspace_write.network_access=true', 'exec',
                '--ephemeral', '--sandbox', 'workspace-write', '--add-dir',
                '{projdir}/.git', '-C', '{projdir}', '-']
AGY_FLOW = ['agy-flow', '--model', 'gemini-4-hydrogen-high', '--effort', 'high',
            '--mode', 'auto', '--sandbox', '--dangerously-skip-permissions',
            '--disable-slash-commands', '--output-format', 'json',
            '--print-timeout', '20m', '-p', '{prompt}']
CARBON_FLOW = [
    'medium' if i > 0 and AGY_FLOW[i - 1] == '--effort' else p.replace('gemini-4-hydrogen-high', 'gemini-4-carbon-medium')
    for i, p in enumerate(AGY_FLOW)
]
CARBON_AGY = ['agy'] + CARBON_FLOW[1:]


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


def _git(repo, *args):
    proc = subprocess.run(
        ['git', '-C', str(repo), *args],
        capture_output=True, text=True, encoding='utf-8', check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc.stdout.strip()


def _init_package_repo(root, *, queue_text=None, with_spec=True):
    repo = root / 'flow'
    repo.mkdir()
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.name', 'Gate Test')
    _git(repo, 'config', 'user.email', 'gate@example.invalid')
    pkg_dir = repo / 'docs' / 'work' / 'ai-evolution-ideas'
    pkg_dir.mkdir(parents=True)
    if with_spec:
        (pkg_dir / 'implementation.md').write_text('# Implementation admission\n', encoding='utf-8')
        (pkg_dir / 'spec.md').write_text('# Spec\n', encoding='utf-8')
    cr_dir = repo / 'docs' / 'work' / 'native-committed-recovery'
    cr_dir.mkdir(parents=True)
    (cr_dir / 'spec.md').write_text('# CR-1\n', encoding='utf-8')
    if queue_text is None:
        queue_text = (
            '# TASK_QUEUE\n\n'
            '| # | ID | name | status | start baseline | end baseline |\n'
            '|---|---|---|---|---|---|\n'
            '| 16 | IL-1 | Difficult learner isolation | `TODO` | — | pending |\n'
            '<!-- task:IL-1 files: learner/store.py -->\n'
            '<!-- task:IL-1 verify: {"cmd":"python3 -m unittest","timeout_s":60} -->\n'
            '<!-- task:IL-1 spec: docs/work/ai-evolution-ideas/implementation.md -->\n'
            '| 17 | IL-2 | Ordinary learner adapter | `TODO` | — | pending |\n'
            '<!-- task:IL-2 files: learner/cli.py -->\n'
            '<!-- task:IL-2 verify: {"cmd":"python3 -m unittest","timeout_s":60} -->\n'
            '<!-- task:IL-2 spec: docs/work/ai-evolution-ideas/implementation.md -->\n'
            '| 15 | CR-1 | Recover verified work | `BLOCKED` | 247 passed | pending |\n'
            '<!-- task:CR-1 files: gate/gate.py -->\n'
            '<!-- task:CR-1 spec: docs/work/native-committed-recovery/spec.md -->\n'
        )
    (repo / 'TASK_QUEUE.md').write_text(queue_text, encoding='utf-8')
    (repo / '.gate').mkdir()
    cfg = copy.deepcopy(gate.DEFAULT_CONFIG)
    cfg['workers'] = ['agy-flow']
    cfg['worker_cmds'] = {'agy-flow': CARBON_FLOW}
    cfg['probe'] = {'enabled': False}
    cfg['judge'] = {'enabled': False}
    cfg['max_tasks'] = 1
    cfg['max_attempts_per_task'] = 1
    cfg['max_model_calls'] = 5
    cfg['verify_cmd'] = 'python3 -c "print(\'1 passed\')"'
    cfg['model_policy_exception'] = {
        'enabled': True,
        'repository': str(repo),
        'package': 'ai-evolution-ideas',
        'tasks': ['IL-1'],
        'authorization': {
            'approved_by': 'owner: 2026-11-16 ai-evolution-ideas admission',
            'record': 'docs/work/ai-evolution-ideas/implementation.md',
        },
        'expires_at': time.time() + 3600,
    }
    (repo / '.gate' / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-q', '-m', 'init')
    return repo, cfg


class PackageExceptionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo, self.cfg = _init_package_repo(Path(self.tmp.name))
        self.ctx = {
            'repository': str(self.repo),
            'project_prefix': '',
            'task': 'IL-1',
            'task_status': 'TODO',
            'dispatchable': True,
            'package': 'ai-evolution-ideas',
            'spec': 'docs/work/ai-evolution-ideas/implementation.md',
        }

    def check(self, worker, argv, cfg=None, context=None, model='', policy_path=POLICY):
        return validate_worker(
            worker, argv, model, self.cfg if cfg is None else cfg,
            policy_path=policy_path, context=context,
        )

    def blocked(self, worker, argv, contains=None, **kwargs):
        err = self.check(worker, argv, **kwargs)
        self.assertIsInstance(err, str)
        self.assertTrue(err.startswith('model_policy:'), err)
        self.assertIn('host must reconcile', err)
        if contains:
            self.assertIn(contains, err)
        return err

    def test_canonical_defaults_and_hydrogen_unchanged(self):
        self.assertEqual(model_policy.CANONICAL_REPO, Path.home() / 'AI/maintenance/flow')
        self.assertEqual(model_policy.EXCEPTION_PACKAGE, 'ai-evolution-ideas')
        self.assertIsNone(self.check('agy-flow', AGY_FLOW, cfg={}))
        self.assertIsNone(self.check('agy', GEMINI, cfg={}))
        self.assertIsNone(self.check('codex', CODEX, cfg={}))

    def test_carbon_and_opus_allowed_with_bound_exception_and_no_fabricated_fallback(self):
        with patch.object(model_policy, 'CANONICAL_REPO', self.repo):
            self.assertIsNone(self.check('agy-flow', CARBON_FLOW, context=self.ctx))
            self.assertIsNone(self.check('agy', CARBON_AGY, context=self.ctx))
            self.assertIsNone(self.check('claude', OPUS, context=self.ctx))
            # {model} placeholder resolution works for difficult-coding models too.
            opus_tmpl = ['claude', '--model', '{model}', '--effort', 'medium', '-p', '{prompt}']
            self.assertIsNone(self.check('claude', opus_tmpl, model='claude-opus-5-5', context=self.ctx))
            # Timezone-aware ISO-8601 expiry is accepted.
            iso_cfg = copy.deepcopy(self.cfg)
            iso_cfg['model_policy_exception']['expires_at'] = '2099-01-01T00:00:00Z'
            self.assertIsNone(self.check('claude', OPUS, cfg=iso_cfg, context=self.ctx))
            # Hydrogen remains usable alongside or without an exception.
            self.assertIsNone(self.check('agy-flow', AGY_FLOW, cfg=iso_cfg, context=self.ctx))

    def test_missing_or_forged_config_context_rejected(self):
        with patch.object(model_policy, 'CANONICAL_REPO', self.repo):
            # Missing engine context fails closed even when cfg embeds a fake context.
            forged_cfg = copy.deepcopy(self.cfg)
            forged_cfg['context'] = dict(self.ctx)
            forged_cfg['engine_context'] = dict(self.ctx)
            self.blocked('agy-flow', CARBON_FLOW, cfg=forged_cfg, context=None, contains='context')
            self.blocked('claude', OPUS, cfg=forged_cfg, context=None, contains='context')

    def test_wrong_repository_package_task_and_cr1_rejected(self):
        # Default CANONICAL_REPO rejects temp repo.
        self.blocked('agy-flow', CARBON_FLOW, context=self.ctx, contains='wrong repository')
        with patch.object(model_policy, 'CANONICAL_REPO', self.repo):
            wrong_pkg = dict(self.ctx, package='native-committed-recovery',
                             spec='docs/work/native-committed-recovery/spec.md')
            self.blocked('agy-flow', CARBON_FLOW, context=wrong_pkg, contains='wrong package')

            unlisted_task = dict(self.ctx, task='IL-2')
            self.blocked('claude', OPUS, context=unlisted_task, contains='allowlist')

            not_dispatchable = dict(self.ctx, task_status='BLOCKED', dispatchable=False)
            self.blocked('agy-flow', CARBON_FLOW, context=not_dispatchable, contains='dispatchable')

            subproj = dict(self.ctx, project_prefix='sub')
            self.blocked('agy-flow', CARBON_FLOW, context=subproj, contains='subprojects')

            cr1_cfg = copy.deepcopy(self.cfg)
            cr1_cfg['model_policy_exception']['tasks'] = ['IL-1', 'CR-1']
            self.blocked('agy-flow', CARBON_FLOW, cfg=cr1_cfg, context=self.ctx, contains='forbidden')
            cr1_ctx = dict(self.ctx, task='CR-1', package='native-committed-recovery',
                           spec='docs/work/native-committed-recovery/spec.md', dispatchable=False)
            self.blocked('claude', OPUS, cfg=cr1_cfg, context=cr1_ctx, contains='wrong package')

    def test_malformed_expired_and_bad_auth_grants_rejected(self):
        with patch.object(model_policy, 'CANONICAL_REPO', self.repo):
            bad_grants = [
                None,
                {},
                {**self.cfg['model_policy_exception'], 'enabled': False},
                {**self.cfg['model_policy_exception'], 'enabled': 'true'},
                {**self.cfg['model_policy_exception'], 'extra': 1},
                {**self.cfg['model_policy_exception'], 'repository': 'flow'},
                {**self.cfg['model_policy_exception'], 'repository': '/tmp/other'},
                {**self.cfg['model_policy_exception'], 'package': 'flow-evolution'},
                {**self.cfg['model_policy_exception'], 'tasks': []},
                {**self.cfg['model_policy_exception'], 'tasks': ['*']},
                {**self.cfg['model_policy_exception'], 'tasks': ['IL-*']},
                {**self.cfg['model_policy_exception'], 'tasks': ['IL-1', 'IL-1']},
                {**self.cfg['model_policy_exception'], 'tasks': 'IL-1'},
                {**self.cfg['model_policy_exception'], 'authorization': 'owner'},
                {**self.cfg['model_policy_exception'], 'authorization': {'approved_by': '', 'record': 'docs/work/ai-evolution-ideas/implementation.md'}},
                {**self.cfg['model_policy_exception'], 'authorization': {'approved_by': 'owner', 'record': 'docs/work/ai-evolution-ideas/missing.md'}},
                {**self.cfg['model_policy_exception'], 'authorization': {'approved_by': 'owner', 'record': 'docs/work/native-committed-recovery/spec.md'}},
                {**self.cfg['model_policy_exception'], 'authorization': {'approved_by': 'owner', 'record': '../secret.md'}},
                {**self.cfg['model_policy_exception'], 'expires_at': time.time() - 5},
                {**self.cfg['model_policy_exception'], 'expires_at': '2020-01-01T00:00:00Z'},
                {**self.cfg['model_policy_exception'], 'expires_at': '2099-01-01T00:00:00'},
                {**self.cfg['model_policy_exception'], 'expires_at': True},
                {**self.cfg['model_policy_exception'], 'expires_at': float('inf')},
                {**self.cfg['model_policy_exception'], 'expires_at': 'tomorrow'},
            ]
            for grant in bad_grants:
                bad_cfg = copy.deepcopy(self.cfg)
                bad_cfg['model_policy_exception'] = grant
                with self.subTest(grant=grant):
                    self.blocked('agy-flow', CARBON_FLOW, cfg=bad_cfg, context=self.ctx)

    def test_unsupported_models_wrong_effort_duplicates_and_overrides_rejected(self):
        with patch.object(model_policy, 'CANONICAL_REPO', self.repo):
            # Codex Astra and Claude Fable remain forbidden even under valid exception.
            self.blocked('codex', ASTRA, context=self.ctx)
            self.blocked('claude', FABLE, context=self.ctx)
            # Wrong effort on Carbon / Opus / Hydrogen.
            self.blocked('agy-flow', [p if p != 'medium' else 'high' for p in CARBON_FLOW], context=self.ctx)
            self.blocked('claude', [p if p != 'medium' else 'high' for p in OPUS], context=self.ctx)
            self.blocked('agy-flow', [p if p != 'high' else 'medium' for p in AGY_FLOW], context=self.ctx)
            # Sonnet still requires primary_coding_unavailable even inside an exception task.
            self.blocked('claude', CLAUDE, context=self.ctx, contains='fallback_reason')
            # Opus with fabricated/fable fallback reason still fails without exception.
            no_exc = {'model_policy_fallback_reason': 'fable_quota_exhausted'}
            self.blocked('claude', OPUS, cfg=no_exc, context=self.ctx)
            # Duplicate pins and route overrides stay rejected on exception models.
            self.blocked('agy-flow', CARBON_FLOW + ['--model=gemini-4-carbon-medium'], context=self.ctx)
            self.blocked('agy-flow', CARBON_FLOW + ['--effort=medium'], context=self.ctx)
            self.blocked('agy-flow', CARBON_FLOW + ['--provider=x'], context=self.ctx)
            self.blocked('claude', OPUS + ['--profile=x'], context=self.ctx)
            self.blocked('claude', OPUS + ['--settings', '{"model":"x"}'], context=self.ctx)

            # Malformed policy design section fails closed on exception path.
            with tempfile.TemporaryDirectory() as ptmp:
                bad_policy = json.loads(POLICY.read_text())
                bad_policy['design']['gemini']['effort'] = ''
                ppath = Path(ptmp) / 'policy.json'
                ppath.write_text(json.dumps(bad_policy))
                self.blocked('agy-flow', CARBON_FLOW, context=self.ctx, policy_path=ppath)


class PackageExceptionEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo, self.cfg = _init_package_repo(Path(self.tmp.name))
        self.budget = gate.RUN_BUDGET.copy()
        gate.RUN_BUDGET.update(max=5, calls=0)
        self.addCleanup(gate.RUN_BUDGET.update, self.budget)

    def _spawn(self, repo, cfg, worker, **kwargs):
        return gate.spawn_worker(
            repo, cfg, worker, 'Implement task', repo / '.gate' / 'prompt.md', **kwargs
        )

    def _patch_worker_popen(self):
        real_popen = subprocess.Popen
        worker_calls = []

        def fake_popen(cmd, *args, **kwargs):
            if cmd and Path(cmd[0]).name == 'git':
                return real_popen(cmd, *args, **kwargs)
            worker_calls.append(cmd)
            proc = Mock(returncode=0, stdout=io.StringIO('OK\n'), stdin=io.StringIO())
            proc.poll.return_value = 0
            return proc

        return patch.object(gate.subprocess, 'Popen', side_effect=fake_popen), worker_calls

    def test_spawn_worker_derives_engine_context_for_carbon_and_opus(self):
        popen_patch, worker_calls = self._patch_worker_popen()
        with patch.object(gate._model_policy, 'POLICY_PATH', POLICY), \
             patch.object(gate._model_policy, 'CANONICAL_REPO', self.repo), \
             patch.object(gate, 'resolve_tool', return_value='/native/agy-flow'), \
             popen_patch, \
             patch.object(gate, 'journal'):
            res, err = self._spawn(self.repo, self.cfg, 'agy-flow', task='IL-1')
            self.assertIsNone(err)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(gate.RUN_BUDGET['calls'], 1)
            self.assertEqual(len(worker_calls), 1)

            opus_cfg = copy.deepcopy(self.cfg)
            opus_cfg['worker_cmds'] = {'claude': OPUS}
            res2, err2 = self._spawn(self.repo, opus_cfg, 'claude', task='IL-1')
            self.assertIsNone(err2)
            self.assertEqual(res2.returncode, 0)
            self.assertEqual(gate.RUN_BUDGET['calls'], 2)
            self.assertEqual(len(worker_calls), 2)

    def test_spawn_worker_rejects_before_budget_journal_popen_on_boundary_failures(self):
        # 1. Wrong repository (default CANONICAL_REPO != temp repo), even with matching grant.
        popen_patch, worker_calls = self._patch_worker_popen()
        with patch.object(gate._model_policy, 'POLICY_PATH', POLICY), \
             patch.object(gate, 'resolve_tool', return_value='/native/agy-flow'), \
             popen_patch, \
             patch.object(gate, 'journal') as journal:
            res, err = self._spawn(self.repo, self.cfg, 'agy-flow', task='IL-1')
            self.assertIsNone(res)
            self.assertTrue(err.startswith('model_policy:'), err)
            self.assertEqual(gate.RUN_BUDGET['calls'], 0)
            self.assertEqual(worker_calls, [])
            journal.assert_not_called()

        # 2. Missing task arg, unlisted task, CR-1, wrong-package spec, symlink escape, subdirectory.
        popen_patch, worker_calls = self._patch_worker_popen()
        with patch.object(gate._model_policy, 'POLICY_PATH', POLICY), \
             patch.object(gate._model_policy, 'CANONICAL_REPO', self.repo), \
             patch.object(gate, 'resolve_tool', return_value='/native/agy-flow'), \
             popen_patch, \
             patch.object(gate, 'journal') as journal:
            for kwargs in ({}, {'task': 'IL-2'}, {'task': 'CR-1'}, {'task': 'MISSING'}):
                res, err = self._spawn(self.repo, self.cfg, 'agy-flow', **kwargs)
                self.assertIsNone(res)
                self.assertTrue(err.startswith('model_policy:'), err)
            # Subdirectory instead of git root fails closed.
            res, err = self._spawn(self.repo / 'docs', self.cfg, 'agy-flow', task='IL-1')
            self.assertIsNone(res)
            self.assertTrue(err.startswith('model_policy:'), err)
            # Wrong-package spec in TASK_QUEUE.md fails closed even if task is allowlisted.
            qpath = self.repo / 'TASK_QUEUE.md'
            original_q = qpath.read_text(encoding='utf-8')
            qpath.write_text(
                original_q.replace(
                    '<!-- task:IL-1 spec: docs/work/ai-evolution-ideas/implementation.md -->',
                    '<!-- task:IL-1 spec: docs/work/native-committed-recovery/spec.md -->',
                ),
                encoding='utf-8',
            )
            res, err = self._spawn(self.repo, self.cfg, 'agy-flow', task='IL-1')
            self.assertIsNone(res)
            self.assertTrue(err.startswith('model_policy:'), err)
            qpath.write_text(original_q, encoding='utf-8')

            # Symlinked authorization record escaping package dir fails closed.
            escaped = self.repo / 'docs' / 'work' / 'ai-evolution-ideas' / 'escaped.md'
            escaped.symlink_to(self.repo / 'docs' / 'work' / 'native-committed-recovery' / 'spec.md')
            sym_cfg = copy.deepcopy(self.cfg)
            sym_cfg['model_policy_exception']['authorization']['record'] = 'docs/work/ai-evolution-ideas/escaped.md'
            res, err = self._spawn(self.repo, sym_cfg, 'agy-flow', task='IL-1')
            self.assertIsNone(res)
            self.assertTrue(err.startswith('model_policy:'), err)

            self.assertEqual(gate.RUN_BUDGET['calls'], 0)
            self.assertEqual(worker_calls, [])
            journal.assert_not_called()

    def test_cmd_run_passes_task_context_and_blocks_unlisted_head_task_before_popen(self):
        # Head task IL-1 is allowlisted -> cmd_run dispatches and invokes Popen once.
        args = Mock(repo=str(self.repo), max_tasks=1, force=True, strict_admit=True)
        popen_patch, worker_calls = self._patch_worker_popen()
        with patch.dict(os.environ, {'GATE_CONFIG': ''}), \
             patch.object(gate._model_policy, 'POLICY_PATH', POLICY), \
             patch.object(gate._model_policy, 'CANONICAL_REPO', self.repo), \
             patch.object(gate, 'resolve_tool', return_value='/native/agy-flow'), \
             popen_patch:
            with self.assertRaises(SystemExit):
                gate.cmd_run(args)
            self.assertEqual(len(worker_calls), 1)
            self.assertEqual(gate.RUN_BUDGET['calls'], 1)

        # Now change exception allowlist to ['IL-2'] so head task IL-1 is NOT allowlisted.
        cfg_path = self.repo / '.gate' / 'config.json'
        cfg2 = json.loads(cfg_path.read_text(encoding='utf-8'))
        cfg2['model_policy_exception']['tasks'] = ['IL-2']
        cfg_path.write_text(json.dumps(cfg2), encoding='utf-8')
        popen_patch2, worker_calls2 = self._patch_worker_popen()
        with patch.dict(os.environ, {'GATE_CONFIG': ''}), \
             patch.object(gate._model_policy, 'POLICY_PATH', POLICY), \
             patch.object(gate._model_policy, 'CANONICAL_REPO', self.repo), \
             patch.object(gate, 'resolve_tool', return_value='/native/agy-flow'), \
             popen_patch2:
            with self.assertRaises(SystemExit):
                gate.cmd_run(args)
            self.assertEqual(worker_calls2, [])
            self.assertEqual(gate.RUN_BUDGET['calls'], 0)

    def test_spawn_worker_retries_use_pinned_policy_snapshot_and_guard_integrity(self):
        from flow import skills as flow_skills

        policy_v1 = json.loads(POLICY.read_text(encoding='utf-8'))
        policy_v2 = copy.deepcopy(policy_v1)
        policy_v2['coding']['codex']['model'] = 'gpt-6.2-sol-next'
        p_file = Path(self.tmp.name) / 'live_policy.json'
        p_file.write_text(json.dumps(policy_v1), encoding='utf-8')

        # Pin active task IL-1 under policy_v1, then mutate live policy_v2 mid-task.
        flow_skills.pin_task_start(
            self.repo,
            'IL-1',
            skill_name='flow-run',
            policy_path=p_file,
            model_policy_py=Path(model_policy.__file__),
        )
        p_file.write_text(json.dumps(policy_v2), encoding='utf-8')

        # Active pinned IL-1 retry validates against pinned policy_v1 (gpt-6.1-sol), NOT mutated live policy_v2!
        codex_cfg = copy.deepcopy(self.cfg)
        codex_cfg['worker_cmds'] = {'codex': CODEX}
        popen_patch, worker_calls = self._patch_worker_popen()
        with patch.object(gate._model_policy, 'POLICY_PATH', p_file), \
             patch.object(gate._model_policy, 'CANONICAL_REPO', self.repo), \
             patch.object(gate, 'resolve_tool', return_value='/native/codex'), \
             popen_patch, \
             patch.object(gate, 'journal'):
            res, err = self._spawn(self.repo, codex_cfg, 'codex', task='IL-1')
            self.assertIsNone(err)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(len(worker_calls), 1)

            # New unpinned task IL-2 validates against live policy_v2 (refusing gpt-6.1-sol).
            res_new, err_new = self._spawn(self.repo, codex_cfg, 'codex', task='IL-2')
            self.assertIsNone(res_new)
            self.assertTrue(err_new.startswith('model_policy:'), err_new)

            # Tampering pinned guard_sha256 fails closed before budget/Popen.
            pin_path = self.repo / '.gate' / 'pins' / 'IL-1.json'
            pin_data = json.loads(pin_path.read_text(encoding='utf-8'))
            pin_data['model_policy']['guard_sha256'] = '0' * 64
            pin_path.write_text(json.dumps(pin_data), encoding='utf-8')
            res_tamper, err_tamper = self._spawn(self.repo, codex_cfg, 'codex', task='IL-1')
            self.assertIsNone(res_tamper)
            self.assertTrue(err_tamper.startswith('model_policy:'), err_tamper)
            self.assertEqual(len(worker_calls), 1)

    def test_bug_f54b34a0ebac_oserror_and_symlink_failures_fail_closed_without_traceback(self):
        # 1. Circular symlinks in authorization.record / spec and OSError during Path.resolve/is_file
        #    yield formatted model_policy: refusal strings, never tracebacks, before Popen/accounting.
        loop_a = self.repo / 'docs' / 'work' / 'ai-evolution-ideas' / 'loop-a.md'
        loop_b = self.repo / 'docs' / 'work' / 'ai-evolution-ideas' / 'loop-b.md'
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)

        loop_cfg = copy.deepcopy(self.cfg)
        loop_cfg['model_policy_exception']['authorization']['record'] = 'docs/work/ai-evolution-ideas/loop-a.md'
        popen_patch, worker_calls = self._patch_worker_popen()
        with patch.object(gate._model_policy, 'POLICY_PATH', POLICY), \
             patch.object(gate._model_policy, 'CANONICAL_REPO', self.repo), \
             patch.object(gate, 'resolve_tool', return_value='/native/agy-flow'), \
             popen_patch, \
             patch.object(gate, 'journal') as journal:
            res, err = self._spawn(self.repo, loop_cfg, 'agy-flow', task='IL-1')
            self.assertIsNone(res)
            self.assertTrue(err.startswith('model_policy:'), err)
            self.assertIn('host must reconcile', err)

            # Python <3.13 Path.resolve() raises RuntimeError('Symlink loop from ...') on cyclic symlinks.
            ctx = gate.worker_policy_context(self.repo, self.cfg, 'IL-1')
            orig_resolve = Path.resolve

            def _legacy_loop_resolve(self_path, *args, **kwargs):
                if self_path.name == 'loop-a.md':
                    raise RuntimeError(f'Symlink loop from {str(self_path)!r}')
                return orig_resolve(self_path, *args, **kwargs)

            with patch.object(model_policy, 'CANONICAL_REPO', self.repo), \
                 patch.object(Path, 'resolve', _legacy_loop_resolve):
                v_loop = validate_worker('agy-flow', CARBON_FLOW, '', loop_cfg, policy_path=POLICY, context=ctx)
                self.assertIsInstance(v_loop, str)
                self.assertTrue(v_loop.startswith('model_policy:'), v_loop)
                self.assertIn('Symlink loop', v_loop)
                self.assertIn('host must reconcile', v_loop)
                res_loop, err_loop = self._spawn(self.repo, loop_cfg, 'agy-flow', task='IL-1')
                self.assertIsNone(res_loop)
                self.assertTrue(err_loop.startswith('model_policy:'), err_loop)
                self.assertIn('Symlink loop', err_loop)
                self.assertIn('host must reconcile', err_loop)

            def _unrelated_runtime_error(self_path, *args, **kwargs):
                if self_path.name == 'implementation.md':
                    raise RuntimeError('unrelated resolve failure')
                return orig_resolve(self_path, *args, **kwargs)

            with patch.object(model_policy, 'CANONICAL_REPO', self.repo), \
                 patch.object(Path, 'resolve', _unrelated_runtime_error):
                with self.assertRaisesRegex(RuntimeError, 'unrelated resolve failure'):
                    validate_worker('agy-flow', CARBON_FLOW, '', self.cfg, policy_path=POLICY, context=ctx)

            # Simulated ELOOP/OSError from Path.resolve inside _validate_exception.
            def _eloop_resolve(self_path, *args, **kwargs):
                if self_path.name == 'implementation.md':
                    raise OSError(40, 'Too many levels of symbolic links')
                return orig_resolve(self_path, *args, **kwargs)

            with patch.object(Path, 'resolve', _eloop_resolve):
                v_err = validate_worker('agy-flow', CARBON_FLOW, '', self.cfg, policy_path=POLICY, context=ctx)
                self.assertIsInstance(v_err, str)
                self.assertTrue(v_err.startswith('model_policy:'), v_err)
                self.assertIn('host must reconcile', v_err)

                # In worker_policy_context + spawn_worker, OSError yields nondispatchable context & fail-closed refusal.
                nd_ctx = gate.worker_policy_context(self.repo, self.cfg, 'IL-1')
                self.assertIsNotNone(nd_ctx)
                self.assertFalse(nd_ctx['dispatchable'])
                self.assertIsNone(nd_ctx['spec'])
                res_os, err_os = self._spawn(self.repo, self.cfg, 'agy-flow', task='IL-1')
                self.assertIsNone(res_os)
                self.assertTrue(err_os.startswith('model_policy:'), err_os)

            # OSError from Path.is_file during exception path validation.
            orig_is_file = Path.is_file

            def _io_is_file(self_path, *args, **kwargs):
                if self_path.name == 'implementation.md':
                    raise PermissionError(13, 'Permission denied')
                return orig_is_file(self_path, *args, **kwargs)

            with patch.object(Path, 'is_file', _io_is_file):
                v_perm = validate_worker('agy-flow', CARBON_FLOW, '', self.cfg, policy_path=POLICY, context=ctx)
                self.assertTrue(v_perm.startswith('model_policy:'), v_perm)
                nd_perm = gate.worker_policy_context(self.repo, self.cfg, 'IL-1')
                self.assertFalse(nd_perm['dispatchable'])
                res_perm, err_perm = self._spawn(self.repo, self.cfg, 'agy-flow', task='IL-1')
                self.assertIsNone(res_perm)
                self.assertTrue(err_perm.startswith('model_policy:'), err_perm)

            self.assertEqual(gate.RUN_BUDGET['calls'], 0)
            self.assertEqual(worker_calls, [])
            journal.assert_not_called()

    def test_bug_f54b34a0ebac_queue_containment_under_git_root_and_preserved_authority_checks(self):
        outside_dir = Path(self.tmp.name) / 'outside'
        outside_dir.mkdir()
        outside_queue = outside_dir / 'OUTSIDE_QUEUE.md'
        outside_queue.write_text(
            (self.repo / 'TASK_QUEUE.md').read_text(encoding='utf-8'),
            encoding='utf-8',
        )

        # 1. Traversal, absolute path, and symlink escaping git_top must never read outside queue.
        symlink_queue = self.repo / 'SYM_QUEUE.md'
        symlink_queue.symlink_to(outside_queue)

        orig_read_text = Path.read_text
        outside_reads = []

        def _tracking_read_text(self_path, *args, **kwargs):
            if self_path.resolve() == outside_queue.resolve():
                outside_reads.append(str(self_path))
            return orig_read_text(self_path, *args, **kwargs)

        popen_patch, worker_calls = self._patch_worker_popen()
        with patch.object(gate._model_policy, 'POLICY_PATH', POLICY), \
             patch.object(gate._model_policy, 'CANONICAL_REPO', self.repo), \
             patch.object(gate, 'resolve_tool', return_value='/native/agy-flow'), \
             popen_patch, \
             patch.object(gate, 'journal') as journal:
            with patch.object(Path, 'read_text', _tracking_read_text):
                for bad_qf in ('../outside/OUTSIDE_QUEUE.md', str(outside_queue), 'SYM_QUEUE.md'):
                    with self.subTest(queue_file=bad_qf):
                        bad_cfg = copy.deepcopy(self.cfg)
                        bad_cfg['queue_file'] = bad_qf
                        ctx = gate.worker_policy_context(self.repo, bad_cfg, 'IL-1')
                        self.assertIsNotNone(ctx)
                        self.assertFalse(ctx['dispatchable'])
                        self.assertIsNone(ctx['package'])
                        self.assertIsNone(ctx['spec'])
                        res, err = self._spawn(self.repo, bad_cfg, 'agy-flow', task='IL-1')
                        self.assertIsNone(res)
                        self.assertTrue(err.startswith('model_policy:'), err)
            self.assertEqual(outside_reads, [], 'Outside-root queue file must never be read')

            # Missing, wrong, expired authority and CR-1 still refuse before Popen/accounting.
            expired_cfg = copy.deepcopy(self.cfg)
            expired_cfg['model_policy_exception']['expires_at'] = time.time() - 60
            wrong_auth_cfg = copy.deepcopy(self.cfg)
            wrong_auth_cfg['model_policy_exception']['authorization']['approved_by'] = '   '
            no_auth_cfg = copy.deepcopy(self.cfg)
            del no_auth_cfg['model_policy_exception']
            for label, bad_cfg, task_id in (
                ('missing_grant', no_auth_cfg, 'IL-1'),
                ('wrong_authority', wrong_auth_cfg, 'IL-1'),
                ('expired_grant', expired_cfg, 'IL-1'),
                ('cr1_forbidden', self.cfg, 'CR-1'),
            ):
                with self.subTest(refusal=label):
                    res, err = self._spawn(self.repo, bad_cfg, 'agy-flow', task=task_id)
                    self.assertIsNone(res)
                    self.assertTrue(err.startswith('model_policy:'), err)
            self.assertEqual(gate.RUN_BUDGET['calls'], 0)
            self.assertEqual(worker_calls, [])
            journal.assert_not_called()

            # Valid scoped Carbon on contained queue still succeeds.
            res_ok, err_ok = self._spawn(self.repo, self.cfg, 'agy-flow', task='IL-1')
            self.assertIsNone(err_ok)
            self.assertEqual(res_ok.returncode, 0)
            self.assertEqual(gate.RUN_BUDGET['calls'], 1)
            self.assertEqual(len(worker_calls), 1)


if __name__ == '__main__':
    unittest.main()
