"""Offline local release tests: every destination lives under a temporary home."""
import importlib.util
import json
import os
import shutil
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


if __name__ == '__main__':
    unittest.main()
