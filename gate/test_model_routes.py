"""Offline local eligibility and real entrypoint contracts."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import model_routes as routes


class LocalRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, HOME=str(self.home))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.policy = routes.policy_path()
        self.policy.parent.mkdir(parents=True)
        policy = json.loads(Path(__file__).with_name('test_policy.json').read_text())
        policy['design']['codex']['model'] = 'astra'
        policy['design']['gemini']['model'] = 'carbon'
        self.policy.write_text(json.dumps(policy))

    def test_explicit_fable_review_entrypoint_fallback_and_empty_chain(self):
        import io
        from test_compat import load_gate_module
        gate = load_gate_module()
        template = ['claude', '--model', 'claude-fable-5-2', '--effort', 'medium',
                    '--permission-mode', 'plan', '-p', '{prompt}']
        cfg = dict(gate.DEFAULT_CONFIG, judge={'enabled': True, 'chain': ['fable', 'astra']},
                   judge_cmds={'fable': template})
        path = routes.inventory_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'observations': [dict(route='claude', model='claude-fable-5-2',
            status='credit_denied', observed_at=1, reason='secret raw metadata')]}))
        output = io.StringIO()
        with patch('sys.stderr', output), patch.object(gate, 'ensure_probed', return_value=True), \
             patch.object(gate, 'spawn_worker', return_value=(gate.WorkerResult(0, 'fixture'), None)) as spawn:
            verdict, member = gate.judge_once(self.home, cfg, 'review', self.home, 'audit', 'fixture',
                                              parser=lambda *_: {'verdict':'pass'})
        self.assertEqual(member, 'astra')
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(output.getvalue().count('subscription requires usage credits'), 1)
        self.assertIn('flow inventory recheck --model claude-fable-5-2 --route claude', output.getvalue())
        self.assertNotIn('secret', output.getvalue())
        cfg['judge']['chain'] = ['fable']
        with patch.object(gate.subprocess, 'Popen', side_effect=AssertionError('model call')):
            verdict, why = gate.judge_once(self.home, cfg, 'review', self.home, 'audit', 'fixture')
            self.assertIsNone(verdict)
            self.assertIn('flow inventory recheck --model claude-fable-5-2 --route claude', why)
            # Admission uses the same public review pin before launch.
            admission = gate.admit_verdicts(cfg, self.home,
                {'X-1': {'status':'TODO', 'cells':['Implement']}}, {'X-1':['x.py']})
            self.assertIn('flow inventory recheck', admission['X-1']['reason'])

    def test_explicit_worker_pin_displays_hold_before_successful_fallback(self):
        import io
        from test_compat import load_gate_module
        gate = load_gate_module()
        cfg = dict(gate.DEFAULT_CONFIG, workers=['codex'], worker_cmds={
            'agy':['agy','--model','gemini-4-hydrogen-high','--effort','high'],
            'codex':['codex','--model','gpt-6.1-sol','--effort','medium']})
        path = routes.inventory_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'observations':[dict(route='agy', model='gemini-4-hydrogen-high',
            status='credit_denied', observed_at=1)]}))
        output = io.StringIO()
        with patch('sys.stderr', output), patch.object(gate, 'resolve_tool', side_effect=lambda x:x):
            self.assertEqual(gate.available_workers(self.home, cfg, prefer='agy'), ['codex'])
        self.assertIn('flow inventory recheck --model gemini-4-hydrogen-high --route agy', output.getvalue())

    def test_hint_quotes_exact_model_and_avoids_cross_profile_or_quota_guidance(self):
        import shlex
        model = "model'; echo unsafe"
        row = dict(route='claude', model=model, status='credit_denied', observed_at=1)
        state = {'observations':[row]}
        hint = routes.credit_hold_hint('claude', model, state=state, now=10)
        command = hint.split('explicitly recheck: ')[1]
        self.assertEqual(shlex.split(command), ['flow','inventory','recheck','--model',model,'--route','claude'])
        self.assertEqual(routes.credit_hold_hint('claude', model,
            state={'observations':[dict(row,status='recheck')]}, now=10), '')
        self.assertEqual(routes.credit_hold_hint('claude', model,
            state={'observations':[dict(row,status='cooldown',until=100,reason='quota')]}, now=10), '')
        self.assertEqual(routes.credit_hold_hint('claude', model, state=state, scope='other', now=10), '')
        row['scope'] = 'other'
        hint = routes.credit_hold_hint('claude', model, state=state, scope='other', now=10)
        self.assertNotIn('flow inventory recheck', hint)

    def test_carbon_review_scoped_settings_preserve_wrapper_and_deny_mutation(self):
        template = ['env', 'TLS_ROUTE=existing', 'agy', '--model', 'carbon', '--effort',
                    'medium', '--mode', 'plan', '-p', '{prompt}']
        cfg = {'judge': {'chain': ['audit']}, 'judge_cmds': {'audit': template}}
        resolved = routes.review_contracts(cfg, ['audit'], {})[0][2]
        self.assertEqual(resolved[:3], template[:3])
        settings = json.loads(resolved[resolved.index('--settings') + 1])
        self.assertIn('read_file({projdir}/.gate/final-review-evidence/**)', settings['permissions']['allow'])
        self.assertIn('command(*)', settings['permissions']['deny'])
        cfg['judge_cmds']['audit'] = resolved
        self.assertEqual(routes.review_contracts(cfg, ['audit'], {})[0][2], resolved)
        cfg['judge_cmds']['audit'] = resolved + ['--settings', '{"permissions":{"allow":["command(*)"]}}']
        with self.assertRaises(ValueError):
            routes.review_contracts(cfg, ['audit'], {})

    def test_env_wrapped_historical_reviewer_preserves_transport_and_exclusion(self):
        template = ['env', '--chdir', '/tmp', 'WRAPPED=1', '/opt/claude',
                    '--model', 'claude-fable-5-2', '--effort', 'medium',
                    '--permission-mode', 'plan', '-p', '{prompt}']
        cfg = {'judge': {'chain': ['env']}, 'judge_cmds': {'env': template}}
        self.assertEqual(routes.review_contracts(cfg, ['env'], {}), [('env', 'claude', template)])
        state = {'observations': [dict(route='claude', model='claude-fable-5-2',
                 status='excluded', authority='owner', observed_at=1)]}
        policy = routes.load_policy()
        with patch.object(routes, 'read_json', return_value=state), patch.object(routes, 'load_policy', return_value=policy):
            with self.assertRaisesRegex(ValueError, 'independent eligible'):
                routes.review_contracts(cfg, ['env'], {})
        with self.assertRaisesRegex(ValueError, 'independent eligible'):
            routes.review_contracts(cfg, ['env'], {}, ['claude'])

    def test_custom_contract_has_no_invented_model_identity(self):
        from test_compat import load_gate_module
        gate = load_gate_module()
        self.assertEqual(gate.trusted_model(['company-review', '--packet', '{prompt}']), '')
        self.assertEqual(gate.trusted_model(['codex', '--model', 'old']), '')

    def test_known_profiles_do_not_share_account_limits(self):
        state = {'observations': [dict(route='codex', model='gpt-6.1-sol', status='cooldown',
                 authority='account', scope='work', observed_at=1, until=100)]}
        self.assertEqual(routes.unavailable('codex', 'gpt-6.1-sol', state=state, now=50, scope='work'), 'account_cooldown')
        self.assertIsNone(routes.unavailable('codex', 'gpt-6.1-sol', state=state, now=50, scope='personal'))
        a = routes.account_scope('codex', ['env', 'CODEX_HOME=/private/a', 'codex'])
        b = routes.account_scope('codex', ['env', 'CODEX_HOME=/private/b', 'codex'])
        self.assertNotEqual(a, b)
        self.assertNotIn('/private', a)

    def test_real_work_quota_is_shared_across_projects_and_profiles_split(self):
        from test_compat import load_gate_module
        gate = load_gate_module()
        cfg = dict(gate.DEFAULT_CONFIG, workers=['codex', 'agy'], worker_cmds={
            'codex': ['codex', '--model', 'gpt-6.1-sol', '--effort', 'medium'],
            'agy': ['agy', '--model', 'gemini-4-hydrogen-high', '--effort', 'high']})
        first, second = self.home / 'first', self.home / 'second'
        first.mkdir(); second.mkdir()
        gate.disable_worker(first, cfg, 'codex', 'quota', model='gpt-6.1-sol')
        with patch.object(gate, 'resolve_tool', side_effect=lambda name: name):
            self.assertEqual(gate.available_workers(second, cfg), ['agy'])
            with patch.dict(os.environ, FLOW_ACCOUNT_SCOPE_CODEX='other-subscription'):
                self.assertEqual(gate.available_workers(second, cfg), ['codex', 'agy'])
        row = json.loads(routes.inventory_path().read_text())['observations'][0]
        self.assertEqual(row['status'], 'cooldown')
        self.assertEqual(row['authority'], 'account')
        self.assertEqual(gate.returned_model('{"model":"sol"}', 'codex'), None)
        self.assertEqual(gate.returned_model(json.dumps({'type':'result', 'session_id':'fixture',
                         'modelUsage':{'claude-fable-5-2': {'inputTokens':1}}}), 'claude'), 'claude-fable-5-2')

    def test_claude_usage_credit_denial_uses_persistent_hold_and_fallback(self):
        from test_compat import load_gate_module
        gate = load_gate_module()
        message = ('Fable 5.2 requires usage credits. Switch to another model, '
                   'or manage usage credits ... to continue.')
        output = json.dumps({'type': 'result', 'is_error': True,
                             'subtype': 'success', 'result': message})
        reason = gate.tool_outage_reason(output, 1)
        self.assertEqual(reason, 'credit_denied')
        cfg = dict(gate.DEFAULT_CONFIG, workers=['claude', 'codex'], worker_cmds={
            'claude': ['claude', '--model', 'claude-fable-5-2', '--effort', 'medium'],
            'codex': ['codex', '--model', 'gpt-6.1-sol', '--effort', 'medium']})
        first, second = self.home / 'first', self.home / 'second'
        first.mkdir(); second.mkdir()
        gate.disable_worker(first, cfg, 'claude', reason, model='claude-fable-5-2')
        with patch.object(gate, 'resolve_tool', side_effect=lambda name: name):
            self.assertEqual(gate.available_workers(second, cfg), ['codex'])
            with patch.dict(os.environ, FLOW_ACCOUNT_SCOPE_CLAUDE='other-subscription'):
                self.assertEqual(gate.available_workers(second, cfg), ['claude', 'codex'])

    def test_usage_credit_mentions_in_successful_content_are_not_outages(self):
        from test_compat import load_gate_module
        gate = load_gate_module()
        message = ('Fable 5.2 requires usage credits. Switch to another model, '
                   'or manage usage credits ... to continue.')
        for output in (
                message,
                json.dumps({'type': 'result', 'is_error': False,
                            'subtype': 'success', 'result': message}),
                json.dumps({'type': 'assistant', 'message': {'content': message}}),
                json.dumps({'type': 'tool_result', 'is_error': True, 'content': message})):
            with self.subTest(output=output):
                self.assertIsNone(gate.tool_outage_reason(output, 0))

    def test_fallback_custom_worker_alias_is_compared_by_actual_native_route(self):
        template = ['codex', 'exec', '--model', 'astra', '-c', 'model_reasoning_effort="medium"', '--sandbox', 'read-only', '-']
        cfg = {'judge': {'chain': ['audit']}, 'judge_cmds': {'audit': template},
               'worker_cmds': {'backup': ['env', 'codex', '--model', 'gpt-6.1-sol', '--effort', 'medium']}}
        with self.assertRaisesRegex(ValueError, 'independent eligible'):
            routes.review_contracts(cfg, ['audit'], {}, ['backup'])

    def test_unknown_is_eligible_catalog_is_not_entitlement(self):
        self.assertIsNone(routes.unavailable('agy', 'gemini-4-hydrogen-high', now=100))
        self.assertEqual(routes.coding_pin('agy')['model'], 'gemini-4-hydrogen-high')

    def test_owner_exclusion_survives_newer_catalog_or_success(self):
        state = {'observations': [
            dict(route='claude', model='claude-fable-5-2', status='excluded', authority='owner', observed_at=20),
            dict(route='claude', model='claude-fable-5-2', status='success', authority='account', observed_at=30)]}
        self.assertIn('excluded', routes.unavailable('claude', 'claude-fable-5-2', state=state, now=40))

    def test_event_time_not_array_order_and_cooldown_expiry(self):
        rows = [dict(route='agy', model='gemini-4-hydrogen-high', authority='account', status=s,
                     observed_at=t, until=100) for s,t in [('cooldown',30),('success',20)]]
        self.assertIn('cooldown', routes.unavailable('agy','gemini-4-hydrogen-high',state={'observations':rows},now=50))
        self.assertIsNone(routes.unavailable('agy','gemini-4-hydrogen-high',state={'observations':rows},now=101))
        rows.append(dict(rows[0],status='success',observed_at=40))
        self.assertIsNone(routes.unavailable('agy','gemini-4-hydrogen-high',state={'observations':rows},now=50))

    def test_restriction_is_route_model_scoped_unless_pool_evidenced(self):
        state={'observations':[dict(route='agy',model='carbon',status='excluded',authority='owner',observed_at=1)]}
        self.assertIsNone(routes.unavailable('agy','gemini-4-hydrogen-high',state=state))
        self.assertIsNone(routes.unavailable('codex','carbon',state=state))

    def test_central_review_is_exact_readonly_and_independent_of_actual_worker(self):
        choices=routes.review_contracts({}, ['fable','opus','codex'], {}, {'agy'})
        self.assertEqual([c[0] for c in choices], ['astra'])
        self.assertIn('astra', choices[0][2])
        self.assertIn('read-only', choices[0][2])
        choices=routes.review_contracts({}, ['fable','opus','codex'], {}, {'codex'})
        self.assertEqual([c[0] for c in choices], ['carbon'])
        self.assertIn('plan', choices[0][2])

    def test_explicit_stale_override_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'review'):
            routes.review_contracts({'judge':{'chain':['fable']}, 'judge_cmds':{'fable':['claude','--model','old-fable','-p','{prompt}']}}, ['fable'], {}, set())

    def test_compatible_exact_override_and_non_owner_custom_contract(self):
        template=['codex','exec','--model','astra','-c','model_reasoning_effort="medium"','--sandbox','read-only','-']
        cfg={'judge':{'chain':['codex']},'judge_cmds':{'codex':template}}
        self.assertEqual(routes.review_contracts(cfg,['codex'],{},set())[0][2],template)
        self.policy.unlink()
        self.assertEqual(routes.review_contracts({'judge_cmds':{'custom':['custom','{prompt}']}},['custom'],{},set()), [('custom','custom',['custom','{prompt}'])])

    def test_portable_review_filters_exact_credit_holds_and_actual_worker(self):
        self.policy.unlink()
        argv = ['codex', 'exec', '--model', 'fixture-design', '-c',
                'model_reasoning_effort="medium"', '--sandbox', 'read-only', '-']
        cfg = {'judge': {'chain': ['audit']}, 'judge_cmds': {'audit': argv},
               'worker_cmds': {'backup': ['codex', 'exec', '-']}}
        self.assertEqual(routes.review_contracts(cfg, ['audit'], {})[0][1], 'codex')
        with self.assertRaisesRegex(ValueError, 'independent eligible'):
            routes.review_contracts(cfg, ['audit'], {}, ['backup'])
        path = routes.inventory_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'observations': [dict(route='codex', model='fixture-design',
            status='credit_denied', observed_at=1)]}))
        hints = []
        with self.assertRaisesRegex(ValueError, 'independent eligible'):
            routes.review_contracts(cfg, ['audit'], {}, diagnostic=hints.append)
        self.assertEqual(len(hints), 1)
        self.assertIn('flow inventory recheck', hints[0])

    def test_gate_local_filter_and_review_admission(self):
        spec=importlib.util.spec_from_file_location('routes_gate', Path(__file__).with_name('gate.py'))
        gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
        cfg=dict(gate.DEFAULT_CONFIG, workers=['agy','codex'], judge={'enabled':True,'chain':['fable']})
        cfg['judge_cmds']={'fable':['claude','--model','obsolete','-p','{prompt}']}
        with patch.object(gate,'resolve_tool',side_effect=lambda x:x), patch.object(gate,'tool_state',return_value={}):
            self.assertEqual(gate.available_workers(self.home,cfg),['agy','codex'])
            task={'X-1':{'status':'TODO','cells':['Implement']}}
            verdict=gate.admit_verdicts(cfg,self.home,task,{'X-1':['x.py']})
            self.assertIn('review',verdict['X-1']['reason'])

    def test_healthy_fable_exact_custom_route_survives_stale_display_alias(self):
        template = ['claude','--model','claude-fable-5-2','--effort','medium',
                    '--permission-mode','plan','-p','{prompt}']
        cfg = {'judge':{'chain':['fable']},'judge_cmds':{'fable':template}}
        self.assertEqual(routes.review_contracts(cfg,['fable'],{})[0], ('fable','claude',template))
        with self.assertRaisesRegex(ValueError,'independent'):
            routes.review_contracts(cfg,['fable'],{},['claude'])
        implicit = {'judge': {'enabled': True}, 'judge_cmds': {'fable': template}}
        self.assertEqual([c[0] for c in routes.review_contracts(implicit, ['fable', 'opus', 'codex'], {})],
                         ['fable', 'carbon', 'astra'])

    def test_historical_builtin_template_is_validated_instead_of_silently_replaced(self):
        old = ['codex','exec','--sandbox','read-only','-']
        with self.assertRaisesRegex(ValueError,'review'):
            routes.review_contracts({'judge':{'chain':['codex']}},['codex'],{'codex':old})
        stale = ['codex','exec','--model','old-astra','-c','model_reasoning_effort="medium"',
                 '--sandbox','read-only','-']
        with self.assertRaisesRegex(ValueError,'stale'):
            routes.review_contracts({'judge':{'chain':['codex']},'judge_cmds':{'codex':stale}},['codex'],{})
        historical = {'judge': {'chain': ['fable', 'opus', 'codex']}}
        with self.assertRaises(ValueError):
            routes.review_contracts(historical, historical['judge']['chain'], {'codex': old})

    def test_explicit_custom_non_native_contract_is_preserved(self):
        template = ['company-review', '--packet', '{prompt}']
        cfg = {'judge': {'chain': ['company']}, 'judge_cmds': {'company': template}}
        self.assertEqual(routes.review_contracts(cfg, ['company'], {}),
                         [('company', 'company', template)])

    def test_clean_route_fallback_reviews_the_actual_worker_without_quality_penalty(self):
        from types import SimpleNamespace
        from test_autonomy import judged_runner_repo, verdict
        from test_compat import load_gate_module, run
        gate = load_gate_module()
        repo = judged_runner_repo(self.home, ['agy', 'codex'],
                                  extra={'judge': {'enabled': True}, 'serialize_full_acceptance': False})
        seen, reviewers = [], []

        def spawn(repo, cfg, worker, prompt, pf, **kwargs):
            seen.append(worker)
            if worker == 'agy':
                return None, 'model_routes:account_cooldown'
            queue = repo / 'TASK_QUEUE.md'
            queue.write_text(queue.read_text().replace('`TODO`', '`DONE`'))
            (repo / 'eav2.txt').write_text('implemented\n')
            for args in (('add', 'TASK_QUEUE.md', 'eav2.txt'), ('commit', '-qm', 'finish fixture')):
                result = run('git', '-C', str(repo), *args)
                self.assertEqual(result.returncode, 0, result.stderr)
            return gate.WorkerResult(0, 'done'), None

        def review(repo, cfg, prompt, run_dir, tag, run_id, n_tasks=1):
            contracts = gate.review_contracts(cfg, cfg['_review_workers'])
            reviewers.extend(c[1] for c in contracts)
            return verdict(95, 'pass', []), contracts[0][0]

        with patch.dict(os.environ, GATE_CONFIG=''), \
             patch.object(gate, 'resolve_tool', side_effect=lambda name: name), \
             patch.object(gate, 'spawn_worker', side_effect=spawn), \
             patch.object(gate, 'judge_once', side_effect=review):
            gate.cmd_run(SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=True, force=True))
        self.assertEqual(seen, ['agy', 'codex'])
        self.assertEqual(reviewers, ['agy'])
        events = list(gate.iter_journal(repo))
        self.assertEqual(len([e for e in events if e['event'] == 'route_unavailable']), 1)
        self.assertEqual([e['attempt'] for e in events if e['event'] == 'attempt_start'], [1, 1])
        self.assertFalse(any(e['event'] == 'quality_fail' for e in events))

    def test_portable_pinned_quota_rotates_cleanly_and_guards_spawn(self):
        from types import SimpleNamespace
        from test_autonomy import judged_runner_repo
        from test_compat import load_gate_module, run
        gate = load_gate_module()
        self.policy.unlink()
        commands = {'codex': ['codex', '--model', 'portable-sol', '-c',
                             'model_reasoning_effort="medium"'],
                    'agy': ['agy', '--model', 'portable-neon', '--effort', 'high']}
        repo = judged_runner_repo(self.home, ['codex', 'agy'], extra={
            'worker_cmds': commands, 'judge': {'enabled': False},
            'probe': {'enabled': False}, 'serialize_full_acceptance': False})
        seen = []

        def spawn(repo, cfg, worker, prompt, pf, **kwargs):
            seen.append(worker)
            self.assertLessEqual(len(seen), 2, 'quota worker selected repeatedly')
            if worker == 'codex':
                return gate.WorkerResult(1, 'rate limit exceeded'), None
            queue = repo / 'TASK_QUEUE.md'
            queue.write_text(queue.read_text().replace('`TODO`', '`DONE`'))
            (repo / 'eav2.txt').write_text('implemented\n')
            for args in (('add', 'TASK_QUEUE.md', 'eav2.txt'), ('commit', '-qm', 'finish fixture')):
                result = run('git', '-C', str(repo), *args)
                self.assertEqual(result.returncode, 0, result.stderr)
            return gate.WorkerResult(0, 'done'), None

        with patch.dict(os.environ, GATE_CONFIG=''), \
                patch.object(gate, 'resolve_tool', side_effect=lambda name: name), \
                patch.object(gate, 'spawn_worker', side_effect=spawn):
            gate.cmd_run(SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=True, force=True))
        self.assertEqual(seen, ['codex', 'agy'])
        cfg = gate.load_config(repo)
        with patch.object(gate, 'resolve_tool', side_effect=lambda name: name), \
                patch.object(gate, 'validate_worker', return_value=None):
            self.assertEqual(gate.available_workers(repo, cfg), ['agy'])
            result, error = gate.spawn_worker(repo, cfg, 'codex', 'Implement', repo / 'prompt')
            self.assertIsNone(result)
            self.assertEqual(error, 'model_routes:account_cooldown')
            # Unpinned native and custom legacy routes remain usable.
            cfg['worker_cmds']['codex'] = ['codex', 'exec', '-']
            cfg['workers'] = ['codex', 'legacy']
            cfg['worker_cmds']['legacy'] = ['company-worker']
            self.assertEqual(gate.available_workers(repo, cfg), ['codex', 'legacy'])
        events = list(gate.iter_journal(repo))
        self.assertEqual([e['attempt'] for e in events if e['event'] == 'attempt_start'], [1, 1])
        self.assertFalse(any(e['event'] == 'quality_fail' for e in events))

    def test_custom_alias_cannot_hide_stale_native_pin_or_mode_override(self):
        base=['agy','--model','carbon','--effort','medium','--mode','plan','-p','{prompt}']
        cfg={'judge':{'chain':['audit']},'judge_cmds':{'audit':base}}
        self.assertEqual(routes.review_contracts(cfg,['audit'],{})[0][1], 'agy')
        for bad in (base+['--mode','auto'], base+['--agent','writer'],
                    [p.replace('carbon','obsolete') for p in base]):
            cfg['judge_cmds']['audit']=bad
            with self.assertRaises(ValueError): routes.review_contracts(cfg,['audit'],{})

    def test_normal_planning_and_admission_are_local_with_implicit_chain(self):
        spec=importlib.util.spec_from_file_location('routes_admit',Path(__file__).with_name('gate.py'))
        gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
        cfg=dict(gate.DEFAULT_CONFIG, judge={'enabled':True})
        with patch.object(gate.subprocess,'Popen',side_effect=AssertionError('remote call')):
            verdicts=gate.admit_verdicts(cfg,self.home,{'X-1':{'status':'TODO','cells':['Implement']}}, {'X-1':['x.py']})
        self.assertNotIn('review',verdicts['X-1']['reason'])

    def test_new_owner_config_keeps_review_implicit_and_portable_defaults_unchanged(self):
        from types import SimpleNamespace
        from test_compat import committed_runner_repo, load_gate_module
        gate = load_gate_module()
        repo = committed_runner_repo(self.home, ['codex'])
        gate.cmd_init(SimpleNamespace(repo=str(repo), force=True))
        cfg = gate.load_config(repo)
        cfg['judge']['enabled'] = True
        self.assertNotIn('chain', cfg['judge'])
        self.assertEqual([c[0] for c in gate.review_contracts(cfg)], ['carbon', 'astra'])
        self.policy.unlink()
        gate.cmd_init(SimpleNamespace(repo=str(repo), force=True))
        self.assertEqual(gate.load_config(repo)['judge']['chain'], ['fable', 'opus', 'codex'])

    def test_quota_on_review_model_does_not_bench_coding_model(self):
        spec=importlib.util.spec_from_file_location('routes_quota',Path(__file__).with_name('gate.py'))
        gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
        cfg=dict(gate.DEFAULT_CONFIG,workers=['agy'])
        cfg['worker_cmds']={'agy':['agy','--model','gemini-4-hydrogen-high','--effort','high','-p','{prompt}']}
        with patch.object(gate,'resolve_tool',return_value='agy'):
            gate.disable_worker(self.home,cfg,'agy','quota',model='carbon')
            self.assertEqual(gate.available_workers(self.home,cfg),['agy'])
            gate.disable_worker(self.home,cfg,'agy','quota',model='gemini-4-hydrogen-high')
            self.assertEqual(gate.available_workers(self.home,cfg),[])
            cfg['workers'] = ['agy-flow']
            cfg['worker_cmds']['agy-flow'] = cfg['worker_cmds']['agy']
            self.assertEqual(gate.available_workers(self.home,cfg),[])

    def test_credit_hold_survives_expiry_cached_routes_and_failed_shared_write(self):
        from test_compat import load_gate_module
        from learner import model_inventory as inventory
        gate = load_gate_module()
        cfg = dict(gate.DEFAULT_CONFIG, workers=['claude', 'codex'], worker_cmds={
            'claude': ['claude', '--model', 'claude-fable-5-2', '--effort', 'medium'],
            'codex': ['codex', '--model', 'gpt-6.1-sol', '--effort', 'medium']})
        review = ['claude', '--model', 'claude-fable-5-2', '--effort', 'medium',
                  '--permission-mode', 'plan', '-p', '{prompt}']
        review_cfg = {'judge': {'chain':['fable']}, 'judge_cmds': {'fable':review}}
        cached = routes.review_contracts(review_cfg, ['fable'], {})
        gate.disable_worker(self.home, cfg, 'claude', 'credit_denied', model='claude-fable-5-2')
        with patch.object(gate, 'resolve_tool', side_effect=lambda name:name), \
             patch.object(gate.time, 'time', return_value=10**12):
            self.assertEqual(gate.available_workers(self.home, cfg), ['codex'])
            with self.assertRaisesRegex(ValueError, 'independent eligible'):
                routes.review_contracts(review_cfg, ['fable'], {})
            # A cached review template must still be filtered before Popen/accounting.
            with patch.object(gate.subprocess, 'Popen', side_effect=AssertionError('dispatch')):
                proc, err = gate.spawn_worker(self.home, cfg, 'claude', 'review',
                    self.home/'prompt.md', template=cached[0][2])
                self.assertIsNone(proc)
                self.assertIn('subscription_credit_denied', err)
        inventory.recheck_subscription('claude-fable-5-2', now=10**12+1)
        with patch.object(gate, 'resolve_tool', side_effect=lambda name:name), \
             patch.object(gate.time, 'time', return_value=10**12+2):
            self.assertEqual(gate.available_workers(self.home, cfg), ['claude', 'codex'])
        # A failed shared observation still leaves an exact local persistent hold.
        with patch.object(inventory, 'record_observation', side_effect=ValueError('writer busy')), \
             patch.object(gate.time, 'time', return_value=10**12+2):
            gate.disable_worker(self.home, cfg, 'claude', 'credit_denied', model='claude-fable-5-2')
        with patch.object(gate, 'resolve_tool', side_effect=lambda name:name):
            self.assertEqual(gate.available_workers(self.home, cfg), ['codex'])
        inventory.recheck_subscription('claude-fable-5-2', now=10**12+3)
        with patch.object(gate, 'resolve_tool', side_effect=lambda name:name), \
             patch.object(gate.time, 'time', return_value=10**12+4):
            self.assertEqual(gate.available_workers(self.home, cfg), ['claude', 'codex'])

    def test_terminal_dispatch_persists_hold_before_caller_and_keeps_budget(self):
        import io
        from types import SimpleNamespace
        from test_compat import load_gate_module
        gate = load_gate_module()
        output = json.dumps({'type':'result', 'is_error':True,
                             'result':'Fable requires usage credits.'})
        fake = SimpleNamespace(stdout=io.StringIO(output), returncode=1, poll=lambda:1)
        template = ['claude', '--model', 'claude-fable-5-2', '--effort', 'medium', '-p', '{prompt}']
        cfg = dict(gate.DEFAULT_CONFIG)
        gate.RUN_BUDGET.update(max=3, calls=0)
        with patch.object(gate, 'resolve_tool', return_value='fixture-cli'), \
             patch.object(gate.subprocess, 'Popen', return_value=fake) as popen:
            proc, err = gate.spawn_worker(self.home, cfg, 'claude', 'authorized work',
                                          self.home/'prompt.md', template=template)
            self.assertIsNone(err)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(gate.RUN_BUDGET['calls'], 1)
            self.assertEqual(routes.unavailable('claude', 'claude-fable-5-2'), 'subscription_credit_denied')
            proc, err = gate.spawn_worker(self.home, cfg, 'claude', 'authorized work',
                                          self.home/'prompt.md', template=template)
            self.assertIsNone(proc)
            self.assertIn('subscription_credit_denied', err)
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(gate.RUN_BUDGET['calls'], 1)
        self.assertEqual(gate.RUN_BUDGET['max'], 3)

    def test_legacy_local_credit_recheck_and_unrelated_quota(self):
        info = dict(reason=routes.LEGACY_CREDIT_REASON, disabled_until=100,
                    seen='1970-01-01T00:00:10Z')
        release = dict(route='claude', model='claude-fable-5-2', authority='account',
                       entitlement=True, status='recheck', observed_at=20)
        state = {'observations':[release]}
        self.assertTrue(routes.local_model_blocked(info, 'claude', 'claude-fable-5-2', {},
                                                   scope='default-login', now=1000))
        self.assertFalse(routes.local_model_blocked(info, 'claude', 'claude-fable-5-2', state,
                                                    scope='default-login', now=30))
        self.assertTrue(routes.local_model_blocked(dict(info, reason='quota'),
                        'claude', 'claude-fable-5-2', state, scope='default-login', now=30))
        self.assertTrue(routes.local_model_blocked(info, 'claude', 'claude-fable-5-2', state,
                                                  scope='other', now=30))

    def test_failed_credit_and_quota_writes_keep_independent_local_holds(self):
        from test_compat import load_gate_module
        from learner import model_inventory as inventory
        gate = load_gate_module()
        cfg = dict(gate.DEFAULT_CONFIG, workers=['alias'], worker_cmds={
            'alias':['env', 'CLAUDE_CONFIG_DIR=/private/profile', 'claude',
                     '--model','claude-fable-5-2','--effort','medium']})
        template = cfg['worker_cmds']['alias']
        with patch.object(inventory, 'record_observation', side_effect=ValueError('busy')):
            gate.disable_worker(self.home, cfg, 'alias', 'quota', model='claude-fable-5-2')
            gate.disable_worker(self.home, cfg, 'alias', 'credit_denied', model='claude-fable-5-2')
        with patch.dict(os.environ, CLAUDE_CONFIG_DIR='/private/profile'):
            inventory.recheck_subscription('claude-fable-5-2')
        # Releasing credits does not clear the failed-write quota's local expiry.
        with patch.object(gate, 'resolve_tool', return_value='fixture'):
            self.assertEqual(gate.available_workers(self.home, cfg), [])
        # The credit denial was keyed to the native profile, not the alias.
        key = routes.account_key('claude', 'claude-fable-5-2', routes.account_scope('claude', template))
        self.assertIn(key + ':subscription-credit', gate.tool_state(self.home))

    def test_regular_quota_and_network_do_not_establish_credit_entitlement(self):
        from test_compat import load_gate_module
        gate = load_gate_module()
        for text in ('429 rate limit', 'quota exhausted', 'network unavailable'):
            output = json.dumps({'type':'result', 'is_error':True, 'result':text})
            self.assertNotEqual(gate.tool_outage_reason(output, 1), 'credit_denied')
        # Recovered nested tool failures never establish provider refusal.
        output = json.dumps({'type':'tool_result', 'is_error':True,
                             'content':'requires usage credits'})
        self.assertIsNone(gate.tool_outage_reason(output, 1))


if __name__ == '__main__':
    unittest.main()
