"""Offline regressions for local registration, outcome import, and learned priority."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import contextlib
import io
import subprocess
import sys
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from flow.learning import generic_profile, import_learner, main, project_root
from learner.assessment import assess_pending_work, get_recommendations, run_due
from learner.model_dispatch import FixtureModelRunner
from learner.contracts import BudgetPolicy, DispatchBlockedError, ValidationError
from learner.feedback import consume_project_outcomes, project_scores
from learner.pipeline import run_intake
from learner.store import LearnerStore
from learner.tests.fixtures import build_standard_transport, make_profile


class OptinRepairs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 1794800000.
        self.store = LearnerStore(self.root / 'store', clock=lambda: self.now)
        self.a = self.root / 'a'; self.a.mkdir()
        self.b = self.root / 'b'; self.b.mkdir()
        self.p = self.store.register_profile(make_profile(root_path=str(self.a)))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def assessment(self, goal='quality', proposed=True):
        if not self.store.list_source_artifacts():
            run_intake(self.store, 'opportunity-led', build_standard_transport())
        used = {a.source_id for a in self.store.list_assessments(self.p.project_id) if a.goal_ref in ('quality', 'other')}
        art = next(a for a in self.store.list_source_artifacts() if a.source_id not in used)
        a, _ = self.store.ensure_assessment_for_artifact(self.p, art)
        self.now += 1
        return self.store.transition_assessment(a.assessment_id,
            lifecycle_state='proposed' if proposed else 'matched',
            decision='proposed' if proposed else 'qualify', last_completed_step='triage_qualified',
            reason='fixture', goal_ref=goal, capability_ref='tests',
            model_inferences=[{'step': 'verified_proposal', 'citations_valid': True, 'title': goal}])

    def row(self, a, ref='experiment-1', **updates):
        return dict(project_id=self.p.project_id, assessment_id=a.assessment_id,
                    external_experiment_ref=ref, status='accepted', baseline_summary='baseline',
                    outcome_summary='measured', evaluator='owner', verified=True) | updates

    def write(self, rows):
        path = self.a / '.flow' / 'verified-outcomes.jsonl'
        path.parent.mkdir(exist_ok=True)
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        return path

    def test_reregister_both_retention_policies_and_reopen(self):
        for policy in ('purge_private', 'retain_redacted'):
            with self.subTest(policy=policy):
                self.store.unregister_profile(self.p.project_id, retention=policy)
                self.store.close()
                self.store = LearnerStore(self.root / 'store')
                with self.assertRaisesRegex(ValidationError, 'different repository'):
                    self.store.register_profile(replace(self.p, root_path=str(self.b)))
                saved = self.store.register_profile(self.p)
                self.assertGreater(saved.revision, self.p.revision)
                self.p = saved

    def test_revision_cannot_move_identity_but_alias_and_destination_registration_work(self):
        with self.assertRaisesRegex(ValidationError, 'cannot change'):
            self.store.revise_profile(self.p.project_id, {'root_path': str(self.b)})
        self.store.revise_profile(self.p.project_id, {'root_path': str(self.a / '..' / 'a')})
        self.store.unregister_profile(self.p.project_id)
        dest = self.store.register_profile(replace(self.p, project_id='destination', root_path=str(self.b)))
        self.assertEqual(dest.root_path, str(self.b))

    def test_simultaneous_registration_is_atomic_across_stores(self):
        def register(i):
            store = LearnerStore(self.root / 'store')
            try:
                store.register_profile(make_profile(project_id=f'b-{i}', root_path=str(self.b)))
                return True
            except ValidationError:
                return False
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(register, range(8))), 1)

    def test_actual_flow_grant_cannot_exempt_existing_generic_profile_or_opt_out_flag(self):
        resolver = mock.patch('learner.store._trusted_flow_source', return_value=self.root / 'flow-source')
        resolver.start()
        self.addCleanup(resolver.stop)
        owner = self.store.register_profile(replace(make_profile('flow', root_path=str(self.root / 'flow-source')), adapter='flow'))
        self.store.revise_profile(self.p.project_id, {'budget': BudgetPolicy(daily_calls=1).to_dict(), 'provenance': {}})
        self.store.set_trial_grant(unlimited=True, authority='owner', scope='flow_learning')
        def reserve(pid, key):
            return self.store.reserve_dispatch(project_id=pid, idempotency_key=key,
                scope_type='model_call', budget_bucket='daily_calls', cap_override=100)
        # Existing Flow grants predate the explicit owner field.
        legacy_grant = self.store.get_setting('trial:grant')
        legacy_grant.pop('owner_project_id')
        self.store.set_setting('trial:grant', legacy_grant)
        with self.assertRaises(DispatchBlockedError):
            self.store.reserve_dispatch(project_id=self.p.project_id, idempotency_key='lower-cap',
                scope_type='model_call', budget_bucket='daily_calls', cap_override=0)
        reserve(self.p.project_id, 'one')
        self.assertEqual(self.store.get_setting('trial:grant')['owner_project_id'], owner.project_id)
        with self.assertRaises(DispatchBlockedError):
            reserve(self.p.project_id, 'two')
        for i in range(12):
            reserve(owner.project_id, f'flow-{i}')
        impostor = self.store.register_profile(replace(owner, project_id='impostor', root_path=str(self.b), budget=BudgetPolicy(daily_calls=0)))
        with self.assertRaises(DispatchBlockedError):
            reserve(impostor.project_id, 'impostor')

    def test_shared_dispatches_charge_non_owner_budget_without_changing_owner_grant(self):
        resolver = mock.patch('learner.store._trusted_flow_source', return_value=self.root / 'flow-source')
        resolver.start()
        self.addCleanup(resolver.stop)
        owner = self.store.register_profile(replace(make_profile('flow', root_path=str(self.root / 'flow-source')), adapter='flow'))
        self.store.revise_profile(self.p.project_id, {'budget': BudgetPolicy(daily_calls=1).to_dict(), 'provenance': {}})
        self.store.set_trial_grant(unlimited=True, authority='owner', scope='flow_learning')
        def reserve(targets, key):
            return self.store.reserve_dispatch(project_id='__shared__', target_project_ids=targets,
                idempotency_key=key, scope_type='shared_triage', budget_bucket='daily_calls')
        reserve([owner.project_id, self.p.project_id], 'mixed')
        with self.assertRaises(DispatchBlockedError):
            reserve([self.p.project_id], 'generic-second')
        with self.assertRaises(DispatchBlockedError):
            self.store.reserve_dispatch(project_id=self.p.project_id, idempotency_key='direct-second',
                scope_type='model_call', budget_bucket='daily_calls')
        for i in range(12):
            reserve([owner.project_id], f'owner-{i}')

    def test_unverified_outcomes_do_not_rank_but_bridge_changes_returned_order(self):
        a = self.assessment()
        other = self.assessment('other')
        for i in range(2):
            row = self.row(a, f'unverified-{i}'); row.pop('verified')
            self.store.record_outcome_reference(**row)
        def touch_other():
            self.now += 1
            self.store.transition_assessment(other.assessment_id, lifecycle_state='proposed',
                decision='proposed', last_completed_step='fixture', reason='newer recommendation')
        touch_other()
        self.assertEqual(project_scores(self.store, self.p.project_id), {})
        self.assertEqual(get_recommendations(self.store, project_id=self.p.project_id)['recommendations'][0]['assessment_id'], other.assessment_id)
        self.write([self.row(a, 'one'), self.row(a, 'two')])
        self.assertEqual(consume_project_outcomes(self.store, self.p), 2)
        self.assertEqual(consume_project_outcomes(self.store, self.p), 0)
        touch_other()
        result = get_recommendations(self.store, project_id=self.p.project_id)
        self.assertEqual(result['recommendations'][0]['assessment_id'], a.assessment_id)
        self.assertEqual(result['recommendations'][0]['verified_outcome_rank'], 1)
        self.store.set_learning_enabled(self.p.project_id, False)
        self.assertEqual(get_recommendations(self.store, project_id=self.p.project_id)['recommendations'][0]['assessment_id'], other.assessment_id)

    def test_verified_scores_prioritize_comparable_pending_assessments_once_per_project(self):
        low = self.assessment('other', proposed=False)
        high = self.assessment('quality', proposed=False)
        self.write([self.row(high, 'one'), self.row(high, 'two')])
        consume_project_outcomes(self.store, self.p)
        # Remove unrelated seen fixture items so only comparable matched work remains.
        for a in self.store.list_assessments(self.p.project_id):
            if a.assessment_id not in (low.assessment_id, high.assessment_id):
                self.store.transition_assessment(a.assessment_id, lifecycle_state='closed',
                    decision='reject', last_completed_step='fixture', reason='fixture')
        with mock.patch('learner.feedback.project_scores', wraps=project_scores) as scores, mock.patch(
                'learner.assessment.fetch_primary_evidence', side_effect=RuntimeError('stop at first selected fetch')) as fetch:
            with self.assertRaisesRegex(RuntimeError, 'first selected fetch'):
                assess_pending_work(self.store, build_standard_transport(), FixtureModelRunner())
            self.assertEqual(fetch.call_args.args[0].source_id, high.source_id)
            scores.assert_called_once_with(self.store, self.p.project_id)

    def test_conflicting_batch_is_rejected_before_any_write(self):
        a = self.assessment()
        self.write([self.row(a, 'new'), self.row(a, 'collision'), self.row(a, 'collision', status='rejected')])
        with self.assertRaisesRegex(ValidationError, 'Conflicting'):
            consume_project_outcomes(self.store, self.p)
        self.assertEqual(self.store.list_outcomes(), [])
        self.write([self.row(a, 'existing')]); consume_project_outcomes(self.store, self.p)
        self.write([self.row(a, 'new'), self.row(a, 'existing', evaluator='different')])
        with self.assertRaises(ValidationError):
            consume_project_outcomes(self.store, self.p)
        self.assertEqual(len(self.store.list_outcomes()), 1)

    def test_limits_and_project_validation(self):
        a = self.assessment()
        path = self.write([self.row(a)])
        for patch, value in [('MAX_OUTCOME_BYTES', 2), ('MAX_OUTCOME_RECORDS', 0)]:
            with mock.patch('learner.feedback.'+patch, value), self.assertRaises(ValidationError):
                consume_project_outcomes(self.store, self.p)
        path.write_text(json.dumps(self.row(a, project_id='wrong')))
        with self.assertRaises(ValidationError):
            consume_project_outcomes(self.store, self.p)
        self.assertEqual(self.store.list_outcomes(), [])

    def test_two_project_due_continues_on_broken_import_and_reports_each_failure(self):
        a = self.assessment()
        good = self.store.register_profile(make_profile('good', root_path=str(self.b)))
        good_a, _ = self.store.ensure_assessment_for_artifact(good, self.store.list_source_artifacts()[0])
        good_path = self.b / '.flow' / 'verified-outcomes.jsonl'; good_path.parent.mkdir()
        good_path.write_text(json.dumps(self.row(good_a, project_id=good.project_id)))
        path = self.write([self.row(a)])
        for broken in ('[' * 2000 + '0' + ']' * 2000, '{', json.dumps(self.row(a, project_id='wrong')), '\n'.join(json.dumps(r) for r in [self.row(a), self.row(a, status='rejected')]), None):
            if broken is None:
                path.unlink()
                path.symlink_to(self.a, target_is_directory=True)
                with self.assertRaisesRegex(ValidationError, 'regular file'):
                    consume_project_outcomes(self.store, self.p)
            else:
                path.write_text(broken)
            with mock.patch('learner.assessment._collect_and_ingest_mode', return_value={'status': 'ok', 'coverage': []}) as collect, mock.patch('learner.assessment.assess_pending_work', return_value={'status': 'ok'}) as assess:
                self.store.set_setting('schedule:last_run', {})
                result = run_due(self.store, build_standard_transport(), model_runner=object())
                self.assertIn(self.p.project_id, result['outcome_import_errors'])
                self.assertEqual(result['status'], 'degraded')
                self.assertIn(good.project_id, assess.call_args.kwargs['project_ids'])
                self.assertTrue(any(c.kwargs.get('project_id') == good.project_id for c in collect.call_args_list))
        self.assertEqual(len(self.store.list_outcomes(good.project_id)), 1)
        with mock.patch('learner.feedback.os.open', side_effect=PermissionError('unreadable')), mock.patch('learner.assessment.assess_pending_work', return_value={'status': 'ok'}):
            result = run_due(self.store, build_standard_transport(), model_runner=object())
            self.assertIn(self.p.project_id, result['outcome_import_errors'])

    def test_outcome_import_without_secure_platform_support(self):
        path = self.a / '.flow' / 'verified-outcomes.jsonl'
        for missing in ('O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK', 'dir_fd'):
            with self.subTest(missing=missing), contextlib.ExitStack() as stack:
                if missing == 'dir_fd':
                    stack.enter_context(mock.patch('learner.feedback.os.supports_dir_fd', set(), create=True))
                else:
                    stack.enter_context(mock.patch.object(os, missing, create=True))
                    delattr(os, missing)
                self.assertEqual(consume_project_outcomes(self.store, self.p), 0)
                path.parent.mkdir(exist_ok=True)
                path.write_text('')
                try:
                    with self.assertRaisesRegex(ValidationError, 'unsupported on this platform'):
                        consume_project_outcomes(self.store, self.p)
                finally:
                    path.unlink()

    def test_outcome_symlinks_preserve_local_targets_and_reject_escapes(self):
        a = self.assessment()
        path = self.write([self.row(a)])
        target = self.a / 'local-outcomes'
        path.rename(target)
        path.symlink_to(target)
        self.assertEqual(consume_project_outcomes(self.store, self.p), 1)
        path.unlink()
        outside = self.b / 'outside-outcomes'
        outside.write_text(target.read_text())
        path.symlink_to(outside)
        with self.assertRaises(ValidationError):
            consume_project_outcomes(self.store, self.p)

    def test_fifo_import_does_not_block_other_project(self):
        good = self.store.register_profile(make_profile('good', root_path=str(self.b)))
        path = self.a / '.flow' / 'verified-outcomes.jsonl'
        path.parent.mkdir()
        os.mkfifo(path)
        with mock.patch('learner.assessment._collect_and_ingest_mode', return_value={'status': 'ok', 'coverage': []}) as collect, mock.patch('learner.assessment.assess_pending_work', return_value={'status': 'ok'}) as assess:
            result = run_due(self.store, build_standard_transport(), model_runner=object())
            self.assertIn(self.p.project_id, result['outcome_import_errors'])
            self.assertIn(good.project_id, assess.call_args.kwargs['project_ids'])
            self.assertTrue(any(c.kwargs.get('project_id') == good.project_id for c in collect.call_args_list))

    def test_flow_identity_and_missing_grant_in_fresh_stores(self):
        from learner.store import _trusted_flow_source
        source = Path(__file__).resolve().parents[1]
        self.assertEqual(_trusted_flow_source(), source)
        for root in (source, self.b):
            with self.subTest(root=root):
                store = LearnerStore(self.root / ('trusted-db' if root == source else 'impostor-db'))
                try:
                    owner = store.register_profile(replace(make_profile('flow', root_path=str(root)), adapter='flow', budget=BudgetPolicy(daily_calls=0)))
                    with self.assertRaises(DispatchBlockedError):
                        store.reserve_dispatch(project_id='flow', idempotency_key='no-grant', scope_type='model_call', budget_bucket='daily_calls')
                    if root == source:
                        store.set_trial_grant(unlimited=True, authority='owner', scope='flow_learning')
                        store.reserve_dispatch(project_id='flow', idempotency_key='granted', scope_type='model_call', budget_bucket='daily_calls')
                    else:
                        with self.assertRaises(ValidationError):
                            store.set_trial_grant(unlimited=True, authority='owner', scope='flow_learning')
                finally:
                    store.close()

    def test_installed_source_uses_only_valid_release_manifest(self):
        from learner.store import _trusted_flow_source
        home = self.root / 'home'
        release = home / '.local/share/flow/v1'
        module = release / 'runtime/learner/store.py'
        module.parent.mkdir(parents=True)
        module.touch()
        manifest = release / 'manifest.json'
        metadata = {'format': 1, 'version': 'v1', 'source': str(self.a)}
        with mock.patch('learner.store.__file__', str(module)), mock.patch('learner.store.Path.home', return_value=home):
            self.assertIsNone(_trusted_flow_source())
            manifest.write_text(json.dumps(metadata))
            self.assertEqual(_trusted_flow_source(), self.a)
            for changes in ({'version': 'wrong'}, {'source': 'relative'}, {'format': 2}):
                manifest.write_text(json.dumps(metadata | changes))
                self.assertIsNone(_trusted_flow_source())
        fake = self.b / 'runtime/learner/store.py'
        fake.parent.mkdir(parents=True)
        fake.touch()
        (self.b / 'manifest.json').write_text(json.dumps(metadata))
        with mock.patch('learner.store.__file__', str(fake)):
            self.assertIsNone(_trusted_flow_source())

    def test_cli_plain_onboard_preserves_explicit_inputs_and_pause_display(self):
        def call(*args):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = main(['--repo', str(self.b), '--storage-root', str(self.root / 'store'), *args])
            return rc, stdout.getvalue(), stderr.getvalue()
        with mock.patch('flow.learning.subprocess.run', side_effect=FileNotFoundError):
            rc, text, _ = call('onboard', '--goal', 'Explicit goal', '--mission', 'Explicit mission', '--json')
        self.assertEqual(rc, 0)
        profile = json.loads(text)['profile']
        self.assertEqual(profile['goals'], ['Explicit goal'])
        self.assertEqual(profile['mission'], 'Explicit mission')
        self.assertEqual(call('pause')[0], 0)
        rc, text, _ = call()
        self.assertEqual(rc, 0)
        self.assertIn('b: learning paused', text)
        self.assertIn('verified outcomes=0', text)
        self.assertNotIn('due enabled', text)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--repo', str(self.root / 'missing'), '--storage-root', str(self.root / 'store')]), 1)

    def test_simultaneous_one_command_requests_return_one_registration(self):
        def call(_):
            return subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'flow/learning.py'),
                '--repo', str(self.b), '--storage-root', str(self.root / 'store'), '--json'],
                capture_output=True, text=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(call, range(4)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        payloads = [json.loads(result.stdout) for result in results]
        self.assertEqual(sum(p['registered'] for p in payloads), 1)
        self.assertEqual(len({p['profile']['project_id'] for p in payloads}), 1)

    def test_plain_docs_stay_private_curated_permissions_survive_and_explicit_inputs_win(self):
        (self.b / 'README.md').write_text('# Local\nPrivate mission.\n## Goals\n- Improve replay\n## Capabilities\n- Fixture runner\n')
        with mock.patch('flow.learning.subprocess.run', side_effect=FileNotFoundError):
            self.assertEqual(project_root(self.b), self.b)
        p = generic_profile(import_learner(), self.b)
        self.assertEqual(p.mission, 'Private mission.')
        self.assertEqual(p.goals, ('Improve replay',))
        self.assertEqual(p.existing_capabilities, ('Fixture runner',))
        self.assertFalse(p.export_policy.allow_public_concept_queries)
        self.assertEqual(p.public_concepts, ())
        (self.b / '.flow').mkdir()
        (self.b / '.flow/learner-profile.json').write_text(json.dumps({'root_path': str(self.b), 'public_concepts': ['evaluation harness'], 'export_policy': {'private_retention': 'retain_redacted', 'allow_public_concept_queries': True}}))
        curated = generic_profile(import_learner(), self.b, goals=['Explicit goal'], mission_override='Explicit mission')
        self.assertEqual(curated.export_policy.private_retention, 'retain_redacted')
        self.assertTrue(curated.export_policy.allow_public_concept_queries)
        self.assertEqual(curated.public_concepts, ('evaluation harness',))
        self.assertEqual(curated.goals, ('Explicit goal',))
        self.assertEqual(curated.mission, 'Explicit mission')
        with self.assertRaises(ValueError):
            project_root(self.b / 'missing')
