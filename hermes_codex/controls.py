"""Trusted-user controls over owned SDK homes, with durable uncertain intent.

The SDK does the protocol. All public control requests run off the Hermes thread.
Local records are not evidence that a native mutation succeeded: read-back is required.
"""
import hashlib
import threading
import time
import uuid
from contextlib import contextmanager

from .state import Conflict, Denied


CONTROL_FIELDS = {
    'threads': ({}, {'search', 'archived', 'limit', 'page'}),
    'providers': (set(), set()),
    'account': ({'operation'}, {'name', 'method'}),
    'provider-select': ({'provider'}, set()),
    'open': ({'task'}, set()),
    'thread-import': ({'proposal', 'thread'}, {'source_task'}),
    'import-catalog': (set(), {'source_task', 'archived', 'limit', 'page'}),
    'reveal': ({'task'}, set()),
    'focus': ({'task'}, set()),
    'reconcile': ({'task'}, set()),
    'recovery': ({'task'}, set()),
    'queue-recover': ({'task', 'id', 'decision'}, set()),
    'maintenance': ({'task', 'operation'}, set()),
    'models': ({'task'}, {'limit', 'page'}),
    'settings': ({'task'}, set()),
    'snapshot': ({'task'}, set()),
    'settings-set': ({'task', 'values'}, set()),
    'goal': ({'task', 'operation'}, {'objective', 'budget'}),
    'attachment-stage': ({'task', 'path', 'kind', 'key'}, {'album', 'caption'}),
    'attachment-clear': ({'task', 'id'}, set()),
    'attachment-use': ({'task', 'id', 'operation'}, {'text', 'key'}),
    'thread-draft': ({'task', 'mutation'}, {'name'}),
    'thread-confirm': ({'id', 'decision'}, set()),
    'thread-reconcile': ({'id'}, set()),
}


def text(value, label, maximum=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise ValueError(f'{label} requires 1–{maximum} characters')
    return value.strip()


class Controls:
    def init_controls(self):
        self.control_threads = {}
        self.control_busy = set()
        self.goal_clients = {}
        self.goal_threads = {}
        self.account_clients = {}
        self.login_links = {}
        with self.store.lock:
            rows = self.store.db.execute("SELECT owner,kind,id,value FROM documents WHERE kind IN ('control','thread-draft','account','goal')").fetchall()
            import json
            for owner, kind, key, raw in rows:
                value = json.loads(raw)
                if kind == 'account' and value.get('state') in {'pending', 'starting'}:
                    value['state'] = 'stale'
                    self.store.put(owner, kind, key, value)
                if kind == 'goal' and value.get('state') == 'active':
                    value['state'] = 'unknown'
                    self.store.put(owner, kind, key, value)
                if (value.get('state') in {'pending', 'running', 'applying'} and kind == 'control') or value.get('state') == 'applying':
                    value['state'] = 'unknown'
                    self.store.put(owner, kind, key, value)

    def control(self, owner, action, payload):
        if action not in CONTROL_FIELDS or not isinstance(payload, dict):
            raise ValueError('Unknown scoped control')
        required, optional = CONTROL_FIELDS[action]
        if not set(required) <= payload.keys() or payload.keys() - set(required) - optional:
            raise ValueError('Invalid control fields; ownership is runtime-derived')
        with self.lock:
            if self.closed or owner in self.control_busy:
                raise Conflict('A scoped control is in progress; poll its status')
            if sum(t.is_alive() for t in self.control_threads.values()) >= self.max_workers:
                raise Conflict('Control concurrency limit reached')
            job = {'id': str(uuid.uuid4()), 'action': action, 'state': 'pending', 'created': time.time(), 'mutation_started': False}
            self.store.put(owner, 'control', job['id'], job)
            self.control_busy.add(owner)
            worker = threading.Thread(target=self._control_run, args=(owner, job, dict(payload)), daemon=True)
            self.control_threads[job['id']] = worker
            worker.start()
            return dict(job)

    def control_status(self, owner, key):
        from .service import sanitized
        with self.lock:
            return sanitized(self.store.get(owner, 'control', key))

    def _control_run(self, owner, job, payload):
        from .service import sanitized
        try:
            job['state'] = 'running'
            self.store.put(owner, 'control', job['id'], job)
            job['result'] = sanitized(self._control_action(owner, job, payload))
            job['state'] = 'completed'
        except Exception as exc:
            from .presentation import error_info
            job.update(state='unknown' if job['mutation_started'] else 'failed', error=error_info(exc, self.settings(owner).get('locale', 'en')))
        finally:
            with self.lock:
                self.store.put(owner, 'control', job['id'], job)
                self.control_busy.discard(owner)

    def _mutating(self, owner, job):
        job['mutation_started'] = True
        self.store.put(owner, 'control', job['id'], job)

    @staticmethod
    def _deny_control(method, params):
        if method in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}:
            return {'decision': 'decline'}
        if method == 'item/tool/requestUserInput':
            return {'answers': {}}
        raise Denied('No live authorized task owns this server request')

    def task_provider(self, owner, task):
        self.store.task(owner, task)
        try:
            return self.store.get(owner, 'route', task)['provider']
        except Denied:
            return 'default'  # immutable legacy routing; never infer from current selection

    def selected_account(self, owner):
        try:
            return self.store.get(owner, 'selected-account', self.settings(owner)['provider'])['id']
        except Denied:
            return None

    def worker_home(self, owner, task):
        try:
            route = self.store.get(owner, 'route', task)
        except Denied:
            route = {}
        if route.get('home_task'):
            self.store.task(owner, route['home_task'])
            return self.worker_home(owner, route['home_task'])
        if route.get('account'):
            from .accounts import account_home
            account = self.store.get(owner, 'account', route['account'])
            if account['provider'] != self.task_provider(owner, task):
                raise Denied('Account and task provider differ')
            return account_home(self, owner, route['account'])
        base = self.home / 'workers' / hashlib.sha256(owner.encode()).hexdigest()
        provider = self.task_provider(owner, task)
        if provider != 'default':
            base = base / ('provider-' + hashlib.sha256(provider.encode()).hexdigest())
        return base / task

    @contextmanager
    def control_client(self, owner, task):
        self.store.task(owner, task)
        client = self.factory(self.worker_home(owner, task), approval_handler=self._deny_control)
        try:
            yield client
        finally:
            client.close()

    def require_route(self, owner, task):
        try:
            account = self.store.get(owner, 'route', task).get('account')
        except Denied:
            account = None
        if self.task_provider(owner, task) != self.settings(owner)['provider'] or account != self.selected_account(owner):
            raise Conflict('Select the owning provider and account before changing this task')

    def _idle_task(self, owner, task):
        record = self.store.task(owner, task)
        self.require_route(owner, task)
        if self.goal_blocks(owner, task):
            raise Conflict('Pause/reconcile the native goal before changing this task')
        if record['state'] in {'pending', 'starting', 'running', 'cancelling', 'unknown'}:
            raise Conflict('Task has active or uncertain work; reconcile before changing it')
        if any(x.get('task') == task and x.get('state') in {'pending', 'decided'} for x in self.store.list(owner, 'interaction')):
            raise Conflict('Task has an unresolved interaction')
        if any(q.get('task') == task and q['state'] in {'queued', 'uncertain_steer'} for q in self.store.list(owner, 'queue')):
            raise Conflict('Task has queued or uncertain steering work')
        return self.store.get(owner, 'binding', task)

    def _thread_read(self, client, binding):
        thread = client.call('thread/read', {'threadId': binding['thread'], 'includeTurns': True})['thread']
        if thread['id'] != binding['thread']:
            raise Conflict('Runtime returned a different thread')
        return thread

    def import_source(self, owner, source_task=None):
        if source_task:
            self.require_route(owner, source_task)
            try:
                route = self.store.get(owner, 'route', source_task)
            except Denied:
                route = {'provider': self.task_provider(owner, source_task), 'account': None}
            return self.worker_home(owner, source_task), route | {'home_task': source_task}
        from .accounts import account_home
        account = self.selected_account(owner)
        if account is None:
            raise Denied('Select a named account or an owned source task for native history')
        return account_home(self, owner, account), {'provider': self.settings(owner)['provider'], 'account': account}

    def _control_action(self, owner, job, p):
        action = job['action']
        if action == 'import-catalog':
            home, route = self.import_source(owner, p.get('source_task'))
            archived, limit = p.get('archived', False), p.get('limit', 20)
            if type(archived) is not bool or type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError('Invalid native history archive/page options')
            offset = 0
            if p.get('page'):
                page = self.store.get(owner, 'import-page', p['page'])
                if page['route'] != route or page['expires'] <= time.time() or page['archived'] != archived:
                    raise Conflict('Native history selection is stale; refresh')
                data, offset, limit = page['data'], page['offset'], page['limit']
            else:
                client = self.factory(home, approval_handler=self._deny_control)
                try:
                    data = [{k: t.get(k) for k in ('id', 'name', 'preview', 'cwd', 'status')} for t in self.thread_catalog(client, archived).values()]
                finally:
                    client.close()
            token = None
            if offset + limit < len(data):
                token = str(uuid.uuid4())
                self.store.put(owner, 'import-page', token, {'route': route, 'archived': archived, 'data': data, 'offset': offset + limit, 'limit': limit, 'expires': time.time() + 300})
            return {'data': data[offset:offset + limit], 'next': token, 'total': len(data), 'source': 'selected native home; not yet imported'}
        if action == 'recovery':
            record = self.store.task(owner, p['task'])
            result = {kind: [v for v in self.store.list(owner, kind) if v.get('task') == p['task']]
                      for kind in ('queue', 'interaction', 'plan', 'attachment', 'goal', 'thread-draft')}
            return result | {'task': record, 'automatic_replay': False,
                'actions': {'queue': 'continue queued input or dismiss uncertain steering; never automatically retry steering',
                            'interaction': 'stale callbacks cannot be answered; reconcile the turn and request fresh input',
                            'plan': 'show/continue/revise/cancel the exact revision', 'attachment': 'use-next/analyze-now/clear staged media',
                            'task': 'reconcile exact known turn; missing turn identifiers remain unknown'}}
        if action == 'queue-recover':
            with self.lock:
                record = self.store.task(owner, p['task'])
                item = self.store.get(owner, 'queue', p['id'])
                if item['task'] != p['task']:
                    raise Denied('Queue belongs to another task')
                if p['decision'] == 'dismiss' and item['state'] == 'uncertain_steer':
                    item.update(state='dismissed_uncertain', consumed_by_runtime=None, resolved=time.time())
                    return self.store.put(owner, 'queue', item['id'], item)
                if p['decision'] == 'continue' and item['state'] == 'queued' and record['state'] == 'completed':
                    self._advance_queue(owner, p['task'])
                    return self.store.get(owner, 'queue', item['id'])
                if p['decision'] == 'continue' and item['state'] == 'submitted' and record['state'] == 'pending' and not any(a['state'] in {'starting','running','unknown','cancelling'} for a in record['attempts']):
                    self._launch(owner, p['task'])
                    return self.store.get(owner, 'queue', item['id'])
                raise Conflict('Only a safe queued continuation or uncertain-steer dismissal is available; no replay')
        if action == 'thread-import':
            from .access import validate, identity
            proposal = self.store.get(owner, 'proposal', p['proposal'])
            if not proposal['authorized'] or proposal['expires'] <= time.time():
                raise Denied('Authorize the exact import assignment first')
            home, route = self.import_source(owner, p.get('source_task'))
            provider = route['provider']
            if [provider, route.get('account')] != [self.settings(owner)['provider'], self.selected_account(owner)] or [proposal['provider'], proposal.get('account')] != [provider, route.get('account')]:
                raise Denied('Import source, proposal and selected route must match')
            brief = validate(proposal['brief'], self.roots, self.allow_full_access)
            if identity(brief) != proposal.get('paths'):
                raise Denied('Authorized roots changed')
            thread_id = text(p['thread'], 'Thread', 500)
            client = self.factory(home, approval_handler=self._deny_control)
            try:
                if thread_id not in self.thread_catalog(client, False):
                    raise Denied('Thread is not available in the authorized source home')
                thread = self._thread_read(client, {'thread': thread_id})
            finally:
                client.close()
            if thread.get('cwd') != brief['workspace'] or thread.get('status', {}).get('type') == 'active':
                raise Denied('Native thread workspace differs or a native turn is active')
            task = self.store.submit(owner, brief['workspace'], brief['idempotency_key'], brief, bool(brief['write_roots']), paths=proposal['paths'])
            try:
                binding = self.store.get(owner, 'binding', task['id'])
                if binding['thread'] != thread_id or self.worker_home(owner, task['id']) != home:
                    raise Conflict('Assignment already binds another thread/home')
            except Denied:
                if task['state'] != 'pending' or task['attempts']:
                    raise Conflict('Assignment has already executed')
                if route.get('home_task') == task['id']:
                    route.pop('home_task')
                for existing in self.store.list(owner, 'binding'):
                    if existing['thread'] == thread_id and self.worker_home(owner, existing['task']) == home:
                        raise Conflict('Native thread is already bound; open its existing task')
                with self.store.transaction():
                    self.store.put(owner, 'route', task['id'], route)
                    self.store.put(owner, 'binding', task['id'], {'task': task['id'], 'thread': thread_id, 'provider': provider, 'imported': True, 'effective': {'source': 'history only; policy applies on authorized continuation'}})
                    self.store.db.execute("UPDATE tasks SET state='completed',review_state='not_ready' WHERE id=?", (task['id'],))
            return {'task': task['id'], 'thread': thread, 'imported': True, 'executed': False}
        if action == 'account':
            from .accounts import control
            return control(self, owner, job, p)
        if action == 'providers':
            return {'data': [{'id': key, **value} for key, value in self.profiles.items()], 'selected': self.settings(owner)['provider']}
        if action == 'provider-select':
            return self.select_provider(owner, p['provider'])
        if action == 'snapshot':
            task = self.store.task(owner, p['task'])
            settings = self.settings(owner)
            related = {kind: [v for v in self.store.list(owner, kind) if v.get('task') == task['id']] for kind in ('interaction', 'attachment', 'plan', 'goal')}
            try:
                health = self.store.get(owner, 'health', task['id']) | {'source': 'cached', 'stale': True}
            except Denied:
                health = {'source': 'unavailable', 'execution_ready': False}
            return {'task': task, 'configured': settings, 'binding': self.store.get(owner, 'binding', task['id']), 'health': health,
                    'interactions': related['interaction'], 'attachments': related['attachment'], 'goals': related['goal'],
                    'plans': [p for p in related['plan'] if p['state'] == 'pending' or settings.get('plan_history', True)],
                    'queue': self.queue_items(owner, task['id']), 'connected': task['id'] in self.goal_clients or bool(self.threads.get(task['id']) and self.threads[task['id']].is_alive())}
        if action in {'reveal', 'focus'}:
            self.store.task(owner, p['task'])
            binding = self.store.get(owner, 'binding', p['task'])
            from .presentation import reveal
            return reveal(binding['thread'], self.desktop_command)
        if action == 'reconcile':
            task = self.store.task(owner, p['task'])
            if task['state'] not in {'unknown', 'cancelling'} or not task['attempts']:
                raise Conflict('Task does not need uncertain-turn reconciliation')
            attempt = task['attempts'][-1]
            if not attempt['thread'] or not attempt['turn']:
                return task | {'recovery': 'Submission lacks confirmed turn identity; no automatic retry is safe'}
            with self.control_client(owner, task['id']) as client:
                thread = self._thread_read(client, {'thread': attempt['thread']})
            matched = next((t for t in thread['turns'] if t['id'] == attempt['turn']), None)
            state = {'completed': 'completed', 'failed': 'failed', 'interrupted': 'cancelled'}.get((matched or {}).get('status'))
            if state:
                matched = self.completed_turn(owner, attempt['id'], attempt['thread'], matched)
                from .service import sanitized
                try:
                    execution = self.store.get(owner, 'execution', attempt['id'])
                except Denied:
                    execution = {}
                if state == 'completed' and (execution.get('mode') == 'plan' or any(i.get('type') == 'plan' for i in matched.get('items', []))):
                    self.save_plan(owner, task['id'], attempt['id'], attempt['thread'], matched)
                self.store.finish(owner, task['id'], attempt['id'], state, {'runtime': sanitized(matched, bounded=False), 'thread': attempt['thread'], 'turn': attempt['turn'], 'evidence_source': 'runtime-reconciliation', 'verified': False})
            return self.store.task(owner, task['id'])
        if action == 'maintenance':
            binding = self._idle_task(owner, p['task'])
            if p['operation'] not in {'reconnect', 'restart'}:
                raise ValueError('Maintenance operation must be reconnect or restart')
            if self.pending_plan(owner, p['task']) or any(a.get('task') == p['task'] and a['state'] == 'staged' for a in self.store.list(owner, 'attachment')):
                raise Conflict('Resolve pending plan/attachments before maintenance')
            old = self.goal_clients.get(p['task'])
            if old:
                old.close()
                self.goal_threads[p['task']].join()
            for retry in range(3):
                try:
                    with self.control_client(owner, p['task']) as client:
                        account = client.call('account/read', {'refreshToken': False})
                        limits = client.call('account/rateLimits/read', {})
                        native = self._thread_read(client, binding)
                        if native.get('status', {}).get('type') == 'active':
                            raise Conflict('Native thread is active; maintenance readiness is not confirmed')
                        health = {'ready': True, 'execution_ready': not account.get('requiresOpenaiAuth', True) or account.get('account') is not None,
                                  'operation': p['operation'], 'identity': getattr(client, 'identity', {}), 'account': account, 'limits': limits,
                                  'target': p['task'], 'scope': 'plugin-owned worker only', 'observed': time.time()}
                        self.store.put(owner, 'health', p['task'], health)
                        return health
                except (ConnectionError, TimeoutError):
                    if retry == 2:
                        raise
                    if self.stop.wait(.05 * 2**retry):
                        raise Conflict('Service is closing')
        if action == 'attachment-use':
            batch = self.store.get(owner, 'attachment', p['id'])
            self.store.task(owner, p['task'])
            if batch['task'] != p['task']:
                raise Denied('Attachment belongs to another task')
            if p['operation'] == 'use-next':
                if batch['state'] != 'staged' or set(p) != {'task', 'id', 'operation'}:
                    raise Conflict('Only a staged batch can be selected for the next message')
                batch['use_next'] = True
                return self.store.put(owner, 'attachment', batch['id'], batch)
            if p['operation'] == 'analyze-now':
                return self.followup(owner, p['task'], p.get('key'), p.get('text'), attachment_ids=[batch['id']], _from_control=True)
            raise ValueError('Choose use-next or analyze-now; use attachment-clear to discard')
        if action == 'attachment-stage':
            from .attachments import stage
            return stage(self, owner, p)
        if action == 'attachment-clear':
            self.store.task(owner, p['task'])
            batch = self.store.get(owner, 'attachment', p['id'])
            if batch['task'] != p['task'] or batch['state'] != 'staged':
                raise Conflict('Only a staged batch for this task can be cleared')
            batch['state'] = 'cleared'
            return self.store.put(owner, 'attachment', p['id'], batch)
        if action == 'goal':
            return self._goal_control(owner, job, p)
        if action in {'models', 'settings', 'settings-set'}:
            return self._settings_control(owner, action, p)
        if action == 'threads':
            return self._browse(owner, p)
        if action == 'open':
            binding = self._idle_task(owner, p['task'])
            with self.control_client(owner, p['task']) as client:
                thread = self._thread_read(client, binding)
            if binding.get('archived'):
                raise Conflict('Unarchive this thread before opening')
            self.store.put(owner, 'selection', 'current', {'task': p['task'], 'thread': binding['thread']})
            return {'thread': thread, 'task': p['task'], 'history_source': 'runtime', 'effective': binding.get('effective', {}),
                    'desktop': self.desktop_sync(binding['thread']) if self.settings(owner).get('sync_on_open') else {'requested': False}}
        if action == 'thread-draft':
            binding = self._idle_task(owner, p['task'])
            if p['mutation'] not in {'rename', 'archive', 'unarchive'}:
                raise ValueError('Unknown thread mutation')
            if p['mutation'] in {'archive', 'unarchive'} and not self.settings(owner).get('allow_archive', False):
                raise Denied('Archive permission is disabled')
            name = text(p.get('name'), 'Name', 200) if p['mutation'] == 'rename' else None
            with self.control_client(owner, p['task']) as client:
                original = self._thread_read(client, binding)
            draft = {'id': str(uuid.uuid4()), 'task': p['task'], 'thread': binding['thread'], 'mutation': p['mutation'], 'name': name,
                     'original_name': original.get('name'), 'state': 'pending', 'expires': time.time() + 300}
            return self.store.put(owner, 'thread-draft', draft['id'], draft)
        if action == 'thread-reconcile':
            draft = self.store.get(owner, 'thread-draft', p['id'])
            if draft['state'] not in {'applying', 'unknown'}:
                raise Conflict('Only uncertain mutations need reconciliation')
            binding = self._idle_task(owner, draft['task'])
            if binding['thread'] != draft['thread']:
                raise Conflict('Thread binding changed; cannot reconcile another thread')
            with self.control_client(owner, draft['task']) as client:
                current = self._thread_read(client, binding)
                if draft['mutation'] == 'rename':
                    observed = current.get('name')
                    applied = observed == draft['name']
                    if not applied and observed != draft['original_name']:
                        raise Conflict('Native name diverged; refresh and request a new confirmation')
                    binding['name'] = observed
                else:
                    desired = draft['mutation'] == 'archive'
                    applied = binding['thread'] in self.thread_catalog(client, desired)
                    if not applied and binding['thread'] not in self.thread_catalog(client, not desired):
                        raise Conflict('Native history cannot establish archive state')
                    binding['archived'] = desired if applied else not desired
            with self.store.transaction():
                self.store.put(owner, 'binding', draft['task'], binding)
                if binding.get('archived'):
                    try:
                        selection = self.store.get(owner, 'selection', 'current')
                    except Denied:
                        selection = {}
                    if selection.get('task') == draft['task']:
                        self.store.put(owner, 'selection', 'current', {})
                draft.update(state='reconciled_applied' if applied else 'reconciled_unapplied', observed=time.time(),
                             evidence='native current state; mutation origin is not inferred')
                return self.store.put(owner, 'thread-draft', draft['id'], draft)
        if action == 'thread-confirm':
            draft = self.store.get(owner, 'thread-draft', p['id'])
            if draft['state'] != 'pending' or draft['expires'] <= time.time():
                raise Conflict('Thread confirmation is stale')
            if p['decision'] not in {'confirm', 'cancel'}:
                raise ValueError('Choose confirm or cancel')
            if p['decision'] == 'cancel':
                draft['state'] = 'cancelled'
                return self.store.put(owner, 'thread-draft', draft['id'], draft)
            binding = self._idle_task(owner, draft['task'])
            if binding['thread'] != draft['thread']:
                raise Conflict('Thread binding changed')
            mutation = draft['mutation']
            if mutation != 'rename' and not self.settings(owner).get('allow_archive', False):
                raise Denied('Archive permission is disabled')
            with self.control_client(owner, draft['task']) as client:
                current = self._thread_read(client, binding)
                if current.get('name') != draft['original_name']:
                    raise Conflict('Thread changed since confirmation was prepared; refresh the draft')
                if current.get('status', {}).get('type') == 'active':
                    raise Conflict('Native thread has an active turn')
                draft['state'] = 'applying'
                self.store.put(owner, 'thread-draft', draft['id'], draft)
                self._mutating(owner, job)
                if mutation == 'rename':
                    client.call('thread/name/set', {'threadId': binding['thread'], 'name': draft['name']})
                    if self._thread_read(client, binding).get('name') != draft['name']:
                        raise Conflict('Rename read-back differs; reconcile before retry')
                    binding['name'] = draft['name']
                else:
                    client.call('thread/' + mutation, {'threadId': binding['thread']})
                    archived = mutation == 'archive'
                    if binding['thread'] not in self.thread_catalog(client, archived):
                        raise Conflict('Archive read-back unavailable; reconcile before retry')
                    binding['archived'] = archived
                    if archived:
                        try:
                            selected = self.store.get(owner, 'selection', 'current')
                        except Denied:
                            selected = {}
                        if selected.get('task') == draft['task']:
                            self.store.put(owner, 'selection', 'current', {})
                self.store.put(owner, 'binding', draft['task'], binding)
                draft['state'] = 'applied'
                return self.store.put(owner, 'thread-draft', draft['id'], draft)
        raise ValueError('Unknown control')

    def select_provider(self, owner, provider):
        if not isinstance(provider, str) or provider not in self.profiles:
            raise ValueError('Select a configured Codex provider profile')
        with self.lock, self.store.transaction():
            if self.store.db.execute("SELECT 1 FROM tasks WHERE owner=? AND state IN ('pending','starting','running','cancelling','unknown') LIMIT 1", (owner,)).fetchone():
                raise Conflict('Provider switching is blocked by active or uncertain work')
            for kind, pending in {'interaction': {'pending', 'decided'}, 'plan': {'pending'}, 'queue': {'queued', 'uncertain_steer'}, 'attachment': {'staged'}, 'goal': {'active', 'applying', 'unknown'}}.items():
                if any(v.get('state') in pending for v in self.store.list(owner, kind)):
                    raise Conflict('Resolve pending ' + kind + ' before switching providers')
            old = self.settings(owner)
            self.store.put(owner, 'provider-settings', old['provider'], old)
            try:
                selected = self.store.get(owner, 'selection', 'current')
            except Denied:
                selected = {}
            self.store.put(owner, 'provider-selection', old['provider'], selected)
            try:
                settings = self.store.get(owner, 'provider-settings', provider)
            except Denied:
                settings = {'provider': provider, 'auto_queue': True, 'mode': 'execute', 'locale': old.get('locale', 'en')} | self.profiles[provider].get('defaults', {})
            try:
                selected = self.store.get(owner, 'provider-selection', provider)
            except Denied:
                selected = {}
            self.store.put(owner, 'settings', 'current', settings)
            self.store.put(owner, 'selection', 'current', selected)
            return {'configured': settings, 'selection': selected, 'effective_backend': 'pending native read-back; existing tasks retain their provider'}

    def desktop_sync(self, thread):
        from .presentation import reveal, error_info
        try:
            return reveal(thread, self.desktop_command)
        except Exception as exc:
            return {'requested': False, 'focused': None, 'error': error_info(exc)}

    def goal_blocks(self, owner, task):
        try:
            return self.store.get(owner, 'goal', task)['state'] in {'active', 'unknown', 'applying'} or bool(self.store.db.execute(
                "SELECT 1 FROM writers.claims WHERE store=? AND task=? AND mode='goal'", (self.store.path, task)).fetchone())
        except Denied:
            return False

    def _goal_control(self, owner, job, p):
        task, operation = p['task'], p['operation']
        if operation != 'get':
            self.require_route(owner, task)
        record = self.store.task(owner, task)
        binding = self.store.get(owner, 'binding', task)
        if operation not in {'get', 'set', 'pause', 'resume', 'clear'}:
            raise ValueError('Goal operation must be get, set, pause, resume or clear')
        if 'objective' in p and operation != 'set' or 'budget' in p and operation not in {'set', 'resume'}:
            raise ValueError('Objective/budget not valid for this goal operation')
        if 'budget' in p and (type(p['budget']) is not int or not 1 <= p['budget'] <= 2**63 - 1):
            raise ValueError('Goal budget must be a positive integer total, not an increment')
        objective = text(p.get('objective'), 'Goal objective', 4000) if operation == 'set' else None
        if operation in {'set', 'resume'}:
            self.store.check_paths(owner, task)
            if not binding.get('policy_verified') or binding.get('mode') == 'plan':
                raise Denied('Native goal requires an authorized execution turn with verified effective policy; imported/planning threads cannot inherit unchecked access')
            if record['state'] in {'pending', 'starting', 'running', 'cancelling', 'unknown'} or self.settings(owner)['mode'] == 'plan' or self.pending_plan(owner, task):
                raise Conflict('Finish active work/planning before activating a native goal')
            if any(q.get('task') == task and q['state'] in {'queued', 'uncertain_steer'} for q in self.store.list(owner, 'queue')) or any(i.get('task') == task and i['state'] in {'pending', 'decided'} for i in self.store.list(owner, 'interaction')):
                raise Conflict('Resolve queued work and pending interactions first')
        client = self.goal_clients.get(task)
        if client is None:
            if task in self.threads and self.threads[task].is_alive():
                client = self.clients[task]
            else:
                client = self.factory(self.worker_home(owner, task), approval_handler=lambda method, params: self._goal_interaction(owner, task, method, params))
                self.goal_clients[task] = client
                watcher = threading.Thread(target=self._goal_watch, args=(owner, task, binding['thread'], client), daemon=True)
                self.goal_threads[task] = watcher
                watcher.start()
        current = client.call('thread/goal/get', {'threadId': binding['thread']}).get('goal')
        if current and current['threadId'] != binding['thread']:
            raise Conflict('Goal response belongs to another thread')
        if operation in {'set', 'resume'}:
            native = self._thread_read(client, binding)
            if native.get('status', {}).get('type') == 'active':
                raise Conflict('Native turn is active')
            if operation == 'resume' and current is None:
                raise Conflict('No native goal to resume')
            same = current is not None and (objective is None or objective == current['objective'])
            if same and current['status'] == 'budgetLimited' and ('budget' not in p or p['budget'] <= max(current.get('tokenBudget') or 0, current['tokensUsed'])):
                raise Conflict('Budget-limited goal needs an explicit larger total or a new objective')
            if same and 'budget' in p and p['budget'] <= current['tokensUsed']:
                raise Conflict('Total budget must exceed already consumed tokens')
        if operation != 'get':
            if operation == 'pause' and current is None:
                raise Conflict('No native goal to pause')
            with self.store.transaction():
                if operation in {'set', 'resume'}:
                    self.store.check_paths(owner, task)
                    self.store.claim_writer(record, mode='goal')
                self.store.put(owner, 'goal', task, {'task': task, 'thread': binding['thread'], 'state': 'applying', 'snapshot': current})
            self._mutating(owner, job)
            params = {'threadId': binding['thread']}
            if operation == 'clear':
                client.call('thread/goal/clear', params)
            else:
                params['status'] = 'paused' if operation == 'pause' else 'active'
                if objective is not None:
                    params['objective'] = objective
                if 'budget' in p:
                    params['tokenBudget'] = p['budget']
                client.call('thread/goal/set', params)
            updated = client.call('thread/goal/get', {'threadId': binding['thread']}).get('goal')
            if operation == 'clear' and updated is not None or operation != 'clear' and updated is None:
                raise Conflict('Goal read-back unavailable; inspect goal before retrying')
            if updated:
                if updated['threadId'] != binding['thread'] or objective is not None and updated['objective'] != objective or 'budget' in p and updated['tokenBudget'] != p['budget']:
                    raise Conflict('Goal read-back differs; no automatic retry')
                if operation == 'pause' and updated['status'] != 'paused':
                    raise Conflict('Native pause was not confirmed')
                if current and (objective is None or objective == current['objective']) and current['status'] != 'complete':
                    if updated['tokensUsed'] < current['tokensUsed'] or updated['timeUsedSeconds'] < current['timeUsedSeconds']:
                        raise Conflict('Native goal accounting unexpectedly decreased')
                    if 'budget' not in p and updated.get('tokenBudget') != current.get('tokenBudget'):
                        raise Conflict('Native goal budget changed without explicit authorization')
            current = updated
        snapshot = {'task': task, 'thread': binding['thread'], 'state': current['status'] if current else 'cleared', 'snapshot': current, 'observed': time.time()}
        self.store.put(owner, 'goal', task, snapshot)
        self.sync_goal_writer(owner, task, snapshot['state'])
        return {'goal': current, 'source': 'native', 'pause_interrupts_turn': False, 'automatic_budget_increase': False}

    def sync_goal_writer(self, owner, task, state):
        # Pause/clear stops future continuation, not an unconsumed native turn.
        # Never release from the cached task/notification state alone.
        with self.lock:
            idle = False
            if state not in {'active', 'unknown', 'applying'}:
                client = self.goal_clients.get(task)
                if client is not None:
                    binding = self.store.get(owner, 'binding', task)
                    goal = client.call('thread/goal/get', {'threadId': binding['thread']}).get('goal')
                    native = self._thread_read(client, binding)
                    idle = (not goal or goal['threadId'] == binding['thread'] and goal['status'] not in {'active', 'unknown', 'applying'}) and native.get('status', {}).get('type') == 'idle'
                    idle = idle and all(t.get('status') in {'completed', 'interrupted', 'failed'} for t in native.get('turns', []))
            with self.store.transaction():
                record = self.store._task(owner, task)
                if not idle:
                    self.store.claim_writer(record, mode='goal')
                else:
                    self.store.db.execute("UPDATE writers.claims SET mode='attempt' WHERE store=? AND task=?", (self.store.path, task))
                    if record['state'] not in {'starting', 'running', 'cancelling', 'unknown'}:
                        self.store.release_writer(task)

    def _goal_interaction(self, owner, task, method, params):
        record = self.store.task(owner, task)
        if record['state'] != 'running' or not record['attempts']:
            return self._deny_control(method, params)
        return self._interaction(owner, task, record['attempts'][-1]['id'], method, params)

    def _goal_watch(self, owner, task, thread, client):
        """Observe native continuation; never synthesize turns or increase a budget."""
        attempt = None
        stream_items = {}
        try:
            while not self.stop.is_set():
                event = client.next_event()
                if event is None:
                    raise ConnectionError('Native goal runtime disconnected')
                kind, p = event['method'], event.get('params', {})
                if p.get('threadId') != thread or 'reasoning' in kind.lower():
                    continue
                if kind == 'turn/started':
                    with self.lock:
                        record = self.store.task(owner, task)
                        if any(a['thread'] == thread and a['turn'] == p['turn']['id'] for a in record['attempts']):
                            continue
                        if record['state'] not in {'completed', 'failed', 'cancelled'}:
                            raise Conflict('Native goal turn conflicts with tracked work')
                        self.store.db.execute("UPDATE tasks SET state='pending',review_state='not_ready' WHERE id=?", (task,))
                        attempt = self.store.start(owner, task)
                        self.store.bind(owner, task, attempt, thread, p['turn']['id'])
                        self.clients[task] = client
                        stream_items = {}
                if attempt:
                    current = self.store.task(owner, task)['attempts'][-1]
                    event_turn = p.get('turnId') or p.get('turn', {}).get('id')
                    if event_turn and event_turn != current['turn']:
                        continue
                    if event_turn == current['turn']:
                        self.record_stream(owner, attempt, thread, current['turn'], kind, p, stream_items)
                if kind == 'turn/completed' and attempt:
                    current = self.store.task(owner, task)['attempts'][-1]
                    if p['turn']['id'] != current['turn'] or current['state'] in {'completed', 'failed', 'cancelled'}:
                        continue
                    status = {'completed': 'completed', 'failed': 'failed', 'interrupted': 'cancelled'}.get(p['turn']['status'])
                    if status is None:
                        raise Conflict('Unknown native goal turn outcome')
                    self.store.finish(owner, task, attempt, status, {'runtime': self.completed_turn(owner, attempt, thread, p['turn']), 'evidence_source': 'worker-reported', 'verified': False, 'thread': thread, 'turn': current['turn']})
                    self.sync_goal_writer(owner, task, self.store.get(owner, 'goal', task)['state'])
                if kind in {'thread/goal/updated', 'thread/goal/cleared'}:
                    goal = p.get('goal')
                    if goal and goal['threadId'] != thread:
                        continue
                    self.store.put(owner, 'goal', task, {'task': task, 'thread': thread, 'state': goal['status'] if goal else 'cleared', 'snapshot': goal, 'observed': time.time()})
                    self.sync_goal_writer(owner, task, goal['status'] if goal else 'cleared')
                self._emit(owner, task, attempt, kind, p, notify=kind in {'turn/completed', 'thread/goal/updated'})
        except Exception:
            if not self.closed:
                if attempt:
                    self._unknown(owner, task, attempt)
                try:
                    snapshot = self.store.get(owner, 'goal', task)
                    snapshot['state'] = 'unknown'
                    self.store.put(owner, 'goal', task, snapshot)
                except Denied:
                    pass
        finally:
            client.close()
            with self.lock:
                if self.goal_clients.get(task) is client:
                    self.goal_clients.pop(task, None)

    def native_review(self, owner, task, target, delivery='inline'):
        """Native worker review is a new attempt, not manager acceptance."""
        with self.lock:
            binding = self._idle_task(owner, task)
            if owner in self.control_busy or self.pending_plan(owner, task):
                raise Conflict('Resolve pending controls or plan before review')
            fields = {'uncommittedChanges': {'type'}, 'baseBranch': {'type', 'branch'}, 'commit': {'type', 'sha', 'title'}, 'custom': {'type', 'instructions'}}
            if not isinstance(target, dict) or target.get('type') not in fields or target.keys() - fields[target['type']]:
                raise ValueError('Invalid native review target')
            required = fields[target['type']] - {'type', 'title'}
            for key in required:
                text(target.get(key), 'Review ' + key)
            if 'title' in target and target['title'] is not None:
                text(target['title'], 'Review title', 200)
            if delivery not in {'inline', 'detached'}:
                raise ValueError('Review delivery must be inline or detached')
            if sum(t.is_alive() for t in self.threads.values()) >= self.max_workers:
                raise Conflict('Worker concurrency limit reached')
            continuation = {'thread': binding['thread'], 'text': '', 'review': {'target': target, 'delivery': delivery}}
            with self.store.transaction():
                self.store.put(owner, 'continuation', task, continuation)
                self.store.db.execute("UPDATE tasks SET state='pending',review_state='not_ready' WHERE id=?", (task,))
            self._launch(owner, task, continuation)
            return self.store.task(owner, task)

    def pending_plan(self, owner, task):
        return next((p for p in self.store.list(owner, 'plan') if p['task'] == task and p['state'] == 'pending'), None)

    def plan_action(self, owner, key, action, revision=None):
        """Only direct-user controls may decide a worker-authored plan."""
        with self.lock:
            plan = self.store.get(owner, 'plan', key)
            if action == 'show':
                return plan
            if owner in self.control_busy:
                raise Conflict('Scoped control in progress')
            if action not in {'confirm', 'continue', 'revise', 'cancel'}:
                raise ValueError('Choose show, confirm, continue, revise or cancel')
            if plan['state'] != 'pending':
                raise Conflict('Plan revision is no longer pending')
            task = self.store.task(owner, plan['task'])
            binding = self.store.get(owner, 'binding', plan['task'])
            if task['state'] != 'completed' or task['attempts'][-1]['id'] != plan['attempt'] or binding['thread'] != plan['thread']:
                raise Conflict('Plan is stale or task needs reconciliation')
            if action == 'revise':
                text(revision, 'Revision', 16000)
            with self.store.transaction():
                plan['state'] = {'confirm': 'confirmed', 'continue': 'confirmed', 'revise': 'revising', 'cancel': 'cancelled'}[action]
                self.store.put(owner, 'plan', key, plan)
                if action != 'cancel':
                    item = {'id': 'plan-' + key, 'task': plan['task'], 'text': revision if action == 'revise' else 'Continue this confirmed plan within the original approved assignment:\n' + plan['text'],
                            'mode': 'plan' if action == 'revise' else 'execute', 'state': 'queued', 'created': time.time()}
                    self.store.put(owner, 'queue', item['id'], item)
            if action != 'cancel':
                self._advance_queue(owner, plan['task'])
            return plan

    def save_plan(self, owner, task, attempt, thread, turn):
        """Record complete native plan items, never infer plans from commentary."""
        for item in turn.get('items', []):
            if item.get('type') != 'plan' or not item.get('text'):
                continue
            content = text(item['text'], 'Native plan', 100000)
            key = hashlib.sha256((attempt + '\0' + str(item.get('id')) + '\0' + content).encode()).hexdigest()
            with self.store.transaction():
                for old in self.store.list(owner, 'plan'):
                    if old['task'] == task and old['state'] == 'pending':
                        old['state'] = 'superseded'
                        self.store.put(owner, 'plan', old['id'], old)
                self.store.put(owner, 'plan', key, {'id': key, 'task': task, 'attempt': attempt, 'thread': thread, 'text': content, 'state': 'pending' if self.settings(owner).get('plan_confirmation', True) else 'recorded', 'created': time.time()})

    @staticmethod
    def model_catalog(client):
        cursor, seen, models = None, set(), {}
        while True:
            page = client.call('model/list', {'cursor': cursor, 'limit': 100, 'includeHidden': False})
            for model in page['data']:
                models[model['model']] = model
            cursor = page.get('nextCursor')
            if cursor is None:
                return list(models.values())
            if cursor in seen or len(seen) >= 100:
                raise Conflict('Model discovery pagination did not terminate')
            seen.add(cursor)

    def _settings_control(self, owner, action, p):
        self.store.task(owner, p['task'])
        if action == 'models':
            limit = p.get('limit', 100)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError('Model page size must be 1–100')
            offset = 0
            if p.get('page'):
                page = self.store.get(owner, 'model-page', p['page'])
                if page['task'] != p['task'] or page['expires'] <= time.time():
                    raise Conflict('Model page is stale or belongs to another task')
                models, offset, limit = page['data'], page['offset'], page['limit']
            else:
                with self.control_client(owner, p['task']) as client:
                    models = self.model_catalog(client)
            token = None
            if offset + limit < len(models):
                token = str(uuid.uuid4())
                self.store.put(owner, 'model-page', token, {'task': p['task'], 'data': models, 'offset': offset + limit, 'limit': limit, 'expires': time.time() + 300})
            return {'data': models[offset:offset + limit], 'next': token, 'total': len(models), 'source': 'runtime', 'complete': token is None, 'variants': self.model_variants}
        binding = self.store.get(owner, 'binding', p['task'])
        settings = self.settings(owner)
        if action == 'settings-set':
            self._idle_task(owner, p['task'])
            values = p['values']
            booleans = {'auto_queue', 'allow_archive', 'plan_confirmation', 'plan_history', 'sync_on_open', 'sync_on_complete'}
            if not isinstance(values, dict) or not values or values.keys() - booleans - {'model', 'effort', 'serviceTier', 'mode', 'locale', 'fast', 'variant'}:
                raise ValueError('Unsupported settings fields')
            if any(type(values[k]) is not bool for k in values.keys() & (booleans | {'fast'})):
                raise ValueError('Toggle settings require booleans')
            values = dict(values)
            if 'variant' in values and values['variant'] is not None:
                if values['variant'] not in self.model_variants or values.keys() & {'model', 'effort', 'serviceTier', 'fast'}:
                    raise ValueError('Choose a configured variant without conflicting model fields')
                variant = self.model_variants[values['variant']]
                if not isinstance(variant, dict) or variant.keys() - {'model', 'effort', 'serviceTier'} or 'model' not in variant:
                    raise ValueError('Configured variant is invalid')
                values.update({'model': None, 'effort': None, 'serviceTier': None} | variant)
            elif values.keys() & {'model', 'effort', 'serviceTier', 'fast'}:
                values['variant'] = None
            if 'fast' in values:
                if 'serviceTier' in values:
                    raise ValueError('Set fast or serviceTier, not both')
                values['serviceTier'] = 'fast' if values.pop('fast') else None
            candidate = settings | values
            if candidate['mode'] not in {'execute', 'plan'}:
                raise ValueError('Mode must be execute or plan')
            if candidate['locale'] not in {'en', 'zh', 'fr'}:
                raise ValueError('Supported locales: en, zh, fr')
            with self.control_client(owner, p['task']) as client:
                models = self.model_catalog(client)
            chosen = next((m for m in models if m['model'] == candidate.get('model')), None) if candidate.get('model') is not None else next((m for m in models if m['isDefault']), None)
            if chosen is None:
                raise ValueError('Selected/default model is not in the current provider catalog')
            if candidate.get('effort') is not None and candidate['effort'] not in {e['reasoningEffort'] for e in chosen['supportedReasoningEfforts']}:
                raise ValueError('Reasoning effort is unsupported by the selected model')
            if candidate.get('serviceTier') is not None and candidate['serviceTier'] not in {t['id'] for t in chosen.get('serviceTiers', []) or []}:
                raise ValueError('Service tier is unsupported by the selected model')
            settings = self.store.put(owner, 'settings', 'current', candidate)
        return {'configured': settings, 'effective': binding.get('effective', {}), 'applies': 'next authorized turn', 'task': p['task']}

    @staticmethod
    def thread_catalog(client, archived):
        cursor, seen, result = None, set(), {}
        while True:
            page = client.call('thread/list', {'archived': archived, 'cursor': cursor, 'limit': 100,
                'sourceKinds': ['cli', 'vscode', 'exec', 'appServer', 'subAgent', 'subAgentReview', 'subAgentCompact', 'subAgentThreadSpawn', 'subAgentOther', 'unknown']})
            result.update({t['id']: t for t in page['data']})
            cursor = page.get('nextCursor')
            if cursor is None:
                return result
            if cursor in seen or len(seen) >= 100:
                raise Conflict('Native thread pagination did not terminate; list is incomplete')
            seen.add(cursor)

    def _browse(self, owner, p):
        route = [self.settings(owner)['provider'], self.selected_account(owner)]
        search, archived, limit = p.get('search', ''), p.get('archived', False), p.get('limit', 20)
        if not isinstance(search, str) or len(search) > 200 or type(archived) is not bool or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid thread filter/page size')
        if p.get('page'):
            snapshot = self.store.get(owner, 'thread-page', p['page'])
            if snapshot['expires'] <= time.time() or snapshot.get('route') != route:
                raise Conflict('Thread page expired or routing changed; refresh the list')
            if set(p) != {'page'}:
                raise ValueError('Start a fresh list to change filters')
            data, offset, limit = snapshot['data'], snapshot['offset'], snapshot['limit']
        else:
            data, offset = [], 0
            catalogs = {}
            for binding in self.store.list(owner, 'binding'):
                if self.task_provider(owner, binding['task']) != route[0]:
                    continue
                try:
                    task_route = self.store.get(owner, 'route', binding['task'])
                except Denied:
                    task_route = {}
                if task_route.get('account') != route[1]:
                    continue
                home = str(self.worker_home(owner, binding['task']))
                with self.control_client(owner, binding['task']) as client:
                    if home not in catalogs:
                        catalogs[home] = self.thread_catalog(client, archived)
                    if binding['thread'] not in catalogs[home]:
                        continue
                    thread = self._thread_read(client, binding)
                binding['archived'] = archived
                self.store.put(owner, 'binding', binding['task'], binding)
                name = thread.get('name') or binding.get('name') or thread.get('preview', '')
                if search.casefold() not in (name + ' ' + binding['thread']).casefold():
                    continue
                data.append({'task': binding['task'], 'thread': binding['thread'], 'name': name,
                             'archived': archived, 'status': thread.get('status'), 'turns': thread.get('turns', [])[-3:]})
        try:
            selected = self.store.get(owner, 'selection', 'current')
        except Denied:
            selected = {}
        result = [item | {'current': item['task'] == selected.get('task')} for item in data[offset:offset + limit]]
        tokens = {}
        for label, new_offset in [('next', offset + limit), ('previous', offset - limit)]:
            tokens[label] = None
            if 0 <= new_offset < len(data):
                token = str(uuid.uuid4())
                self.store.put(owner, 'thread-page', token, {'data': data, 'offset': new_offset, 'limit': limit, 'route': route, 'expires': time.time() + 300})
                tokens[label] = token
        return {'data': result, **tokens, 'total': len(data), 'source': 'owned-runtime-threads', 'snapshot': True}
