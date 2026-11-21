"""Offline weekly inventory, last-good state and ordered account observations."""
from contextlib import closing
import json
import os
import shlex
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from learner import model_inventory as inventory


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.path=self.root/'inventory.json'
        env = patch.dict(os.environ, HOME=str(self.root / 'home'))
        env.start()
        self.addCleanup(env.stop)

    def test_human_inventory_status_hints_are_local_and_json_is_unchanged(self):
        import io
        from learner.cli import main
        self.deny(1)
        state = inventory.read_json(self.path)
        state['observations'][0]['reason'] = 'private raw refusal'
        state['subscription_accounts'] = {'claude:default-login': self.account()}
        self.path = self.root / "owner's inventory.json"
        self.path.write_text(json.dumps(state))
        before = self.path.read_bytes()
        with patch.dict(os.environ, FLOW_ACCOUNT_SCOPE_CLAUDE='default-login'), \
             patch.object(inventory.subprocess, 'Popen', side_effect=AssertionError('network/model')):
            for explicit_cache in (False, True):
                args = ['inventory', 'status']
                command = 'flow inventory recheck --model fable --route claude'
                if explicit_cache:
                    args += ['--cache', str(self.path)]
                    command += ' --cache ' + shlex.quote(str(self.path))
                with self.subTest(explicit_cache=explicit_cache), \
                     patch.object(inventory, 'inventory_path', return_value=self.path):
                    human, hint = io.StringIO(), io.StringIO()
                    with patch('sys.stdout', human), patch('sys.stderr', hint):
                        self.assertEqual(main(args), 0)
                    self.assertEqual(human.getvalue(), json.dumps(state, sort_keys=True) + '\n')
                    self.assertIn('Last verified tier: pro', hint.getvalue())
                    self.assertEqual(hint.getvalue().count(command), 1)
                    if not explicit_cache:
                        self.assertNotIn('--cache', hint.getvalue())
                    self.assertNotIn('private', hint.getvalue())
                    raw, errors = io.StringIO(), io.StringIO()
                    with patch('sys.stdout', raw), patch('sys.stderr', errors):
                        self.assertEqual(main(args + ['--json']), 0)
                    self.assertEqual(raw.getvalue(), json.dumps(state, indent=2, sort_keys=True) + '\n')
                    self.assertEqual(errors.getvalue(), '')
        self.assertEqual(self.path.read_bytes(), before)

    def test_inventory_status_without_holds_keeps_payload_and_has_no_hint(self):
        import io
        from learner.cli import main
        for state in ({}, {'observations': [], 'models': self.collect()}):
            self.path.write_text(json.dumps(state))
            for as_json in (False, True):
                with self.subTest(state=state, as_json=as_json):
                    output, errors = io.StringIO(), io.StringIO()
                    args = ['inventory', 'status', '--cache', str(self.path)]
                    if as_json:
                        args.append('--json')
                    with patch('sys.stdout', output), patch('sys.stderr', errors), \
                         patch.object(inventory.subprocess, 'Popen', side_effect=AssertionError('network/model')):
                        self.assertEqual(main(args), 0)
                    expected = json.dumps(state, indent=2, sort_keys=True) if as_json else json.dumps(state, sort_keys=True)
                    self.assertEqual(output.getvalue(), expected + '\n')
                    self.assertEqual(errors.getvalue(), '')

    def collect(self):
        return [{'route':'codex','model':'sol','version':'v1','source':'fixture catalog','observed_at':100}]

    def test_refresh_releases_lock_and_merges_latest_observations_and_hints(self):
        inventory.record_observation('codex', 'sol', 'success', path=self.path, observed_at=10)
        state = json.loads(self.path.read_text())
        state['hints'] = [{'source': 'old-google', 'provider': 'google'},
                          {'source': 'openai', 'provider': 'openai'}]
        self.path.write_text(json.dumps(state))
        def fetch_hints():
            self.assertEqual(inventory.refresh_due(path=self.path, now=101,
                collector=self.collect)['status'], 'deferred')
            inventory.record_observation('codex', 'sol', 'excluded', authority='owner',
                                         path=self.path, observed_at=200)
            inventory.record_observation('codex', 'sol', 'cooldown', until=400,
                                         path=self.path, observed_at=201)
            return ([{'source': 'openai', 'provider': 'openai', 'summary': 'new'}], ['google failed'])
        result = inventory.refresh_due(path=self.path, now=100, collector=self.collect,
                                       hint_collector=fetch_hints)
        self.assertEqual(result['status'], 'refreshed')
        state = json.loads(self.path.read_text())
        self.assertEqual({r['status'] for r in state['observations']}, {'excluded', 'cooldown'})
        self.assertEqual(len(state['hints']), 2)
        self.assertEqual(state['hints'][0]['provider'], 'google')

    def test_primary_markdown_hints_include_dated_readable_model_evidence(self):
        from learner.sources import TransportResponse
        from unittest.mock import Mock
        store = Mock()
        store.put_evidence.return_value = 'digest'
        transport = Mock()
        transport.fetch.return_value = TransportResponse(200,
            b'# Models\nGemini Carbon supports careful reasoning.\nGPT Sol supports coding.', {}, '')
        hints, errors = inventory.collect_hints(store, transport,
            sources=[inventory.PRIMARY_SOURCES[0]], now=100)
        self.assertFalse(errors)
        self.assertTrue(hints[0]['summary'])
        self.assertTrue(hints[0]['models'])
        self.assertEqual(hints[0]['observed_at'], 100)
        self.assertEqual(transport.fetch.call_args.kwargs['allowed_hosts'], ('antigravity.google',))

    def test_weekly_independent_expiry_and_failed_refresh_preserves_good(self):
        first=inventory.refresh_due(path=self.path,now=100,collector=self.collect)
        self.assertEqual(first['status'],'refreshed')
        before=self.path.read_bytes()
        with patch.object(inventory,'collect_local',side_effect=AssertionError('discovery on fresh cache')):
            self.assertEqual(inventory.refresh_due(path=self.path,now=101)['status'],'fresh')
        def failed():raise OSError('offline')
        self.assertEqual(inventory.refresh_due(path=self.path,now=100+inventory.WEEK,collector=failed)['status'],'stale_good')
        self.assertEqual(self.path.read_bytes(),before)

    def test_observations_survive_refresh_and_old_success_cannot_clear_limit(self):
        inventory.record_observation('agy','carbon','cooldown',observed_at=300,until=1000,path=self.path)
        inventory.record_observation('agy','carbon','success',observed_at=200,path=self.path)
        inventory.refresh_due(path=self.path,now=400,collector=self.collect)
        unavailable = inventory.routes.unavailable
        state=json.loads(self.path.read_text())
        self.assertIn('cooldown',unavailable('agy','carbon',state=state,now=500))
        inventory.record_observation('agy','carbon','excluded',authority='owner',observed_at=600,path=self.path)
        inventory.record_observation('agy','carbon','success',observed_at=700,path=self.path)
        self.assertIn('excluded',unavailable('agy','carbon',state=json.loads(self.path.read_text()),now=800))

    def test_account_profiles_and_event_time_are_independent(self):
        for scope, status, stamp in [('work', 'cooldown', 300), ('personal', 'success', 400), ('work', 'success', 200)]:
            inventory.record_observation('codex', 'sol', status, observed_at=stamp,
                                         until=1000 if status == 'cooldown' else None,
                                         scope=scope, path=self.path)
        state = json.loads(self.path.read_text())
        self.assertEqual(len(state['observations']), 2)
        self.assertEqual(inventory.routes.unavailable('codex', 'sol', state=state, now=500, scope='work'), 'account_cooldown')
        self.assertIsNone(inventory.routes.unavailable('codex', 'sol', state=state, now=500, scope='personal'))

    def test_lock_nonblocking_and_pid_start_identity(self):
        self.path.parent.mkdir(exist_ok=True)
        with inventory.refresh_lock(self.path) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(inventory.refresh_due(path=self.path,now=100,collector=self.collect)['status'],'busy')
        lock=self.path.with_suffix('.lock')
        lock.write_text(json.dumps({'pid':os.getpid(),'identity':'reused pid'}))
        self.assertEqual(inventory.refresh_due(path=self.path,now=100,collector=self.collect)['status'],'refreshed')

    def test_invalid_catalog_does_not_replace_cache(self):
        inventory.refresh_due(path=self.path,now=100,collector=self.collect)
        before=self.path.read_bytes()
        result=inventory.refresh_due(path=self.path,now=100+inventory.WEEK,collector=lambda:[{'model':'wrong'}])
        self.assertEqual(result['status'],'stale_good');self.assertEqual(self.path.read_bytes(),before)

    def test_failed_external_discovery_is_attempted_at_most_weekly(self):
        from unittest.mock import Mock
        discover = Mock(side_effect=OSError('catalog offline'))
        self.assertEqual(inventory.refresh_due(path=self.path, now=100, collector=discover)['status'], 'stale_good')
        self.assertEqual(inventory.refresh_due(path=self.path, now=101, collector=discover)['status'], 'deferred')
        self.assertEqual(discover.call_count, 1)
        inventory.refresh_due(path=self.path, now=100 + inventory.WEEK, collector=discover)
        self.assertEqual(discover.call_count, 2)

    def test_local_catalog_scopes_visibility_without_claiming_success(self):
        catalog=self.root/'models.json';catalog.write_text(json.dumps({'models':[{'slug':'sol','display_name':'Sol'}],'client_version':'1'}))
        rows=inventory.collect_local(catalogs={'codex':catalog},now=50)
        self.assertEqual(rows[0]['model'],'sol');self.assertEqual(rows[0]['route'],'codex')
        self.assertNotIn('success',json.dumps(rows))

    def test_owner_fable_api_and_offline_cli(self):
        with patch.dict(os.environ,HOME=str(self.root)):
            p=self.root/'.config/mini-model-routing/policy.json';p.parent.mkdir(parents=True)
            p.write_text(json.dumps({'design':{'claude':{'model':'current-fable','effort':'medium'}}}))
            inventory.exclude_current_fable('Owner reports current machine unavailable',path=self.path,now=100)
            row=json.loads(self.path.read_text())['observations'][0]
            self.assertEqual((row['model'],row['authority'],row['status']),('current-fable','owner','excluded'))

    def test_native_agy_catalog_is_bounded_and_weekly_only(self):
        executable=self.root/'agy'
        executable.write_text('#!'+sys.executable+'\nimport json\nprint(json.dumps({"models":[{"id":"gemini-current"}],"version":"1.2"}))\n')
        executable.chmod(0o755)
        with patch.dict(os.environ,PATH=str(self.root)+os.pathsep+os.environ.get('PATH','')):
            first=inventory.refresh_due(path=self.path,now=100,
                collector=lambda:inventory.collect_local(catalogs={},native_agy=True,now=100))
            self.assertEqual(first['status'],'refreshed')
            model=json.loads(self.path.read_text())['models'][0]
            self.assertEqual((model['route'],model['model'],model['version']),('agy','gemini-current','1.2'))
            with patch.object(inventory.subprocess,'Popen',side_effect=AssertionError('weekly discovery repeated')):
                self.assertEqual(inventory.refresh_due(path=self.path,now=101)['status'],'fresh')
            before=self.path.read_bytes()
            executable.write_text('#!'+sys.executable+'\nprint("x"*1048577)\n')
            result=inventory.refresh_due(path=self.path,now=100+inventory.WEEK,
                collector=lambda:inventory.collect_agy(now=100+inventory.WEEK))
            self.assertEqual(result['status'],'stale_good')
            self.assertEqual(self.path.read_bytes(),before)

    @unittest.skipIf(os.name == 'nt', 'POSIX native catalog fixture')
    def test_native_catalog_eof_before_exit_times_out_and_preserves_good_state(self):
        import time
        inventory.refresh_due(path=self.path, now=100, collector=self.collect)
        before = self.path.read_bytes()
        executable = self.root / 'agy'
        executable.write_text('#!' + sys.executable + '\nimport os,time\nos.close(1)\nos.close(2)\ntime.sleep(30)\n')
        executable.chmod(0o755)
        with patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ.get('PATH', '')), \
             patch.object(inventory, 'AGY_TIMEOUT_S', .2):
            start = time.monotonic()
            result = inventory.refresh_due(path=self.path, now=100 + inventory.WEEK,
                                           collector=inventory.collect_agy)
        self.assertEqual(result['status'], 'stale_good')
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_reviewed_version_specific_hints_preserve_provenance_and_are_advisory(self):
        from learner.store import LearnerStore
        from learner.sources import TransportResponse
        from unittest.mock import Mock
        with closing(LearnerStore(self.root/'evidence-store')) as store:
            transport=Mock()
            transport.fetch.return_value=TransportResponse(
                final_url='https://example.org/releases',status_code=200,headers={},body=b'current-v2 released',truncated=False)
            source={'url':'https://example.org/releases','authority':'secondary','as_of':'2026-11-20',
                    'model':'current-v2','provider':'fixture','reviewed_at':'2026-11-21','reviewed_by':'owner'}
            hints,errors=inventory.collect_hints(store,transport,sources=[source],now=100)
            self.assertFalse(errors)
            self.assertEqual(hints[0]['model'],'current-v2')
            self.assertEqual(hints[0]['reviewed_by'],'owner')
            self.assertNotIn('status',hints[0])
            bad=dict(source);bad.pop('reviewed_by')
            hints,errors=inventory.collect_hints(store,transport,sources=[bad],now=100)
            self.assertFalse(hints);self.assertTrue(errors)
            from dataclasses import replace
            transport.fetch.return_value = replace(transport.fetch.return_value, body=b'current-v20 released')
            hints, errors = inventory.collect_hints(store, transport, sources=[source], now=100)
            self.assertFalse(hints);self.assertTrue(errors)

    def test_reviewed_provider_hints_are_weekly_and_do_not_authorize_models(self):
        from learner.store import LearnerStore
        from learner.sources import FixtureTransport, TransportResponse
        from unittest.mock import Mock
        sources = [dict(url='https://example.org/' + provider, provider=provider,
                        model=provider + '-v2', authority='primary',
                        reviewed_at='2026-11-21', reviewed_by='fixture reviewer')
                   for provider in ('openai', 'google', 'anthropic')]
        with closing(LearnerStore(self.root / 'evidence')) as store:
            transport = FixtureTransport({s['url']: TransportResponse(200, s['model'].encode(), {}, s['url']) for s in sources})
            hints = Mock(side_effect=lambda: inventory.collect_hints(store, transport, sources=sources, now=100))
            result = inventory.refresh_due(path=self.path, now=100, collector=self.collect, hint_collector=hints)
            self.assertFalse(result['hint_errors'])
            state = inventory.read_json(self.path)
            self.assertEqual({h['provider'] for h in state['hints']}, {'openai', 'google', 'anthropic'})
            self.assertNotIn('observations', state)
            inventory.refresh_due(path=self.path, now=101, collector=self.collect, hint_collector=hints)
            self.assertEqual(hints.call_count, 1)

    def test_no_inference_or_network_for_status_and_local_refresh(self):
        from learner.cli import main
        from unittest.mock import Mock
        catalog=self.root/'catalog.json';catalog.write_text(json.dumps({'models':['sol']}))
        with patch.object(inventory.subprocess,'Popen',side_effect=AssertionError('inference')):
            with patch('learner.cli.HttpTransport',side_effect=AssertionError('network')):
                self.assertEqual(main(['inventory','refresh','--cache',str(self.path),'--catalog','codex='+str(catalog),'--json']),0)
                self.assertEqual(main(['inventory','status','--cache',str(self.path),'--json']),0)

    def test_documented_flow_cli_and_live_refresh_use_existing_store_contract(self):
        import subprocess
        from learner.cli import main
        from unittest.mock import Mock
        entry = Path(__file__).resolve().parents[2] / 'flow/flow.py'
        result = subprocess.run([sys.executable, str(entry), 'inventory', 'status',
                                 '--cache', str(self.path), '--json'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})
        self.assertFalse(self.path.exists())
        catalog = Mock(return_value=self.collect())
        hints = Mock(return_value=([{'model': 'reviewed-v2', 'provider': 'fixture'}], []))
        with patch.object(inventory, 'collect_local', catalog), \
             patch.object(inventory, 'collect_hints', hints), \
             patch.object(inventory, 'collect_claude_account', side_effect=OSError('offline fixture')), \
             patch('learner.cli.HttpTransport', return_value=object()):
            args = ['inventory', 'refresh', '--cache', str(self.path), '--live',
                    '--storage-root', str(self.root / 'store'), '--json']
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args), 0)
        catalog.assert_called_once_with(catalogs=None, native_agy=True)
        self.assertEqual(hints.call_count, 1)

    def test_standalone_learner_keeps_status_and_idle_due_without_flow(self):
        import shutil
        import subprocess
        from learner.tests.fixtures import make_profile
        standalone = self.root / 'standalone'
        package = standalone / 'learner'
        package.mkdir(parents=True)
        for source in Path(inventory.__file__).parent.glob('*.py'):
            shutil.copyfile(source, package / source.name)
        profile = make_profile(root_path=str(self.root)).to_dict()
        script = '''
import sys
sys.path.insert(0, sys.argv[1])
import json
from learner.cli import main
assert 'learner.model_inventory' not in sys.modules
assert main(['--storage-root', sys.argv[2], 'status', '--json']) == 0
from learner.store import LearnerStore
from learner.contracts import ProjectProfile
from learner.assessment import run_due
from learner.model_dispatch import FixtureModelRunner
from learner.sources import FixtureTransport
store = LearnerStore(sys.argv[2], clock=lambda: 1000.)
try:
    profile = store.register_profile(ProjectProfile.from_dict(json.loads(sys.argv[3])))
    store.set_setting('schedule:last_run', {'opportunity-led': 1000., 'project-led': {profile.project_id: 1000.}})
    result = run_due(store, FixtureTransport(), FixtureModelRunner())
    assert result['model_inventory']['reason'] == 'shared_flow_runtime_absent', result
    assert 'learner.model_inventory' not in sys.modules
finally:
    store.close()
'''
        result = subprocess.run([sys.executable, '-I', '-c', script, str(standalone),
                                 str(self.root / 'standalone-store'), json.dumps(profile)],
                                capture_output=True, text=True, cwd=standalone)
        self.assertEqual(result.returncode, 0, result.stderr)

    def account(self, tier='pro', identity='same-account', scope='default-login'):
        return dict(route='claude', scope=scope, identity=identity,
                    signals={'subscriptionType': tier, 'planTier': tier}, observed_at=1)

    def refresh_account(self, now, account):
        return inventory.refresh_due(path=self.path, now=now, collector=self.collect,
                                     account_collector=lambda: account)

    def blocked(self, now=10**9, scope='default-login'):
        return inventory.routes.unavailable('claude', 'fable', state=inventory.read_json(self.path),
                                            now=now, scope=scope)

    def deny(self, now=200, scope='default-login'):
        inventory.record_observation('claude', 'fable', 'credit_denied',
                                     observed_at=now, scope=scope, path=self.path)

    def test_weekly_same_plan_news_and_missing_status_do_not_release_credit_hold(self):
        self.refresh_account(100, self.account())
        self.deny()
        self.assertEqual(self.blocked(), 'subscription_credit_denied')
        for n, account in enumerate((self.account(), self.account(identity='other-account'),
                                      self.account(tier='max', scope='other-profile'))):
            result = self.refresh_account(100 + (n+1)*inventory.WEEK, account)
            self.assertEqual(result['status'], 'refreshed')
            self.assertEqual(self.blocked(), 'subscription_credit_denied')
        result = inventory.refresh_due(path=self.path, now=100+4*inventory.WEEK,
            collector=self.collect, account_collector=lambda: (_ for _ in ()).throw(OSError('offline')))
        self.assertTrue(result['account_errors'])
        self.assertEqual(self.blocked(), 'subscription_credit_denied')
        # Only the original matching identity and a changed entitlement permits revalidation.
        self.refresh_account(100+5*inventory.WEEK, self.account('max'))
        self.assertIsNone(self.blocked())
        row = inventory.read_json(self.path)['observations'][0]
        self.assertEqual(row['status'], 'recheck')
        self.deny(100+5*inventory.WEEK+1)
        self.assertEqual(self.blocked(), 'subscription_credit_denied')

    def test_migration_expired_exact_legacy_hold_and_manual_recheck_keep_other_limits(self):
        row = dict(route='claude', model='fable', status='cooldown', authority='account',
                   observed_at=10, until=20, reason=inventory.routes.LEGACY_CREDIT_REASON)
        self.path.write_text(json.dumps({'observations': [row]}))
        self.assertEqual(self.blocked(), 'subscription_credit_denied')
        self.refresh_account(100, self.account())
        self.assertEqual(self.blocked(), 'subscription_credit_denied')
        # Generic cooldown, owner exclusion, and another profile stay independent.
        inventory.record_observation('claude', 'fable', 'cooldown', observed_at=101,
                                     until=1000, path=self.path)
        inventory.record_observation('claude', 'fable', 'excluded', authority='owner',
                                     observed_at=102, path=self.path)
        self.deny(103, 'other')
        inventory.recheck_subscription('fable', path=self.path, now=104)
        state = inventory.read_json(self.path)
        self.assertEqual({r['status'] for r in state['observations']},
                         {'recheck', 'cooldown', 'excluded', 'credit_denied'})
        self.assertEqual(self.blocked(now=105), 'account_cooldown')
        self.assertEqual(self.blocked(now=1001), 'owner_excluded')
        self.assertIsNotNone(self.blocked(scope='other'))
        generic = dict(row, reason='regular quota')
        self.assertIsNone(inventory.routes.unavailable('claude', 'fable',
                          state={'observations':[generic]}, now=100))

    def test_weekly_cannot_release_denial_written_during_collection(self):
        self.refresh_account(100, self.account())
        self.deny(200)
        def account():
            self.deny(100+inventory.WEEK+1)
            return self.account('max')
        inventory.refresh_due(path=self.path, now=100+inventory.WEEK, collector=self.collect,
                              account_collector=account)
        self.assertEqual(self.blocked(), 'subscription_credit_denied')
        # Nor can an old success overwrite the independently ordered hold.
        inventory.record_observation('claude', 'fable', 'success', observed_at=300, path=self.path)
        self.assertEqual(self.blocked(), 'subscription_credit_denied')

    def test_baseline_without_previous_auth_status_never_claims_access(self):
        self.deny()
        self.refresh_account(300, self.account('max'))
        self.assertEqual(self.blocked(), 'subscription_credit_denied')
        self.refresh_account(300+inventory.WEEK, self.account('max'))
        self.assertEqual(self.blocked(), 'subscription_credit_denied')

    def test_same_timestamp_refusal_wins_recheck_and_older_plan_snapshot(self):
        self.deny(100)
        inventory.recheck_subscription('fable', path=self.path, now=200)
        self.deny(200)
        self.assertEqual(self.blocked(), 'subscription_credit_denied')
        self.refresh_account(300, self.account())
        inventory.record_observation('claude', 'fable', 'cooldown', observed_at=301,
                                     until=1000, path=self.path)
        inventory.recheck_subscription('fable', path=self.path, now=302)
        self.assertEqual(self.blocked(now=400), 'account_cooldown')
        self.assertIsNone(self.blocked(now=1001))

    def test_auth_status_adapter_hashes_identity_and_ignores_expiry(self):
        raw = dict(loggedIn=True, authMethod='claude.ai', apiProvider='firstParty',
                   subscriptionType='pro', planTier='default_claude_pro',
                   email='redacted@example.invalid', orgId='redacted-org', expiresAt=123)
        with patch.object(inventory, '_native_json_output', return_value=json.dumps(raw)) as call:
            first = inventory.collect_claude_account(now=100)
            call.assert_called_once_with(['claude', 'auth', 'status', '--json'])
        self.assertNotIn('redacted', json.dumps(first))
        raw['expiresAt'] = 456
        with patch.object(inventory, '_native_json_output', return_value=json.dumps(raw)):
            self.assertEqual(inventory.collect_claude_account(now=100), first)
        for fields in ({'loggedIn':False}, {'authMethod':'api_key'}, {'apiProvider':'bedrock'},
                       {'subscriptionType':None}, {'email':None, 'orgId':None}):
            with patch.object(inventory, '_native_json_output', return_value=json.dumps(dict(raw, **fields))):
                with self.assertRaises(ValueError): inventory.collect_claude_account()

    def test_manual_cli_is_offline_and_repeated_refusal_reinstates_hold(self):
        from learner.cli import main
        self.deny(1)
        with patch.object(inventory.subprocess, 'Popen', side_effect=AssertionError('auth/inference')):
            self.assertEqual(main(['inventory', 'recheck', '--model', 'fable', '--cache', str(self.path), '--json']), 0)
        self.assertIsNone(self.blocked(now=10**12))
        self.deny(10**12+1)
        self.assertEqual(self.blocked(now=10**12+2), 'subscription_credit_denied')


if __name__=='__main__':unittest.main()
