"""Offline residual-review probes: real worker/store, synthetic fixtures only."""
import json
import unittest

import test_review_findings


class ResidualPluginTests(unittest.TestCase):
    def test_partial_quoted_credentials_hide_every_prefix(self):
        from hermes_codex.presentation import redact_text
        for quote in ('"', "'"):
            value = 'password=' + quote + 'first SECOND_SECRET' + '\\' + quote + ' suffix' + quote + ' public'
            for end in range(value.index('SECOND_SECRET') + len('SECOND_SECRET'), len(value) + 1):
                with self.subTest(quote=quote, end=end):
                    self.assertNotIn('SECOND_SECRET', redact_text(value[:end]))
            self.assertEqual(redact_text(value), 'password=[redacted] public')

    def test_unload_does_not_join_while_holding_delivery_lock(self):
        import subprocess
        import sys
        from pathlib import Path
        probe = subprocess.run([sys.executable, str(Path(__file__).with_name('unload_probe.py'))],
                               capture_output=True, text=True, timeout=30)
        print(probe.stdout, end='')
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)


class ResidualGoalTests(unittest.TestCase):
    setUp = test_review_findings.GoalReviewTests.setUp
    wait_job = test_review_findings.GoalReviewTests.wait_job
    run_control = test_review_findings.GoalReviewTests.run_control
    wait = test_review_findings.GoalReviewTests.wait

    def test_goal_stream_uses_same_redacted_snapshot_boundary(self):
        self.run_control('goal', task=self.task, operation='set', objective='Observe', budget=100)
        client = self.svc.goal_clients[self.task]
        for number in range(2):
            turn = {'id': f'goal-stream-{number}', 'status': 'inProgress', 'items': []}
            client.events.put({'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': turn}})
            self.wait(lambda: self.svc.status(self.owner, self.task)['state'] == 'running')
            base = {'threadId': 'thread-1', 'turnId': turn['id'], 'itemId': 'reused-item'}
            for part in ('password=', 'GOAL_STREAM_SECRET', ' public goal'):
                client.events.put({'method': 'item/agentMessage/delta', 'params': base | {'delta': part}})
            client.events.put({'method': 'item/agentMessage/delta', 'params': base | {'turnId': 'stale', 'delta': 'STALE_SECRET'}})
            client.events.put({'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': turn | {'status': 'completed'}}})
            self.wait(lambda: any(e['type'] == 'turn/completed' and e['payload']['turn']['id'] == turn['id'] for e in self.svc.events(self.owner, self.task)))
            result = self.svc.result(self.owner, self.task)
            self.assertEqual(result['runtime']['items'][0]['text'], 'password=[redacted] public goal')
            raw = str(list(self.svc.store.db.iterdump()))
            self.assertNotIn('GOAL_STREAM_SECRET', raw)
            self.assertNotIn('STALE_SECRET', raw)


class ResidualWorkerTests(unittest.TestCase):
    setUp = test_review_findings.WorkerReviewTests.setUp
    make_service = test_review_findings.WorkerReviewTests.make_service
    wait = test_review_findings.WorkerReviewTests.wait
    start = test_review_findings.WorkerReviewTests.start
    complete = test_review_findings.WorkerReviewTests.complete

    def test_unkeyed_stream_preserves_text_without_leaking_fragments(self):
        service = self.make_service()
        task = self.start(service)
        a = task['attempts'][-1]
        base = {'threadId': a['thread'], 'turnId': a['turn']}
        for part in ('password=', 'UNKEYED_SECRET', ' visible unkeyed output'):
            service.clients[task['id']].events.put({'method': 'item/agentMessage/delta', 'params': base | {'delta': part}})
        self.complete(service, task)
        events = [e for e in service.events('owner', task['id']) if e['type'] == 'item/agentMessage/delta']
        self.assertIsNotNone(events[-1]['payload']['snapshot'])
        self.assertEqual(events[-1]['payload']['snapshot']['text'], 'password=[redacted] visible unkeyed output')
        self.assertNotIn('UNKEYED_SECRET', str(list(service.store.db.iterdump())))

    def test_snapshot_events_keep_retention_bounds_and_full_stream(self):
        service = self.make_service()
        from test_worker import brief
        task = service.store.submit('owner', str(self.project), 'bounds', brief(self.project), False)
        attempt = service.store.start('owner', task['id'])
        service.store.bind('owner', task['id'], attempt, 'thread', 'turn')
        base = {'threadId': 'thread', 'turnId': 'turn', 'itemId': 'item'}
        kind = 'item/agentMessage/delta'
        items = {}
        content = 'public ไทย ' * 1600
        with service.store.transaction():
            service.record_stream('owner', attempt, 'thread', 'turn', kind, base | {'delta': content}, items)
            for _ in range(2001):
                service.store.event('owner', task['id'], attempt, kind, base | {'delta': 'not stored independently'})
        self.assertEqual(service.store.db.execute('SELECT count(*) FROM events WHERE task=?', (task['id'],)).fetchone()[0], 2000)
        events = service.events('owner', task['id'], limit=200)
        self.assertEqual(len(events), 200)
        self.assertGreater(events[0]['cursor'], 1)
        self.assertLessEqual(len(events[0]['payload']['snapshot']['text']), 16000 + len(' [truncated]'))
        self.assertEqual(service.store.get('owner', 'stream', attempt)['items'][0]['text'], content)
        service.store.event('owner', task['id'], attempt, kind, base | {'turnId': 'other', 'delta': 'UNBOUND_SECRET'})
        last = json.loads(service.store.db.execute('SELECT payload FROM events ORDER BY cursor DESC LIMIT 1').fetchone()[0])
        self.assertIsNone(last['snapshot'])
        self.assertTrue(last['stream_unavailable'])
        self.assertNotIn('UNBOUND_SECRET', str(last))
        with self.assertRaises(PermissionError):
            service.events('other-owner', task['id'])

    def test_queued_steer_rejects_symlink_without_consuming(self):
        service = self.make_service()
        task = self.start(service)
        item = service.followup('owner', task['id'], 'symlink', 'Continue')
        self.project.rename(self.root / 'actual-project')
        self.project.symlink_to(self.root / 'actual-project', target_is_directory=True)
        with self.assertRaises((PermissionError, ValueError)):
            service.queue_action('owner', task['id'], 'steer', item['id'])
        self.assertEqual(service.store.get('owner', 'queue', item['id'])['state'], 'queued')
        self.assertFalse(any(m == 'turn/steer' for m, _ in service.clients[task['id']].calls))
        self.assertEqual(service.queue_action('owner', task['id'], 'clear'), [])

    def test_queued_steer_rechecks_identity_before_consuming(self):
        service = self.make_service()
        task = self.start(service)
        item = service.followup('owner', task['id'], 'queued', 'Continue safely')
        client = service.clients[task['id']]
        self.project.rename(self.root / 'original-project')
        self.project.mkdir()
        with self.assertRaises(PermissionError):
            service.store.check_paths('owner', task['id'])
        denied = False
        try:
            service.queue_action('owner', task['id'], 'steer', item['id'])
        except PermissionError:
            denied = True
        calls = [m for m, _ in client.calls if m == 'turn/steer']
        print('F5 queued replacement: denied =', denied, '; turn/steer calls =', len(calls))
        self.assertTrue(denied)
        self.assertEqual(calls, [])
        self.assertEqual(service.store.get('owner', 'queue', item['id'])['state'], 'queued')
        # Restore the exact authorized inode: the retained queue is usable once,
        # not silently cancelled, replayed, or rebound to the replacement.
        self.project.rmdir()
        (self.root / 'original-project').rename(self.project)
        self.assertEqual(service.queue_action('owner', task['id'], 'steer', item['id'])['state'], 'steered')
        from hermes_codex.state import Conflict
        with self.assertRaises(Conflict):
            service.queue_action('owner', task['id'], 'steer', item['id'])
        self.assertEqual(sum(m == 'turn/steer' for m, _ in client.calls), 1)

    def test_stream_snapshots_preserve_output_and_redact_open_quotes(self):
        service = self.make_service()
        task = self.start(service)
        a = task['attempts'][-1]
        client = service.clients[task['id']]
        base = {'threadId': a['thread'], 'turnId': a['turn']}
        cases = {
            'quoted': ('item/agentMessage/delta', ['api_', 'key="first ', 'SECOND_SECRET', '" safe']),
            'bearer': ('item/plan/delta', ['Bea', 'rer ', 'BEARER_SECRET', ' safe']),
            'key': ('item/agentMessage/delta', ['s', 'k-', 'KEY_SECRET', ' safe']),
            'output': ('item/commandExecution/outputDelta', ['public ', 'output ', 'password=', 'COMMAND_SECRET', ' safe']),
            'long': ('item/agentMessage/delta', ['legitimate ไทย ' * 1300, ' final tail']),
        }
        # Interleave independent items; inspect every durable prefix, not only
        # final state (SQLite updates cannot undo an earlier disclosure).
        for index in range(5):
            for key, (kind, parts) in cases.items():
                if index >= len(parts):
                    continue
                previous = service.events('owner', task['id'])
                cursor = previous[-1]['cursor'] if previous else 0
                client.events.put({'method': kind, 'params': base | {'itemId': key, 'delta': parts[index]}})
                self.wait(lambda: any(e['payload'].get('itemId') == key for e in service.events('owner', task['id'], cursor=cursor)))
                raw_prefix = str(list(service.store.db.iterdump()))
                for secret in ('SECOND_SECRET', 'BEARER_SECRET', 'KEY_SECRET', 'COMMAND_SECRET'):
                    self.assertNotIn(secret, raw_prefix)
        self.complete(service, task)
        events = service.events('owner', task['id'])
        raw = str(list(service.store.db.iterdump()))
        for secret in ('SECOND_SECRET', 'BEARER_SECRET', 'KEY_SECRET', 'COMMAND_SECRET'):
            self.assertNotIn(secret, raw)
        items = service.store.get('owner', 'stream', a['id'])['items']
        by_id = {i['id']: i for i in items}
        self.assertEqual(by_id['output']['aggregatedOutput'], 'public output password=[redacted] safe')
        self.assertEqual(by_id['long']['text'], ''.join(cases['long'][1]))
        self.assertTrue(all('delta' not in e['payload'] for e in events if 'delta' in e['type'].lower()))
        self.assertTrue(any(e['payload'].get('snapshot', {}).get('text', '').endswith('[truncated]') for e in events if e['payload'].get('snapshot')))
        service.close()
        service = self.make_service()
        self.assertNotIn('SECOND_SECRET', str(service.events('owner', task['id'])))

    def test_split_stream_never_persists_secret(self):
        service = self.make_service()
        task = self.start(service)
        a = task['attempts'][-1]
        client = service.clients[task['id']]
        base = {'threadId': a['thread'], 'turnId': a['turn']}
        for kind in ('agentMessage', 'plan'):
            for part in ('pass', 'word=', 'SYNTHETIC_STREAM_SECRET', ' public tail'):
                client.events.put({'method': f'item/{kind}/delta', 'params': base | {'itemId': kind, 'delta': part}})
        self.complete(service, task)
        events = str(service.events('owner', task['id']))
        raw = str(list(service.store.db.iterdump()))
        result = str(service.result('owner', task['id']))
        print('F2 split-stream: events leak =', 'SYNTHETIC_STREAM_SECRET' in events,
              '; assembled redacts =', 'SYNTHETIC_STREAM_SECRET' not in result)
        self.assertNotIn('SYNTHETIC_STREAM_SECRET', events)
        self.assertNotIn('SYNTHETIC_STREAM_SECRET', raw)
        self.assertIn('public tail', result)

    def test_item_less_deltas_stay_visible_and_redact_split_secret(self):
        service = self.make_service()
        task = self.start(service)
        a = task['attempts'][-1]
        client = service.clients[task['id']]
        base = {'threadId': a['thread'], 'turnId': a['turn']}
        for part in ('progress report: pass', 'word=', 'UNKEYED_SECRET', '', ' done'):
            client.events.put({'method': 'item/agentMessage/delta', 'params': base | {'delta': part}})
        self.wait(lambda: 'done' in str(service.events('owner', task['id'])))
        self.complete(service, task)
        events = str(service.events('owner', task['id']))
        raw = str(list(service.store.db.iterdump()))
        print('F2 item-less deltas: progress visible =', 'progress report' in events,
              '; secret persisted =', 'UNKEYED_SECRET' in raw)
        self.assertIn('progress report', events)
        self.assertNotIn('UNKEYED_SECRET', events)
        self.assertNotIn('UNKEYED_SECRET', raw)
        # Internal assembly keys are never exposed to consumers.
        self.assertNotIn('unkeyed', events)
