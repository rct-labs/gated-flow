"""Offline local release tests: every destination lives under a temporary home."""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'local_release', Path(__file__).resolve().parents[1] / 'flow/release.py')
        self.release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.release)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.source = Path(self.tmp.name) / 'source'
        self.old = self.home / '.local/share/flow/old'
        for root, value in ((self.source, 'new'), (self.old, 'old')):
            for folder in ('flow', 'gate', 'docs'):
                (root / folder).mkdir(parents=True)
            (root / 'flow/flow.py').write_text('from pathlib import Path\nprint(Path(__file__).resolve().parents[1])\n')
            (root / 'gate/gate.py').write_text('print("offline help")\n')
            (root / 'gate/model_policy.py').write_text('policy bytes\n')
            (root / 'README.md').write_text(value)
            for name in self.release.SKILLS:
                skill = root / 'skills' / name
                skill.mkdir(parents=True)
                (skill / 'SKILL.md').write_text(f'---\nname: {name}\n---\n{value}\n')
        for name in self.release.SKILLS:
            canonical = self.home / '.agents/skills' / name
            canonical.mkdir(parents=True)
            (canonical / 'SKILL.md').write_bytes((self.old / 'skills' / name / 'SKILL.md').read_bytes())
            for agent in ('.codex', '.claude'):
                alias = self.home / agent / 'skills' / name
                alias.parent.mkdir(parents=True, exist_ok=True)
                alias.symlink_to(canonical)
        self.shim = self.home / '.local/bin/flow'
        self.shim.parent.mkdir(parents=True)
        self.shim.write_bytes(self.release.shim_bytes(self.old))
        self.shim.chmod(0o755)
        self.before = self.release.inspect_install(self.home)
        self.target = self.home / '.local/share/flow/new'

    def stage(self):
        return self.release.stage(self.source, self.home, 'new', self.release.digest(self.before))

    def test_stage_is_inert_then_activate_and_rollback_exact_bytes(self):
        self.stage()
        self.assertEqual(self.release.inspect_install(self.home), self.before)
        self.release.activate(self.home, 'new')
        self.assertEqual(self.shim.read_bytes(), self.release.shim_bytes(self.target / 'runtime'))
        self.assertEqual((self.home / '.agents/skills/flow/SKILL.md').read_bytes(),
                         (self.source / 'skills/flow/SKILL.md').read_bytes())
        for agent in ('.codex', '.claude'):
            self.assertTrue((self.home / agent / 'skills/flow').is_symlink())
        self.release.rollback(self.home, 'new')
        self.assertEqual(self.release.inspect_install(self.home), self.before)
        self.assertTrue((self.old / 'README.md').exists())
        self.assertEqual(json.loads((self.target / 'state.json').read_text())['phase'], 'rolled_back')

    def test_partial_failure_restores_installation(self):
        self.stage()
        replace = self.release.replace_skill
        calls = []
        def fail_second(*args):
            calls.append(args)
            if len(calls) == 2:
                raise OSError('simulated disk failure')
            return replace(*args)
        with mock.patch.object(self.release, 'replace_skill', side_effect=fail_second):
            with self.assertRaises(OSError):
                self.release.activate(self.home, 'new')
        self.assertEqual(self.release.inspect_install(self.home), self.before)

    def test_interrupted_activation_can_be_rolled_back(self):
        self.stage()
        with mock.patch.object(self.release, 'replace_skill', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.release.activate(self.home, 'new')
        self.release.rollback(self.home, 'new')
        self.assertEqual(self.release.inspect_install(self.home), self.before)

    def test_interrupt_between_directory_renames_recovers_missing_skill(self):
        self.stage()
        rename = os.rename
        def interrupt_pending(src, dst):
            if str(src).endswith('flow.pending'):
                raise KeyboardInterrupt()
            return rename(src, dst)
        with mock.patch.object(self.release.os, 'rename', side_effect=interrupt_pending):
            with self.assertRaises(KeyboardInterrupt):
                self.release.activate(self.home, 'new')
        self.assertFalse((self.home / '.agents/skills/flow').exists())
        self.release.rollback(self.home, 'new')
        self.assertEqual(self.release.inspect_install(self.home), self.before)

    def test_interrupted_rollback_is_resumable(self):
        self.stage()
        self.release.activate(self.home, 'new')
        rename = os.rename
        def interrupt_pending(src, dst):
            if str(src).endswith('flow.pending'):
                raise KeyboardInterrupt()
            return rename(src, dst)
        with mock.patch.object(self.release.os, 'rename', side_effect=interrupt_pending):
            with self.assertRaises(KeyboardInterrupt):
                self.release.rollback(self.home, 'new')
        self.release.rollback(self.home, 'new')
        self.assertEqual(self.release.inspect_install(self.home), self.before)

    def test_prior_runtime_drift_and_changed_alias_refused(self):
        self.stage()
        prior = self.old / 'README.md'
        prior.write_text('changed old runtime')
        with self.assertRaises(ValueError):
            self.release.activate(self.home, 'new')
        prior.write_text('old')
        self.release.activate(self.home, 'new')
        alias = self.home / '.claude/skills/flow'
        alias.unlink()
        alias.symlink_to(self.source)
        with self.assertRaises(ValueError):
            self.release.rollback(self.home, 'new')
        self.assertEqual(self.shim.read_bytes(), self.release.shim_bytes(self.target / 'runtime'))

    def test_actual_checkout_smoke_is_offline_and_isolated(self):
        self.release.smoke(Path(__file__).resolve().parents[1])

    def test_changed_destination_refused_before_activation_and_rollback(self):
        self.stage()
        skill = self.home / '.agents/skills/flow/SKILL.md'
        skill.write_text('unrelated edit')
        with self.assertRaises(ValueError):
            self.release.activate(self.home, 'new')
        self.assertEqual(skill.read_text(), 'unrelated edit')
        skill.write_bytes((self.old / 'skills/flow/SKILL.md').read_bytes())
        self.release.activate(self.home, 'new')
        skill.write_text('post-release edit')
        with self.assertRaises(ValueError):
            self.release.rollback(self.home, 'new')
        self.assertEqual(skill.read_text(), 'post-release edit')

    def test_source_and_staged_drift_are_refused(self):
        self.stage()
        path = self.source / 'README.md'
        path.write_text('changed after review')
        with self.assertRaises(ValueError):
            self.release.activate(self.home, 'new')
        path.write_text('new')
        (self.target / 'runtime/README.md').write_text('tampered copy')
        with self.assertRaises(ValueError):
            self.release.activate(self.home, 'new')
        self.assertEqual(self.release.inspect_install(self.home), self.before)

    def test_model_policy_must_match_existing_runtime(self):
        (self.source / 'gate/model_policy.py').write_text('changed policy')
        with self.assertRaises(ValueError):
            self.stage()
        self.assertFalse(self.target.exists())

    def test_unowned_shim_alias_or_skill_refused(self):
        for path, content in ((self.shim, b'#!/bin/sh\necho unrelated\n'),
                              (self.home / '.agents/skills/flow/SKILL.md', b'name: somebody-else')):
            old = path.read_bytes()
            path.write_bytes(content)
            with self.assertRaises(ValueError):
                self.stage()
            path.write_bytes(old)
        alias = self.home / '.codex/skills/flow'
        alias.unlink()
        alias.symlink_to(self.source)
        with self.assertRaises(ValueError):
            self.stage()

    def test_explicit_baseline_and_unique_safe_version_required(self):
        with self.assertRaises(ValueError):
            self.release.stage(self.source, self.home, 'new', 'wrong hash')
        for version in ('../escape', 'old', '.', ''):
            with self.assertRaises(ValueError):
                self.release.stage(self.source, self.home, version, self.release.digest(self.before))
        self.stage()
        with self.assertRaises(ValueError):
            self.stage()

    def test_failed_smoke_does_not_touch_live_installation(self):
        self.stage()
        with mock.patch.object(self.release, 'smoke', side_effect=ValueError('bad smoke')):
            with self.assertRaises(ValueError):
                self.release.activate(self.home, 'new')
        self.assertEqual(self.release.inspect_install(self.home), self.before)

    def test_symlink_source_and_parent_destination_refused(self):
        (self.source / 'docs/external').symlink_to(self.old / 'README.md')
        with self.assertRaises(ValueError):
            self.stage()
        (self.source / 'docs/external').unlink()
        canonical = self.home / '.agents/skills/flow'
        canonical.rename(canonical.with_name('saved'))
        canonical.symlink_to(canonical.with_name('saved'))
        with self.assertRaises(ValueError):
            self.stage()

    def test_damaged_abandoned_runtime_and_source_do_not_block_rollback(self):
        self.stage()
        self.release.activate(self.home, 'new')
        (self.target / 'runtime/README.md').write_text('damaged active runtime')
        (self.target / 'runtime/gate/gate.py').unlink()
        (self.source / 'README.md').write_text('source changed after activation')
        self.release.rollback(self.home, 'new')
        self.assertEqual(self.release.inspect_install(self.home), self.before)

    def test_recovery_refuses_tampered_runtime_skills_and_prior_runtime(self):
        for relative in ('runtime/skills/flow/SKILL.md', '../old/README.md'):
            with self.subTest(relative=relative):
                self.stage()
                self.release.activate(self.home, 'new')
                path = self.target / relative
                original = path.read_bytes()
                path.write_text('tampered recovery prerequisite')
                shim = self.shim.read_bytes()
                with self.assertRaises(ValueError):
                    self.release.rollback(self.home, 'new')
                self.assertEqual(self.shim.read_bytes(), shim)
                path.write_bytes(original)
                self.release.rollback(self.home, 'new')
                shutil.rmtree(self.target)

    def test_drifted_backup_refused_without_changes(self):
        self.stage()
        self.release.activate(self.home, 'new')
        (self.target / 'backup/skills/flow/SKILL.md').write_text('bad backup')
        after = self.release.inspect_install(self.home)
        with self.assertRaises(ValueError):
            self.release.rollback(self.home, 'new')
        self.assertEqual(self.release.inspect_install(self.home), after)


class LearningReleaseTransitionTests(unittest.TestCase):
    def setUp(self):
        repo_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('local_release', repo_root / 'flow/release.py')
        self.release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.release)
        self.repo_root = repo_root

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = Path(self.tmp.name)
        self.home = self.box / 'home'
        self.source = self.box / 'flow-checkout'
        self.prior_version = '20261114-budget-choices'
        self.old_release = self.home / '.local/share/flow' / self.prior_version
        self.old_runtime = self.old_release / 'runtime'

        # Seed prior immutable release + historical receipt, plus canonical skills + aliases + shim.
        for folder in ('flow', 'gate', 'docs'):
            (self.old_runtime / folder).mkdir(parents=True)
        (self.old_runtime / 'flow/flow.py').write_text('from pathlib import Path\nprint(Path(__file__).resolve().parents[1])\n')
        (self.old_runtime / 'gate/gate.py').write_text('print("old gate help")\n')
        (self.old_runtime / 'gate/model_policy.py').write_text('# old policy v0\n')
        (self.old_runtime / 'README.md').write_text('old runtime\n')
        for name in self.release.SKILLS:
            skill = self.old_runtime / 'skills' / name
            skill.mkdir(parents=True)
            (skill / 'SKILL.md').write_text(f'---\nname: {name}\n---\nold skill\n')
        (self.old_release / 'manifest.json').write_text('{"format": 1, "version": "20261114-budget-choices"}\n')
        (self.old_release / 'state.json').write_text('{"phase": "active"}\n')
        self.old_manifest_bytes = (self.old_release / 'manifest.json').read_bytes()

        for name in self.release.SKILLS:
            canonical = self.home / '.agents/skills' / name
            canonical.mkdir(parents=True)
            (canonical / 'SKILL.md').write_bytes((self.old_runtime / 'skills' / name / 'SKILL.md').read_bytes())
            for agent in ('.codex', '.claude'):
                alias = self.home / agent / 'skills' / name
                alias.parent.mkdir(parents=True, exist_ok=True)
                alias.symlink_to(canonical)

        self.shim = self.home / '.local/bin/flow'
        self.shim.parent.mkdir(parents=True)
        self.shim.write_bytes(self.release.shim_bytes(self.old_runtime))
        self.shim.chmod(0o755)
        self.before = self.release.inspect_install(self.home)

        # Build complete candidate git checkout with real learner + flow adapter + changed model_policy.py.
        (self.source / 'flow').mkdir(parents=True)
        (self.source / 'gate').mkdir(parents=True)
        (self.source / 'docs/work/ai-evolution-ideas').mkdir(parents=True)
        shutil.copy2(repo_root / 'flow/flow.py', self.source / 'flow/flow.py')
        shutil.copy2(repo_root / 'flow/learning.py', self.source / 'flow/learning.py')
        shutil.copytree(
            repo_root / 'learner',
            self.source / 'learner',
            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'tests'),
        )
        (self.source / 'gate/gate.py').write_text('print("new gate help")\n')
        (self.source / 'gate/model_policy.py').write_text('# new scoped policy v1\n')
        (self.source / 'README.md').write_text('new runtime\n')
        (self.source / 'docs/work/ai-evolution-ideas/release.md').write_text('# release spec\n')
        (self.source / 'docs/work/ai-evolution-ideas/spec.md').write_text('# main spec\n')
        for name in self.release.SKILLS:
            skill = self.source / 'skills' / name
            skill.mkdir(parents=True)
            (skill / 'SKILL.md').write_text(f'---\nname: {name}\n---\nnew skill\n')

        self._git('init', '-q')
        self._git('config', 'user.email', 'test@local')
        self._git('config', 'user.name', 'Test')
        self._git('add', '.')
        self._git('commit', '-q', '-m', 'candidate release')
        self.head = self._git('rev-parse', 'HEAD').strip()

    def _git(self, *args):
        res = subprocess.run(['git', '-C', str(self.source), *args], capture_output=True, text=True, check=True)
        return res.stdout

    def _transition(self, **overrides):
        prior_sha = hashlib.sha256((self.old_runtime / 'gate/model_policy.py').read_bytes()).hexdigest()
        cand_sha = hashlib.sha256((self.source / 'gate/model_policy.py').read_bytes()).hexdigest()
        data = {
            'package': 'ai-evolution-ideas',
            'repository': str(self.source.resolve()),
            'reviewed_revision': self.head,
            'target_version': '20261116-ai-evolution',
            'prior_release': self.prior_version,
            'prior_policy_sha256': prior_sha,
            'candidate_policy_sha256': cand_sha,
            'review_ref': '20261116-040810-review; final-delivery-review',
            'evaluation_ref': 'offline-gate-256-plus-il4',
        }
        data.update(overrides)
        return data

    def test_changed_policy_without_transition_or_unrelated_repo_refused(self):
        fp = self.release.digest(self.before)
        with self.assertRaisesRegex(ValueError, 'byte-identical'):
            self.release.stage(self.source, self.home, '20261116-ai-evolution', fp)
        with self.assertRaisesRegex(ValueError, 'unrelated repository'):
            self.release.stage(self.source, self.home, '20261116-ai-evolution', fp,
                               policy_transition=self._transition())
        self.assertFalse((self.home / '.local/share/flow/20261116-ai-evolution').exists())

    def test_exact_reviewed_transition_activates_imports_learner_and_rolls_back(self):
        fp = self.release.digest(self.before)
        with mock.patch.object(self.release, 'CANONICAL_REPO', self.source):
            target = self.release.stage(
                self.source, self.home, '20261116-ai-evolution', fp,
                policy_transition=self._transition(),
            )
            manifest = json.loads((target / 'manifest.json').read_text())
            self.assertEqual(manifest['prior_release'], self.prior_version)
            self.assertEqual(manifest['source_revision'], {'head': self.head, 'clean': True})
            self.assertTrue(manifest['compatibility']['learner_included'])
            self.assertEqual(manifest['references']['review_ref'], '20261116-040810-review; final-delivery-review')
            self.assertEqual(manifest['references']['evaluation_ref'], 'offline-gate-256-plus-il4')

            self.release.activate(self.home, '20261116-ai-evolution')
            runtime = target / 'runtime'
            self.assertEqual(self.shim.read_bytes(), self.release.shim_bytes(runtime))
            self.assertEqual((runtime / 'gate/model_policy.py').read_text(), '# new scoped policy v1\n')

            # Standalone learner runs from runtime cwd and outside checkout without PYTHONPATH or flow/gate imports.
            outside = self.box / 'outside'
            outside.mkdir()
            env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
            env['HOME'] = str(outside)
            run_cwd = subprocess.run(
                [sys.executable, '-B', '-m', 'learner', '--storage-root', str(outside / 'store'), 'status', '--json'],
                cwd=str(runtime), env=env, capture_output=True, text=True, check=True,
            )
            self.assertIn('"profiles"', run_cwd.stdout)

            # Rollback restores old shim pointing to prior policy runtime, old skills, and preserves prior release.
            self.release.rollback(self.home, '20261116-ai-evolution')
            self.assertEqual(self.release.inspect_install(self.home), self.before)
            self.assertEqual((self.old_release / 'manifest.json').read_bytes(), self.old_manifest_bytes)
            self.assertEqual((self.old_runtime / 'gate/model_policy.py').read_text(), '# old policy v0\n')

    def test_policy_transition_refuses_stale_inputs_dirty_tree_and_head_drift(self):
        fp = self.release.digest(self.before)
        bad_cases = [
            ('stale_revision', {'reviewed_revision': '0' * 40}),
            ('wrong_prior_digest', {'prior_policy_sha256': 'a' * 64}),
            ('wrong_candidate_digest', {'candidate_policy_sha256': 'b' * 64}),
            ('wrong_target', {'target_version': '20261116-other'}),
            ('wrong_prior_release', {'prior_release': '20261113-pstack'}),
            ('wrong_package', {'package': 'other-package'}),
            ('placeholder_review', {'review_ref': 'model_opinion_only'}),
            ('placeholder_eval', {'evaluation_ref': 'pending'}),
        ]
        with mock.patch.object(self.release, 'CANONICAL_REPO', self.source):
            for label, override in bad_cases:
                with self.subTest(case=label):
                    with self.assertRaises(ValueError):
                        self.release.stage(self.source, self.home, '20261116-ai-evolution', fp,
                                           policy_transition=self._transition(**override))

            # Missing or extra keys fail closed.
            missing = self._transition()
            del missing['evaluation_ref']
            with self.assertRaises(ValueError):
                self.release.stage(self.source, self.home, '20261116-ai-evolution', fp,
                                   policy_transition=missing)
            extra = self._transition()
            extra['wildcard'] = '*'
            with self.assertRaises(ValueError):
                self.release.stage(self.source, self.home, '20261116-ai-evolution', fp,
                                   policy_transition=extra)

            # Dirty tree refused.
            (self.source / 'README.md').write_text('dirty change\n')
            with self.assertRaisesRegex(ValueError, 'dirty'):
                self.release.stage(self.source, self.home, '20261116-ai-evolution', fp,
                                   policy_transition=self._transition())
            (self.source / 'README.md').write_text('new runtime\n')

            # Stage cleanly, then advance HEAD before activate -> activate refuses HEAD drift.
            self.release.stage(self.source, self.home, '20261116-ai-evolution', fp,
                               policy_transition=self._transition())
            (self.source / 'docs/work/ai-evolution-ideas/extra.md').write_text('post-stage commit\n')
            self._git('add', '.')
            self._git('commit', '-q', '-m', 'advance head')
            with self.assertRaises(ValueError):
                self.release.activate(self.home, '20261116-ai-evolution')
            self.assertEqual(self.release.inspect_install(self.home), self.before)

    def test_release_excludes_private_store_and_untracked_provider_logs(self):
        # Use unchanged policy to exercise default stage/activate exclusion path.
        (self.source / 'gate/model_policy.py').write_bytes((self.old_runtime / 'gate/model_policy.py').read_bytes())
        self._git('add', 'gate/model_policy.py')
        self._git('commit', '-q', '-m', 'match policy')

        (self.source / 'learner/.learner-data').mkdir()
        (self.source / 'learner/.learner-data/learner.sqlite3').write_text('private-db')
        (self.source / 'learner/private-cache.db').write_text('private-db')
        (self.source / 'gate/provider-run.log').write_text('secret prompt log')
        (self.source / 'learner/agy-events.jsonl').write_text('{"raw":"log"}\n')

        fp = self.release.digest(self.before)
        target = self.release.stage(self.source, self.home, '20261116-clean', fp)
        self.release.activate(self.home, '20261116-clean')
        runtime = target / 'runtime'
        self.assertFalse((runtime / 'learner/.learner-data').exists())
        self.assertFalse((runtime / 'learner/private-cache.db').exists())
        self.assertFalse((runtime / 'gate/provider-run.log').exists())
        self.assertFalse((runtime / 'learner/agy-events.jsonl').exists())
        self.assertTrue((runtime / 'learner/__init__.py').is_file())

    def test_cli_stage_with_policy_transition_file_and_rollback(self):
        transition_file = self.box / 'transition.json'
        transition_file.write_text(json.dumps(self._transition()), encoding='utf-8')
        fp = self.release.digest(self.before)
        with mock.patch.object(self.release, 'CANONICAL_REPO', self.source):
            for argv in (
                ['release.py', 'stage', '--home', str(self.home), '--source', str(self.source),
                 '--version', '20261116-ai-evolution', '--expected-install', fp,
                 '--policy-transition', str(transition_file)],
                ['release.py', 'activate', '--home', str(self.home), '--version', '20261116-ai-evolution'],
                ['release.py', 'rollback', '--home', str(self.home), '--version', '20261116-ai-evolution'],
            ):
                with mock.patch.object(sys, 'argv', argv):
                    self.release.main()
        self.assertEqual(self.release.inspect_install(self.home), self.before)


if __name__ == '__main__':
    unittest.main()
