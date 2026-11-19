import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from flow.learning import main, project_root
from learner.store import LearnerStore
from learner.contracts import ValidationError
from learner.feedback import consume_project_outcomes


class EasyOptinTests(unittest.TestCase):
    def test_lifecycle_isolation_and_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b, storage = root / 'a', root / 'b', root / 'store'
            a.mkdir(); b.mkdir()
            def call(repo, *args):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = main(['--repo', str(repo), '--storage-root', str(storage), '--json', *args])
                self.assertEqual(rc, 0)
                return json.loads(out.getvalue())
            self.assertEqual(call(a, 'status')['profiles'], [])
            first = call(a)
            pid = first['profile']['project_id']
            second = call(b)
            self.assertNotEqual(pid, second['profile']['project_id'])
            self.assertTrue(first['profile']['execution_paused'])
            self.assertFalse(first['profile']['export_policy']['allow_public_concept_queries'])
            call(a, 'pause')
            before = call(a)['profile']
            self.assertFalse(before['learning_enabled'])
            self.assertEqual(before, call(a)['profile'])
            self.assertEqual(call(a, 'status')['profiles'], [before])
            with LearnerStoreContext(storage) as store:
                other = store.get_profile(second['profile']['project_id'])
                from learner.contracts import ProjectProfile
                with self.assertRaises(ValidationError):
                    store.register_profile(ProjectProfile.from_dict(other.to_dict() | {'project_id': 'collision'}))
            result = call(a, 'unregister')
            self.assertEqual(result['retention'], 'purge_private')
            self.assertEqual(call(a, 'status')['profiles'], [])
            self.assertEqual(len(call(b, 'status')['profiles']), 1)

    def test_nested_git_and_plain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            nested = root / 'nested'; nested.mkdir()
            self.assertEqual(project_root(nested), root)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(project_root(Path(tmp)), Path(tmp))

    def test_bridge_pause_and_project_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / '.flow').mkdir()
            (root / '.flow/verified-outcomes.jsonl').write_text(json.dumps({'project_id': 'other', 'verified': True}) + '\n')
            from flow.learning import generic_profile, import_learner
            profile = generic_profile(import_learner(), root)
            from learner.contracts import ProjectProfile
            profile = ProjectProfile.from_dict(profile.to_dict() | {'provenance': {'outcome_bridge': True}})
            with LearnerStoreContext(root / 'store') as store:
                store.register_profile(profile)
                with self.assertRaises(ValidationError):
                    consume_project_outcomes(store, profile)
                paused = store.set_learning_enabled(profile.project_id, False)
                self.assertEqual(consume_project_outcomes(store, paused), 0)


class LearnerStoreContext:
    def __init__(self, path):
        self.store = LearnerStore(path)
    def __enter__(self):
        return self.store
    def __exit__(self, *args):
        self.store.close()

class RankingTests(unittest.TestCase):
    def test_verified_threshold_isolation_and_reversal(self):
        from types import SimpleNamespace as N
        from learner.feedback import track_record
        a = N(project_id='a', goal_ref='quality', capability_ref='tests',
              model_inferences=[{'step': 'verified_proposal', 'citations_valid': True}], verified_observations=[])
        outcomes = []
        class Store:
            def list_outcomes(self, pid):
                return [o for o in outcomes if o.project_id == pid]
            def get_assessment(self, aid):
                return a
        store = Store()
        def add(ref, status):
            a.verified_observations.append(dict(outcome_id=ref, project_id='a', verification_provenance='owner_attestation'))
            outcomes.append(N(outcome_id=ref, project_id='a', assessment_id='x', external_experiment_ref=ref,
                              status=status, baseline_summary='baseline', outcome_summary='observed'))
        add('one', 'accepted')
        self.assertEqual(track_record(store, 'a', 'quality', 'tests'), 0)
        add('two', 'accepted')
        self.assertEqual(track_record(store, 'a', 'quality', 'tests'), 1)
        self.assertEqual(track_record(store, 'b', 'quality', 'tests'), 0)
        add('three', 'rejected'); add('four', 'rejected')
        self.assertEqual(track_record(store, 'a', 'quality', 'tests'), 0)
        a.model_inferences = []
        self.assertEqual(track_record(store, 'a', 'quality', 'tests'), 0)
