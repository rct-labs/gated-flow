"""Offline provider boundaries with real gate/coordinator transitions."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import review_consult as consult
import review_packet as packet
from test_compat import committed_runner_repo, load_gate_module, run
from test_autonomy import verdict, HIGH

ADVICE = dict(diagnosis='ordering defect', evidence='BUG and failing ordering assertion',
              next_change='compare event timestamps', test='old event cannot clear new limitation')


class ConsultationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, HOME=str(self.root / 'home'), GATE_CONFIG='')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repo = committed_runner_repo(self.root, ['codex'])
        self.gate = load_gate_module()
        self.cfg = self.gate.load_config(self.repo)
        self.cfg.update(judge={'enabled': True, 'chain': ['alpha', 'beta']},
                        judge_cmds={'alpha': ['offline-a', '{prompt}'],
                                    'beta': ['offline-b', '{prompt}']},
                        probe={'enabled': False}, delivery={'stage': 'module'})
        queue = self.repo / 'TASK_QUEUE.md'
        self.change('TASK_QUEUE.md', queue.read_text().replace('`TODO`', '`DONE`'))
        self.base = self.head()
        self.change('eav2.txt', 'candidate\n')
        self.calls = []
        self.reply = ADVICE
        self.review = verdict(30, 'escalate', HIGH, 'technical dispute')
        self.outage = False
        self.disagree = True
        self.gate.RUN_BUDGET.update(max=20, calls=0)

    def head(self):
        return run('git', '-C', str(self.repo), 'rev-parse', 'HEAD').stdout.strip()

    def change(self, path, text):
        (self.repo / path).write_text(text)
        run('git', '-C', str(self.repo), 'add', path)
        result = run('git', '-C', str(self.repo), 'commit', '-qm', 'fixture change')
        self.assertEqual(result.returncode, 0, result.stderr)

    def spawn(self, repo, cfg, worker, prompt, prompt_file, **kwargs):
        self.calls.append((worker, prompt, cfg))
        self.gate.RUN_BUDGET['calls'] += 1
        if 'consult-' in prompt_file.name:
            # Real reservation is already durable before either blind call.
            state = self.state()
            self.assertEqual(state['spent'] % 2, 0)
            self.assertTrue(state['episodes'][-1]['reserved'])
            self.assertNotIn('replied', prompt)
            if self.outage and worker == 'beta':
                return self.gate.WorkerResult(1, 'network unreachable'), None
            reply = self.reply if worker == 'alpha' or not self.disagree else {**self.reply, 'next_change': 'retain ordering and add timestamp tie-break'}
            if worker == 'alpha':
                return self.gate.WorkerResult(0, json.dumps(reply)), None
            return self.gate.WorkerResult(0, json.dumps({'structured_output': reply})), None
        return self.gate.WorkerResult(0, json.dumps(self.review)), None

    def state(self):
        state = consult.load(self.repo / '.gate/consult')
        return next(iter(state['lineages'].values()))

    def review_once(self, tasks=None):
        with mock.patch.object(self.gate, 'spawn_worker', side_effect=self.spawn):
            return self.gate.review_package(self.repo, self.cfg, 'offline', self.repo / '.gate',
                                           tasks or ['EAV-2'], self.base,
                                           {'EAV-2': ['eav2.txt'], 'EAV-3': ['eav2.txt']})

    def test_two_blind_replies_host_settlement_and_unchanged_restart(self):
        result = self.review_once()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(result['consultation']['status'], 'needs_host')
        self.assertEqual(result['final'], 'failed')
        item = self.state()
        self.assertNotEqual(item['episodes'][0]['seats'][0]['reply'], item['episodes'][0]['seats'][1]['reply'])
        self.gate = load_gate_module()  # process/run budgets cannot buy duplicate calls
        self.review_once()
        self.assertEqual(len(self.calls), 3)
        self.change('REVIEW-NOTES.md', 'notes only\n')
        self.review_once()
        self.assertEqual(len(self.calls), 3)
        key, candidate, _ = self.gate.consultation_identity(self.repo, self.cfg, ['EAV-2'])
        state = consult.load(self.repo / '.gate/consult')
        item = state['lineages'][key]
        episode = item['episodes'][-1]
        from types import SimpleNamespace
        disposition = self.root / 'host-disposition.json'
        disposition.write_text(json.dumps(dict(status='ready', evidence_checked='ordering assertion reproduced',
                              dissent='different tie-break proposal rejected against acceptance',
                              repair='compare timestamps', test='stale cannot clear new')))
        with mock.patch.object(self.gate, 'load_config', return_value=self.cfg):
            self.gate.cmd_consult_settle(SimpleNamespace(repo=str(self.repo), tasks='EAV-2',
                                        episode=episode['id'], disposition=str(disposition)))
        task_prompt = self.gate.task_packet(self.repo, self.cfg,
                                           (self.repo / 'TASK_QUEUE.md').read_text(), 'EAV-2', ['eav2.txt'])
        self.assertIn('HOST-CHECKED CONSULTATION REPAIR', task_prompt)
        self.assertIn('compare timestamps', task_prompt)
        self.assertIn('stale cannot clear new', task_prompt)
        self.assertEqual(self.review_once()['reason'], 'unchanged_rejected_candidate')
        self.change('eav2.txt', 'fixed\n')
        self.review = verdict(95, 'pass', [])
        self.assertEqual(self.review_once()['final'], 'pass')
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.state()['episodes'][-1]['status'], 'reviewed')

    def test_first_rejection_one_repair_then_consultation_without_progress(self):
        self.review = verdict(30, 'revise', HIGH)
        self.assertEqual(self.review_once()['final'], 'failed')
        self.assertEqual(len(self.calls), 1)
        self.review_once()
        self.assertEqual(len(self.calls), 1)
        queue = self.repo / 'TASK_QUEUE.md'
        self.change('TASK_QUEUE.md', queue.read_text() + '\n<!-- task:EAV-3 origin: review -->\n<!-- task:EAV-3 review_of: EAV-2 -->\n')
        self.change('eav2.txt', 'failed mapped repair\n')
        result = self.review_once(['EAV-3'])
        self.assertIn('consultation', result)
        self.assertEqual(len(self.calls), 4)

    def test_partial_outage_preserves_reply_and_never_repeats_calls(self):
        self.outage = True
        self.review_once()
        self.assertEqual(len(self.calls), 3)
        episode = self.state()['episodes'][0]
        self.assertEqual(episode['status'], 'pending')
        self.assertEqual(episode['seats'][0]['reply'], ADVICE)
        self.review_once()
        self.assertEqual(len(self.calls), 3)

    def test_healthy_and_infrastructure_have_no_consultation(self):
        self.review = verdict(95, 'pass', [])
        self.assertEqual(self.review_once()['final'], 'pass')
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(any(item['spent'] for item in consult.load(self.repo / '.gate/consult')['lineages'].values()))
        self.change('eav2.txt', 'new candidate\n')
        with mock.patch.object(self.gate, 'judge_once', return_value=(None, 'timeout')):
            self.assertEqual(self.review_once()['final'], 'skipped')
        self.assertFalse(any(item['spent'] for item in consult.load(self.repo / '.gate/consult')['lineages'].values()))

    def test_seat_or_budget_unavailability_is_pending_without_adviser_calls(self):
        self.gate.RUN_BUDGET.update(max=2, calls=0)
        result = self.review_once()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['consultation']['status'], 'pending')
        self.assertFalse(any(item['spent'] for item in consult.load(self.repo / '.gate/consult')['lineages'].values()))
        self.gate.RUN_BUDGET.update(max=10, calls=0)
        self.review_once()  # explicit review can fill an unreserved pending round
        self.assertEqual(len(self.calls), 3)

    def test_lineages_and_improved_partial_progress_do_not_collapse(self):
        self.review = verdict(30, 'revise', HIGH)
        self.review_once()
        self.change('eav2.txt', 'unrelated task\n')
        self.review_once(['EAV-3'])
        self.assertEqual(len(self.calls), 2)
        state = consult.load(self.repo / '.gate/consult')
        self.assertEqual(len(state['lineages']), 2)
        self.assertTrue(all(not item['episodes'] for item in state['lineages'].values()))

    def test_concurrent_owner_refused_and_reserved_crash_not_replayed(self):
        with consult.locked(self.repo, self.gate._try_os_lock):
            with self.assertRaisesRegex(ValueError, 'owned'):
                self.review_once()
        self.review_once()
        state = consult.load(self.repo / '.gate/consult')
        episode = next(iter(state['lineages'].values()))['episodes'][0]
        episode['seats'][1] = {**episode['seats'][1], 'status': 'reserved', 'reply': None}
        episode['status'] = 'pending'
        consult.save(self.repo / '.gate/consult', state)
        self.review_once()
        self.assertEqual(len(self.calls), 3)

    def test_consensus_is_still_a_host_hold_and_standalone_review_is_deduplicated(self):
        from types import SimpleNamespace
        self.disagree = False
        self.review_once()
        episode = self.state()['episodes'][-1]
        self.assertEqual(episode['status'], 'needs_host')
        self.assertEqual(episode['seats'][0]['reply'], episode['seats'][1]['reply'])
        self.gate = load_gate_module()
        args = SimpleNamespace(repo=str(self.repo), tasks='EAV-2', base=self.base,
                               no_acceptance=True, acceptance_only=False)
        with mock.patch.object(self.gate, 'load_config', return_value=self.cfg), \
             mock.patch.object(self.gate, 'spawn_worker', side_effect=self.spawn):
            with self.assertRaises(SystemExit) as stopped:
                self.gate.cmd_review(args)
        self.assertEqual(stopped.exception.code, 3)
        self.assertEqual(len(self.calls), 3)

    def test_mapped_repair_retains_meaningful_partial_progress(self):
        self.review = verdict(30, 'revise', HIGH + [{**HIGH[0], 'issue': 'second defect'}])
        self.review_once()
        queue = self.repo / 'TASK_QUEUE.md'
        self.change('TASK_QUEUE.md', queue.read_text() + '\n<!-- task:EAV-3 origin: review -->\n<!-- task:EAV-3 review_of: EAV-2 -->\n')
        self.change('eav2.txt', 'one blocker fixed\n')
        self.review = verdict(50, 'revise', HIGH)
        result = self.review_once(['EAV-3'])
        self.assertNotIn('consultation', result)
        self.assertEqual(len(self.calls), 2)

    def test_human_escalation_does_not_dispatch_advisers(self):
        self.review = {**verdict(30, 'escalate', HIGH), 'escalation_kind': 'human'}
        self.review_once()
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.state()['episodes'])

    def test_cumulative_episode_budget_and_candidate_binding(self):
        self.review_once()
        item = self.state()
        episode = item['episodes'][-1]
        with self.assertRaisesRegex(ValueError, 'changed'):
            consult.settle(item, episode['id'], {}, 'wrong', episode['contract'])
        item['spent'] = 4
        rejected = {**item['last'], 'token': 'new-evidence'}
        state = {'version': 1, 'lineages': {'test': item}}
        directory = self.repo / '.gate/consult'
        invocation = mock.Mock()
        result = consult.coordinate(directory, state, item, rejected, {},
                                    [('alpha', 'alpha', []), ('beta', 'beta', [])],
                                    invocation, packet.digest, 10, lambda e: None)
        self.assertEqual(result['status'], 'pending')
        invocation.assert_not_called()

    def test_ready_handoff_is_consumed_before_worker_spawn_and_cannot_be_replayed(self):
        self.review_once()
        state = consult.load(self.repo / '.gate/consult')
        item = next(iter(state['lineages'].values()))
        item['episodes'][-1]['status'] = 'ready'
        consult.save(self.repo / '.gate/consult', state)
        prompt_file = self.root / 'worker.md'
        prompt_file.write_text('repair')
        real_popen = self.gate.subprocess.Popen
        attempted = []
        def offline_popen(argv, **kwargs):
            if argv[0] == '/offline/codex':
                attempted.append(argv)
                raise OSError('offline boundary')
            return real_popen(argv, **kwargs)
        with mock.patch.object(self.gate, 'resolve_tool', return_value='/offline/codex'), \
             mock.patch.object(self.gate, 'validate_worker', return_value=None), \
             mock.patch.object(self.gate.subprocess, 'Popen', side_effect=offline_popen):
            with self.assertRaises(OSError):
                self.gate.spawn_worker(self.repo, self.cfg, 'codex', 'repair', prompt_file, task='EAV-2')
            _, reason = self.gate.spawn_worker(self.repo, self.cfg, 'codex', 'repair', prompt_file, task='EAV-2')
        self.assertIn('fresh review required', reason)
        self.assertEqual(len(attempted), 1)
        self.assertEqual(self.state()['episodes'][-1]['status'], 'repair_started')

    def test_disputed_reply_triggers_consultation_without_escalate_label(self):
        self.review = {**verdict(40, 'revise', HIGH), 'disputed': True}
        result = self.review_once()
        self.assertEqual(result['consultation']['status'], 'needs_host')
        self.assertEqual(len(self.calls), 3)

    def test_central_seats_resolve_exact_policy_and_local_exclusions(self):
        routes = self.gate._model_routes
        policy = {'design': {role: {'model': model, 'effort': 'medium'} for role, model in
                             [('gemini', 'synthetic-carbon'), ('codex', 'synthetic-astra'),
                              ('claude', 'synthetic-fable')]}}
        cfg = {**self.cfg, 'judge_cmds': {}}
        with mock.patch.object(routes, 'load_policy', return_value=policy), \
             mock.patch.object(routes, 'read_json', return_value={}), \
             mock.patch.object(routes, 'unavailable', side_effect=lambda tool, model, **kw:
                               'owner_excluded' if tool == 'agy' else None):
            seats = self.gate.consultation_seats(cfg)
        self.assertEqual([s[1] for s in seats], ['codex', 'claude'])
        self.assertEqual([routes.contract_pin(s[2])[0] for s in seats],
                         ['synthetic-astra', 'synthetic-fable'])
        for _, tool, argv in seats:
            routes._readonly(argv, tool)
        # Portable hosts cannot acquire a default subscription/paid route.
        with mock.patch.object(routes, 'load_policy', return_value=None):
            self.assertEqual(self.gate.consultation_seats({'judge': {'enabled': True}}), [])

    def test_regrouping_tasks_cannot_bypass_a_pending_hold_or_task_budget(self):
        self.review_once()
        result = self.review_once(['EAV-2', 'EAV-3'])
        self.assertEqual(result['reason'], 'consultation_requires_host')
        self.assertEqual(len(self.calls), 3)
        item = {'spent': 0, 'rejections': {}, 'episodes': [], 'task_keys': ['A']}
        state = {'version': 1, 'lineages': {
            'prior-batch': {'spent': 4, 'task_keys': ['A', 'B']}, 'new-scope': item}}
        rejected = dict(token='regrouped', candidate='candidate', contract='contract')
        invoke = mock.Mock()
        result = consult.coordinate(self.repo / '.gate/consult', state, item, rejected, {},
                                    [('a', 'a', []), ('b', 'b', [])], invoke,
                                    packet.digest, 10, lambda e: None)
        self.assertEqual(result['status'], 'pending')
        invoke.assert_not_called()

    def test_same_provider_aliases_do_not_manufacture_two_seats(self):
        self.cfg['judge_cmds']['beta'] = ['offline-a', '{prompt}']
        result = self.review_once()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['consultation']['status'], 'pending')

    def test_parser_file_plain_and_structured_results(self):
        path = self.root / 'out.json'
        for output in [json.dumps(ADVICE), json.dumps({'structured_output': ADVICE}),
                       json.dumps({'result': json.dumps(ADVICE)})]:
            self.assertEqual(consult.parse(output, path, self.gate._last_json_object), ADVICE)
        path.write_text(json.dumps(ADVICE))
        self.assertEqual(consult.parse('', path, self.gate._last_json_object), ADVICE)
        path.unlink()
        self.assertIsNone(consult.parse('access denied', path, self.gate._last_json_object))


if __name__ == '__main__':
    unittest.main()
