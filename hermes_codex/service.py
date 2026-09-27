"""Durable, scoped worker orchestration. Worker output is never authority."""
try:
    import fcntl
except ImportError:  # diagnostics remain importable on unsupported native Windows
    fcntl = None
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

from .policy import Assignment, workspace_path
from .state import Store, Conflict, Denied, encoded
from .runtime import Runtime
from .controls import Controls
from .attachments import inputs as attachment_inputs

ACTIVE = {'starting', 'running', 'cancelling', 'unknown'}
# Public streamed fields that must be assembled before durable redaction.
DELTA_STREAMS = {'item/agentMessage/delta': ('agentMessage', 'text'), 'item/plan/delta': ('plan', 'text'),
                 'item/commandExecution/outputDelta': ('commandExecution', 'aggregatedOutput'),
                 'item/fileChange/outputDelta': ('fileChange', 'aggregatedOutput')}
UNKEYED = '\x00unkeyed\x00'


def sanitized(value, *, bounded=True):
    if isinstance(value, dict):
        if value.get('type') == 'reasoning':
            # Reasoning items also arrive inside generic item/turn notifications.
            return {'type': 'reasoning', 'redacted': True}
        return {k: ('[redacted]' if any(x in k.lower().replace('_', '').replace('-', '') for x in ('token', 'secret', 'password', 'authorization', 'apikey', 'authurl', 'usercode', 'verificationcode')) and k not in {'tokenBudget', 'tokenUsage', 'tokensUsed', 'totalTokens', 'inputTokens', 'outputTokens', 'cachedInputTokens'} else sanitized(v, bounded=bounded)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitized(v, bounded=bounded) for v in (value[:100] if bounded else value)]
    if isinstance(value, str):
        from .presentation import redact_text
        value = redact_text(value)
        return value[:16000] + (' [truncated]' if len(value) > 16000 else '') if bounded else value
    return value


class WorkflowStore(Store):
    def __init__(self, path):
        super().__init__(path)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS documents(
          owner TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL,
          PRIMARY KEY(owner,kind,id));
        CREATE TABLE IF NOT EXISTS events(
          cursor INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, task TEXT NOT NULL,
          attempt TEXT, type TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
        ''')

    def has_owner(self, owner):
        """Whether durable state exists for exactly this opaque owner token."""
        with self.lock:
            return any(self.db.execute(f'SELECT 1 FROM {table} WHERE owner=? LIMIT 1', (owner,)).fetchone()
                       for table in ('tasks', 'documents', 'events', 'reviews'))

    def migrate_owner(self, source, target):
        """Atomically merge an exactly-derived legacy namespace into its canonical owner."""
        if not source or source == target:
            return
        with self.transaction():
            document_collision = self.db.execute(
                '''SELECT 1 FROM documents old JOIN documents new
                   ON new.owner=? AND new.kind=old.kind AND new.id=old.id
                   WHERE old.owner=? AND (new.value<>old.value OR old.kind IN ('account','route')) LIMIT 1''',
                (target, source)).fetchone()
            task_collision = self.db.execute(
                '''SELECT 1 FROM tasks old JOIN tasks new
                   ON new.owner=? AND new.idem=old.idem
                   WHERE old.owner=? AND new.id<>old.id LIMIT 1''', (target, source)).fetchone()
            if document_collision or task_collision:
                raise Conflict('Legacy ownership migration has conflicting records; resolve explicitly')
            for row in self.db.execute("SELECT id,value FROM documents WHERE owner=? AND kind='account'", (source,)):
                value = json.loads(row['value'])
                if not value.get('host_alias'):
                    value.setdefault('storage_owner', source)
                    self.db.execute("UPDATE documents SET value=? WHERE owner=? AND kind='account' AND id=?",
                                    (encoded(value), source, row['id']))
            task_ids = [row['id'] for row in self.db.execute('SELECT id FROM tasks WHERE owner=?', (source,))]
            for task_id in task_ids:
                row = self.db.execute("SELECT value FROM documents WHERE owner=? AND kind='route' AND id=?",
                                      (source, task_id)).fetchone()
                route = json.loads(row['value']) if row else {'provider': 'default', 'account': None}
                route.setdefault('storage_owner', source)
                self.db.execute("INSERT INTO documents VALUES(?,?,?,?) ON CONFLICT(owner,kind,id) DO UPDATE SET value=excluded.value",
                                (source, 'route', task_id, encoded(route)))
            self.db.execute('DELETE FROM documents WHERE owner=? AND EXISTS '
                            '(SELECT 1 FROM documents new WHERE new.owner=? AND new.kind=documents.kind '
                            'AND new.id=documents.id AND new.value=documents.value)', (source, target))
            for table in ('tasks', 'documents', 'events', 'reviews'):
                self.db.execute(f'UPDATE {table} SET owner=? WHERE owner=?', (target, source))

    def tasks(self, owner):
        with self.lock:
            rows = self.db.execute('SELECT * FROM tasks WHERE owner=? ORDER BY created DESC,id DESC',
                                   (owner,)).fetchall()
            return [self.task(owner, row['id']) for row in rows]

    def get(self, owner, kind, key):
        with self.lock:
            row = self.db.execute('SELECT value FROM documents WHERE owner=? AND kind=? AND id=?', (owner, kind, key)).fetchone()
            if not row:
                raise Denied('Record unavailable in this scope')
            return json.loads(row[0])

    def put(self, owner, kind, key, value):
        with self.lock:
            self.db.execute('INSERT INTO documents VALUES(?,?,?,?) ON CONFLICT(owner,kind,id) DO UPDATE SET value=excluded.value', (owner, kind, key, encoded(value)))
        return value

    def list(self, owner, kind):
        with self.lock:
            return [json.loads(x[0]) for x in self.db.execute('SELECT value FROM documents WHERE owner=? AND kind=? ORDER BY rowid', (owner, kind))]

    def event(self, owner, task, attempt, kind, payload):
        with self.lock:
            self._task(owner, task)
            if kind in DELTA_STREAMS and 'delta' in payload:
                # Never persist independent fragments: a credential prefix may
                # have arrived in an earlier delta. Publish bounded replacement
                # snapshots from the exact-turn, assembled public item instead.
                payload = {k: v for k, v in payload.items() if k != 'delta'}
                item = self.stream_item(owner, attempt, kind, payload)
                payload = payload | {'snapshot': item, 'stream_unavailable': item is None}
            self.db.execute('INSERT INTO events(owner,task,attempt,type,payload,created) VALUES(?,?,?,?,?,?)', (owner, task, attempt, kind, encoded(sanitized(payload)), time.time()))
            # Finite per-task retained stream, with cursors exposing eviction gaps.
            self.db.execute('DELETE FROM events WHERE task=? AND cursor NOT IN (SELECT cursor FROM events WHERE task=? ORDER BY cursor DESC LIMIT 2000)', (task, task))

    def stream_item(self, owner, attempt, kind, payload):
        """Assembled, already-sanitized exact-turn item for a durable event."""
        try:
            stream = self.get(owner, 'stream', attempt)
        except Denied:
            return None
        if (stream.get('thread'), stream.get('turn')) != (payload.get('threadId'), payload.get('turnId')):
            return None
        key = payload.get('itemId')
        if key:
            return next((i for i in stream.get('items', []) if i.get('id') == key), None)
        item_type = DELTA_STREAMS[kind][0]
        item = next((i for i in stream.get('unkeyed', []) if i.get('type') == item_type), None)
        return item and {k: v for k, v in item.items() if k != 'id'}  # never expose the internal assembly key


class Service(Controls):
    def __init__(self, home, allowed_roots, *, transport_factory=Runtime, notify=None, max_workers=4, desktop_command=None, model_variants=None, provider_profiles=None, login_presenter=None, allow_full_access=False, device_login_presenter=None, attachment_roots=None, host_account_homes=None):
        from .runtime import capabilities
        if not capabilities()['implementation_available'] or fcntl is None:
            raise FileNotFoundError('Worker requires POSIX safety primitives; native Windows is not qualified')
        if type(max_workers) is not int or not 1 <= max_workers <= 32 or type(allow_full_access) is not bool:
            raise ValueError('max_workers must be 1–32 and allow_full_access must be boolean')
        if not isinstance(allowed_roots, list) or not isinstance(attachment_roots or [], list):
            raise ValueError('Workspace/attachment roots must be lists')
        self.roots = [workspace_path(p, [p]) for p in allowed_roots]
        self.attachment_roots = [workspace_path(p, [p]) for p in (attachment_roots or [])]
        if not isinstance(host_account_homes or {}, dict):
            raise ValueError('host_account_homes must map approved aliases to paths')
        self.host_account_homes = {name: workspace_path(path, [path]) for name, path in (host_account_homes or {}).items()}
        self.profiles = {'default': {'modelProvider': None, 'defaults': {}}} | (provider_profiles or {})
        for name, profile in self.profiles.items():
            if not isinstance(name, str) or not name or not isinstance(profile, dict) or profile.keys() - {'label', 'modelProvider', 'defaults', 'capabilities'}:
                raise ValueError('Invalid provider profile configuration')
            defaults = profile.get('defaults', {})
            if not isinstance(defaults, dict) or defaults.keys() - {'model', 'effort', 'serviceTier', 'mode'}:
                raise ValueError('Provider defaults may only specify model, effort, serviceTier, mode')
            if profile.get('modelProvider') is not None and (not isinstance(profile['modelProvider'], str) or not profile['modelProvider'].strip()):
                raise ValueError('modelProvider must be a native configured provider identifier')
        self.home = Path(home)
        if self.home.is_symlink():
            raise Denied('State home must not be a symlink')
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lease = open(self.home / 'service.lock', 'a+')
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lease.close()
            raise Conflict('Another worker host owns this profile') from None
        self.store = WorkflowStore(self.home / 'state.sqlite3')
        self.store.recover()
        # Lost SDK callbacks cannot be resurrected or replayed after process death.
        with self.store.lock:
            rows = self.store.db.execute("SELECT owner,kind,id,value FROM documents WHERE kind='interaction'").fetchall()
            for owner, kind, key, raw in rows:
                value = json.loads(raw)
                if value['state'] in {'pending', 'decided'}:
                    value['state'] = 'stale'
                    self.store.put(owner, kind, key, value)
        self.allow_full_access = allow_full_access
        self.factory = transport_factory
        self.notify = notify or (lambda owner, message: False)
        self.max_workers = max_workers
        self.desktop_command = desktop_command
        self.model_variants = model_variants or {}
        self.login_presenter = login_presenter
        self.device_login_presenter = device_login_presenter
        self.lock = threading.RLock()
        self.clients = {}
        self.submissions = {}  # live dispatch intent only; never recovered/replayed
        self.threads = {}
        self.stop = threading.Event()
        self.closed = False
        self.init_controls()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        for client in list(self.clients.values()):
            client.close()
        for thread in list(self.threads.values()):
            thread.join()
        for thread in list(self.control_threads.values()):
            thread.join()
        for client in list(self.account_clients.values()):
            client.close()
        self.login_links.clear()
        for client in list(self.goal_clients.values()):
            client.close()
        for thread in list(self.goal_threads.values()):
            thread.join()
        self.store.recover()
        self.store.close()
        fcntl.flock(self.lease, fcntl.LOCK_UN)
        self.lease.close()

    def propose(self, owner, payload):
        assignment = Assignment.model_validate(payload)
        brief = assignment.model_dump()
        clean = {key: sanitized(value, bounded=False) for key, value in brief.items()}
        for field in ('workspace', 'write_roots', 'idempotency_key', 'authorization_ref'):
            if clean[field] != brief[field]:
                raise Denied('Credentials are not permitted in assignment identifiers or paths')
        brief = Assignment.model_validate(clean).model_dump()
        from .access import validate
        brief = validate(brief, self.roots, self.allow_full_access)
        digest = hashlib.sha256(encoded([brief, self.settings(owner)['provider'], self.selected_account(owner)]).encode()).hexdigest()
        key = digest[:32]
        try:
            return self.store.get(owner, 'proposal', key)
        except Denied:
            return self.store.put(owner, 'proposal', key, {'id': key, 'brief': brief, 'digest': digest, 'provider': self.settings(owner)['provider'], 'account': self.selected_account(owner), 'authorized': False})

    def authorize(self, owner, proposal_id):
        """Only called by a direct user slash-command boundary; never a model tool."""
        with self.lock:
            proposal = self.store.get(owner, 'proposal', proposal_id)
            from .access import validate, identity
            validate(proposal['brief'], self.roots, self.allow_full_access)
            proposal.update(authorized=True, authorized_at=time.time(), expires=time.time() + 900, paths=identity(proposal['brief']))
            return self.store.put(owner, 'proposal', proposal_id, proposal)

    def submit(self, owner, proposal_id):
        with self.lock:
            if owner in self.control_busy:
                raise Conflict('Scoped control in progress')
            proposal = self.store.get(owner, 'proposal', proposal_id)
            if not proposal['authorized'] or proposal['expires'] < time.time():
                raise Denied('Direct user authorization required for this exact assignment')
            if proposal.get('provider', 'default') != self.settings(owner)['provider'] or proposal.get('account') != self.selected_account(owner):
                raise Conflict('Select the authorized proposal provider before submission')
            brief = proposal['brief']
            from .access import validate, identity
            validate(brief, self.roots, self.allow_full_access)
            if identity(brief) != proposal.get('paths'):
                raise Denied('Authorized filesystem roots changed; request a new authorization')
            task = self.store.submit(owner, brief['workspace'], brief['idempotency_key'], brief, bool(brief['write_roots']), paths=proposal['paths'])
            try:
                route = self.store.get(owner, 'route', task['id'])
                if (route['provider'], route.get('account')) != (proposal.get('provider', 'default'), proposal.get('account')):
                    raise Conflict('Idempotency key already belongs to another provider')
            except Denied:
                self.store.put(owner, 'route', task['id'], {'provider': proposal.get('provider', 'default'), 'account': proposal.get('account')})
            if task['state'] == 'pending' and task['id'] not in self.threads:
                if sum(t.is_alive() for t in self.threads.values()) >= self.max_workers:
                    raise Conflict('Worker concurrency limit reached; pending assignment retained')
                self._launch(owner, task['id'])
            return self.store.task(owner, task['id'])

    def queue_items(self, owner, task_id):
        self.store.task(owner, task_id)
        return [q for q in self.store.list(owner, 'queue') if q['task'] == task_id and q['state'] == 'queued']

    def followup(self, owner, task_id, key, text, *, attachment_ids=None, _from_control=False):
        """Direct-user scoped continuation; does not alter the approved sandbox."""
        if not isinstance(key, str) or not 1 <= len(key) <= 200 or not isinstance(text, str) or not 1 <= len(text.strip()) <= 16000:
            raise ValueError('A bounded follow-up and idempotency key are required')
        if sanitized(key, bounded=False) != key:
            raise Denied('Credentials are not permitted in idempotency keys')
        text = sanitized(text, bounded=False)
        with self.lock:
            if owner in self.control_busy and not _from_control:
                raise Conflict('Scoped control in progress')
            task = self.store.task(owner, task_id)
            self.require_route(owner, task_id)
            self.store.check_paths(owner, task_id)
            if self.pending_plan(owner, task_id) or self.goal_blocks(owner, task_id):
                raise Conflict('Resolve pending plan/native goal before continuing')
            if task['state'] in {'unknown', 'cancelling', 'cancelled', 'failed'}:
                raise Conflict('Reconcile or explicitly recover the task before continuing')
            item_id = hashlib.sha256(encoded([task_id, key]).encode()).hexdigest()
            try:
                previous = self.store.get(owner, 'queue', item_id)
            except Denied:
                previous = None
            if previous:
                if previous['text'] != text or previous.get('attachment_ids') != attachment_ids:
                    raise Conflict('Follow-up idempotency conflict')
                return previous
            if task['state'] in ACTIVE and not self.settings(owner)['auto_queue']:
                raise Conflict('Automatic queuing is disabled')
            item = {'id': item_id, 'task': task_id, 'text': text, 'state': 'queued', 'created': time.time(), 'attachment_ids': attachment_ids}
            from .attachments import consume
            with self.store.transaction():
                item['attachments'] = consume(self, owner, task_id, item_id, attachment_ids)
                self.store.put(owner, 'queue', item_id, item)
            self._advance_queue(owner, task_id)
            return self.store.get(owner, 'queue', item_id)

    def queue_action(self, owner, task_id, action, item_id=None):
        with self.lock:
            task = self.store.task(owner, task_id)
            items = self.queue_items(owner, task_id)
            if action in {'clear', 'cancel-next'}:
                for item in items if action == 'clear' else items[:1]:
                    item['state'] = 'cancelled'
                    self.store.put(owner, 'queue', item['id'], item)
                return self.queue_items(owner, task_id)
            if action != 'steer' or task['state'] != 'running':
                raise Conflict('Steering requires the exact live running task')
            item = next((q for q in items if q['id'] == item_id), None)
            if item is None:
                raise Conflict('Queued input is stale or unavailable')
            attempt = task['attempts'][-1]
            client = self.clients.get(task_id)
            if not client:
                raise Conflict('Runtime is not connected')
            # Enqueue-time validation does not authorize a replacement inode.
            # Deny before consuming the queue or sending any deferred input.
            self.store.check_paths(owner, task_id)
            item['state'] = 'uncertain_steer'
            item['attempt'] = attempt['id']
            item['thread'], item['turn'] = attempt['thread'], attempt['turn']
            self.store.put(owner, 'queue', item_id, item)
            # No automatic retry: server might have consumed input before disconnect.
            ack = client.call('turn/steer', {'threadId': attempt['thread'], 'expectedTurnId': attempt['turn'], 'input': [{'type': 'text', 'text': item['text']}] + attachment_inputs(item.get('attachments', []))})
            if ack.get('turnId') != attempt['turn']:
                raise Conflict('Steering acknowledgement has another/missing turn; reconcile without replay')
            item['state'] = 'steered'
            return self.store.put(owner, 'queue', item_id, item)

    def _advance_queue(self, owner, task_id):
        with self.lock:
            if self.closed:
                return
            task = self.store.task(owner, task_id)
            items = self.queue_items(owner, task_id)
            if any(q.get('task') == task_id and q['state'] == 'uncertain_steer' for q in self.store.list(owner, 'queue')):
                return
            if task['state'] != 'completed' or not items or self.pending_plan(owner, task_id) or self.goal_blocks(owner, task_id):
                return
            if sum(t.is_alive() for key, t in self.threads.items() if key != task_id) >= self.max_workers:
                return
            self.store.check_paths(owner, task_id)
            item = items[0]
            binding = self.store.get(owner, 'binding', task_id)
            with self.store.transaction():
                item['state'] = 'submitted'
                self.store.put(owner, 'queue', item['id'], item)
                continuation = {'thread': binding['thread'], 'text': item['text'], 'mode': item.get('mode', self.settings(owner)['mode']), 'attachments': item.get('attachments', [])}
                self.store.put(owner, 'continuation', task_id, continuation)
                self.store.db.execute("UPDATE tasks SET state='pending',review_state='not_ready' WHERE id=?", (task_id,))
            self._launch(owner, task_id, continuation)

    def _launch(self, owner, task_id, continuation=None):
        if continuation is None:
            try:
                continuation = self.store.get(owner, 'continuation', task_id)
            except Denied:
                pass
        attempt = self.store.start(owner, task_id)
        thread = threading.Thread(target=self._run, args=(owner, task_id, attempt, continuation), daemon=True, name='hermes-codex-worker')
        self.threads[task_id] = thread
        thread.start()

    def _emit(self, owner, task_id, attempt, kind, payload, notify=False):
        self.store.event(owner, task_id, attempt, kind, payload)
        if notify:
            try:
                accepted = bool(self.notify(owner, f'Codex task {task_id}: {kind}. Retrieve scoped status/events; worker output is untrusted.'))
            except Exception:
                accepted = False
            self.store.event(owner, task_id, attempt, 'notification', {'accepted_for_delivery': accepted, 'delivered': None})

    def record_stream(self, owner, attempt, thread, turn, kind, payload, items):
        """Accumulate exact-turn items before bounded preview eviction/truncation."""
        if kind in {'item/started', 'item/completed'}:
            item = payload.get('item', {})
            if item.get('type') == 'reasoning' or not item.get('id'):
                return
            items[item['id']] = dict(item)
        elif kind in DELTA_STREAMS:
            item_type, field = DELTA_STREAMS[kind]
            # Item-less deltas share one assembly key per exact turn so a
            # credential split across them is redacted as one value.
            key = payload.get('itemId') or UNKEYED + thread + '\0' + turn
            item = items.setdefault(key, {'id': key, 'type': item_type, field: ''})
            item[field] = (item.get(field) or '') + payload['delta']
        else:
            return
        # Unkeyed deltas stay out of mergeable items: they are progress text,
        # not authoritative turn items.
        self.store.put(owner, 'stream', attempt, {'thread': thread, 'turn': turn,
            'items': sanitized([i for k, i in items.items() if not k.startswith(UNKEYED)], bounded=False),
            'unkeyed': sanitized([i for k, i in items.items() if k.startswith(UNKEYED)], bounded=False)})

    def completed_turn(self, owner, attempt, thread, turn):
        """Merge durable stream with terminal items by ID, terminal data wins."""
        try:
            stream = self.store.get(owner, 'stream', attempt)
        except Denied:
            stream = {}
        retained = stream.get('items', []) if (stream.get('thread'), stream.get('turn')) == (thread, turn['id']) else []
        terminal = sanitized(turn, bounded=False)
        items = terminal.get('items') or []
        ids = {i.get('id') for i in items if i.get('id')}
        return terminal | {'items': [i for i in retained if i.get('id') not in ids] + items}

    def _run(self, owner, task_id, attempt, continuation):
        stream_items = {}
        client = None
        timer = None
        submitted = False
        try:
            task = self.store.task(owner, task_id)
            brief = {key: sanitized(value, bounded=False) for key, value in task['brief'].items()}
            if continuation:
                continuation = sanitized(continuation, bounded=False)
            from .access import validate
            validate(brief, self.roots, self.allow_full_access)
            self.store.check_paths(owner, task_id)
            home = self.worker_home(owner, task_id)
            client = self.factory(home, approval_handler=lambda method, params: self._interaction(owner, task_id, attempt, method, params))
            self.clients[task_id] = client
            if self.closed:
                return
            account = client.call('account/read', {'refreshToken': False})
            if account.get('requiresOpenaiAuth', True) and account.get('account') is None:
                raise Denied('Worker identity is unauthenticated; authorize isolated account setup')
            if self.store.task(owner, task_id)['state'] == 'cancelling':
                self.store.finish(owner, task_id, attempt, 'cancelled', {'summary': 'Stopped before turn submission', 'evidence_source': 'runtime'})
                return
            settings = self.settings(owner)
            provider = self.task_provider(owner, task_id)
            if settings['provider'] != provider:
                raise Conflict('Task provider changed; refusing to route work elsewhere')
            profile = self.profiles[provider]
            provider_params = {'modelProvider': profile['modelProvider']} if profile.get('modelProvider') else {}
            from .access import thread_options
            mode = continuation.get('mode', settings['mode']) if continuation else settings['mode']
            effective_brief = brief | {'sandbox': 'read-only', 'write_roots': [], 'network': False} if mode == 'plan' or continuation and continuation.get('review') else brief
            options = thread_options(effective_brief)
            if continuation:
                response = client.call('thread/resume', {'threadId': continuation['thread'], **options, **provider_params})
            else:
                response = client.call('thread/start', {**options, 'model': settings.get('model'), 'developerInstructions': '\n'.join(brief['instructions']), **provider_params})
            expected = self.sandbox(effective_brief)
            if any(response.get('sandbox', {}).get(k) != v for k, v in expected.items()):
                raise Denied('Native policy read-back differs from authorized scope')
            if profile.get('modelProvider') and response.get('modelProvider') != profile['modelProvider']:
                raise Conflict('Effective native provider differs from the selected profile')
            thread_id = response['thread']['id']
            if continuation and thread_id != continuation['thread']:
                raise Conflict('Resume returned a different thread')
            self.store.put(owner, 'binding', task_id, {'task': task_id, 'thread': thread_id, 'effective': response, 'provider': settings.get('provider', 'default'), 'policy_verified': True, 'mode': mode})
            text = continuation['text'] if continuation else encoded(brief)
            params = {'threadId': thread_id, 'input': [{'type': 'text', 'text': text}], 'approvalPolicy': 'on-request', 'sandboxPolicy': self.sandbox(effective_brief), 'cwd': brief['workspace']}
            if continuation:
                params['input'].extend(attachment_inputs(continuation.get('attachments', [])))
            for field in ('model', 'effort', 'serviceTier'):
                if settings.get(field) is not None:
                    params[field] = settings[field]
            self.store.put(owner, 'execution', attempt, {'task': task_id, 'attempt': attempt, 'mode': mode, 'sandbox': self.sandbox(effective_brief)})
            effective_model = settings.get('model') or response.get('model')
            if not effective_model:
                raise Conflict('Runtime did not report an effective model for collaboration mode')
            params['collaborationMode'] = {'mode': 'plan' if mode == 'plan' else 'default',
                'settings': {'model': effective_model, 'reasoning_effort': settings.get('effort'), 'developer_instructions': None}}
            self.store.check_paths(owner, task_id)
            submitted = True  # a crash beyond here is uncertain, never auto-retry
            if continuation and continuation.get('review'):
                response = client.call('review/start', {'threadId': thread_id, **continuation['review']})
                review_thread = response['reviewThreadId']
                if continuation['review']['delivery'] == 'inline' and review_thread != thread_id:
                    raise Conflict('Inline review returned another thread')
                origin_thread = thread_id
                thread_id = review_thread
                self.store.put(owner, 'binding', task_id, {'task': task_id, 'thread': thread_id, 'origin_thread': origin_thread, 'effective': params, 'provider': settings.get('provider', 'default')})
            else:
                with self.store.lock:
                    self.store.db.execute('UPDATE attempts SET thread=? WHERE id=?', (thread_id, attempt))
                    self.submissions[attempt] = thread_id
                response = client.call('turn/start', params)
            turn_id = response['turn']['id']
            # Bind before consuming early buffered events.
            with self.store.transaction():
                current = self.store._attempt(owner, task_id, attempt)
                if current['turn'] is not None and (current['thread'], current['turn']) != (thread_id, turn_id):
                    raise Conflict('Start response disagrees with early runtime request; reconcile without replay')
                if current['state'] == 'cancelling':
                    self.store.db.execute('UPDATE attempts SET thread=?,turn=? WHERE id=?', (thread_id, turn_id, attempt))
                else:
                    self.store.db.execute("UPDATE attempts SET thread=?,turn=?,state='running' WHERE id=?", (thread_id, turn_id, attempt))
                    self.store.db.execute("UPDATE tasks SET state='running' WHERE id=?", (task_id,))
            self._emit(owner, task_id, attempt, 'running', {'thread': thread_id, 'turn': turn_id})
            timer = threading.Timer(brief['timeout_seconds'], lambda: self._timeout(owner, task_id))
            timer.daemon = True
            timer.start()
            if self.store.task(owner, task_id)['state'] == 'cancelling':
                client.call('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id})
            while not self.stop.is_set():
                event = client.next_event()
                if event is None:
                    raise ConnectionError('Runtime disconnected')
                kind, payload = event['method'], event.get('params', {})
                if 'reasoning' in kind.lower():
                    continue
                if payload.get('threadId') != thread_id:
                    continue
                event_turn = payload.get('turnId') or payload.get('turn', {}).get('id')
                if event_turn and event_turn != turn_id:
                    continue
                if event_turn == turn_id:
                    self.record_stream(owner, attempt, thread_id, turn_id, kind, payload, stream_items)
                self._emit(owner, task_id, attempt, kind, payload)
                if kind == 'turn/completed':
                    turn = self.completed_turn(owner, attempt, thread_id, payload['turn'])
                    status = {'completed': 'completed', 'interrupted': 'cancelled', 'failed': 'failed'}.get(turn['status'])
                    if status is None:
                        raise ValueError('Unrecognized terminal status')
                    result = {'thread': thread_id, 'turn': turn_id, 'evidence_source': 'worker-reported', 'runtime': sanitized(turn, bounded=False), 'verified': False}
                    if status == 'completed' and mode == 'plan':
                        self.save_plan(owner, task_id, attempt, thread_id, turn)
                    self.store.finish(owner, task_id, attempt, status, result)
                    self._emit(owner, task_id, attempt, status, result, notify=True)
                    if settings.get('sync_on_complete'):
                        self._emit(owner, task_id, attempt, 'desktop-sync', self.desktop_sync(thread_id))
                    break
        except Exception as exc:
            if not self.closed:
                if submitted:
                    self._unknown(owner, task_id, attempt)
                else:
                    try:
                        self.store.finish(owner, task_id, attempt, 'failed', {'error': type(exc).__name__, 'message': sanitized(str(exc)), 'evidence_source': 'runtime'})
                    except Conflict:
                        pass
                self._emit(owner, task_id, attempt, 'error', {'code': type(exc).__name__, 'message': sanitized(str(exc)), 'uncertain_submission': submitted}, notify=True)
        finally:
            with self.store.lock:
                self.submissions.pop(attempt, None)
            if timer:
                timer.cancel()
            if client:
                client.close()
            if not self.closed:
                self._advance_queue(owner, task_id)

    @staticmethod
    def sandbox(brief):
        from .access import sandbox
        return sandbox(brief)

    def _unknown(self, owner, task, attempt):
        with self.store.transaction():
            current = self.store._attempt(owner, task, attempt)
            if current['state'] in ACTIVE:
                self.store.db.execute("UPDATE attempts SET state='unknown' WHERE id=?", (attempt,))
                self.store.db.execute("UPDATE tasks SET state='unknown' WHERE id=?", (task,))

    def _timeout(self, owner, task_id):
        try:
            self.cancel(owner, task_id, 'timeout')
        except (Conflict, Denied):
            pass

    def status(self, owner, task_id):
        return sanitized(self.store.task(owner, task_id))

    def events(self, owner, task_id, cursor=0, limit=100):
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Invalid event cursor/limit')
        with self.store.lock:
            self.store._task(owner, task_id)
            rows = self.store.db.execute('SELECT * FROM events WHERE owner=? AND task=? AND cursor>? ORDER BY cursor LIMIT ?', (owner, task_id, cursor, limit)).fetchall()
            return [dict(row) | {'payload': json.loads(row['payload'])} for row in rows]

    def result(self, owner, task_id):
        task = self.store.task(owner, task_id)
        if not task['attempts'] or task['attempts'][-1]['result'] is None:
            raise Conflict('Worker result is not available')
        return sanitized(task['attempts'][-1]['result'])

    def present(self, owner, kind, key, *, page=None):
        """Plain-text immutable pages; no live buttons or markup execution surface."""
        from .presentation import chunks, TITLES, WARNINGS
        if page:
            snapshot = self.store.get(owner, 'presentation', page)
            if snapshot['kind'] != kind or snapshot['key'] != key or snapshot['expires'] <= time.time():
                raise Conflict('Presentation expired or selection changed; refresh the view')
            parts, offset = snapshot['parts'], snapshot['offset']
        else:
            locale = self.settings(owner).get('locale', 'en')
            if kind == 'result':
                record = self.store.task(owner, key)
                if not record['attempts'] or record['attempts'][-1]['result'] is None:
                    raise Conflict('No result available')
                value = record['attempts'][-1]['result']
            elif kind in {'plan', 'interaction', 'control'}:
                value = self.store.get(owner, kind, key)
            elif kind in {'status', 'queue', 'history'}:
                record = self.store.task(owner, key)
                value = record if kind == 'status' else [q for q in self.store.list(owner, 'queue') if q['task'] == key] if kind == 'queue' else record['attempts']
            else:
                raise ValueError('Presentation supports status/result/plan/interaction/control/queue/history')
            body = json.dumps(sanitized(value, bounded=False), ensure_ascii=False, indent=2)
            parts = chunks(TITLES.get(locale, TITLES['en'])[kind] + '\n' + WARNINGS.get(locale, WARNINGS['en']) + '\n' + body)
            offset = 0
        token = None
        if offset + 1 < len(parts):
            token = str(uuid.uuid4())
            self.store.put(owner, 'presentation', token, {'kind': kind, 'key': key, 'parts': parts, 'offset': offset + 1, 'expires': time.time() + 900})
        return {'text': parts[offset], 'next': token, 'page': offset + 1, 'pages': len(parts), 'format': 'plain-text', 'trusted': False}

    def result_page(self, owner, task_id, *, attempt=None, cursor=0):
        """Bounded pages of immutable sanitized JSON; pin an attempt after page one."""
        if type(cursor) is not int or cursor < 0:
            raise ValueError('Invalid result cursor')
        if cursor and attempt is None:
            raise Conflict('Pin the result attempt before requesting another page')
        task = self.store.task(owner, task_id)
        selected = next((a for a in task['attempts'] if a['id'] == attempt), None) if attempt else (task['attempts'][-1] if task['attempts'] else None)
        if selected is None or selected['result'] is None:
            raise Conflict('Worker result is not available for this attempt')
        content = encoded(sanitized(selected['result'], bounded=False))
        if cursor >= len(content) and cursor:
            raise ValueError('Result cursor is beyond the available output')
        end = min(cursor + 12000, len(content))
        return {'attempt': selected['id'], 'text': content[cursor:end], 'next': end if end < len(content) else None,
                'total_characters': len(content), 'format': 'json', 'trusted': False}

    def settings(self, owner):
        try:
            return self.store.get(owner, 'settings', 'current')
        except Denied:
            return {'provider': 'default', 'auto_queue': True, 'mode': 'execute', 'locale': 'en'}

    def cancel(self, owner, task_id, reason='manager request'):
        task = self.store.cancel(owner, task_id, reason)
        if task['state'] == 'cancelled':
            return task
        attempt = task['attempts'][-1]
        def interrupt():
            try:
                if attempt['turn'] and task_id in self.clients:
                    self.clients[task_id].call('turn/interrupt', {'threadId': attempt['thread'], 'turnId': attempt['turn']})
            except Exception:
                self._unknown(owner, task_id, attempt['id'])
        thread = threading.Thread(target=interrupt, daemon=True)
        thread.start()
        return task

    def _live_interaction(self, owner, request):
        attempt = self.store._attempt(owner, request['task'], request['attempt'])
        live = attempt['state'] == 'running' or (attempt['state'] == 'starting' and request['attempt'] in self.submissions and self.submissions[request['attempt']] == request['thread'])
        return (not self.stop.is_set() and live and attempt['thread'] == request['thread'] and attempt['turn'] == request['turn'])

    def decide(self, owner, request_id, decision):
        """Trusted direct-user boundary only. Never register as a model tool."""
        if decision not in {'accept', 'acceptForSession', 'decline', 'cancel'}:
            raise ValueError('Unsupported approval decision')
        with self.store.lock:
            request = self.store.get(owner, 'interaction', request_id)
            if request['method'] not in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}:
                raise Conflict('This request requires structured input, not approval')
            if request['state'] != 'pending':
                raise Conflict('Interaction already resolved')
            if request['expires'] <= time.time() or not self._live_interaction(owner, request):
                request.update(state='stale', response={'decision': 'decline'})
                self.store.put(owner, 'interaction', request_id, request)
                raise Conflict('Interaction expired or runtime binding is no longer live')
            request.update(state='decided', response={'decision': decision})
            self.store.put(owner, 'interaction', request_id, request)
            return sanitized(request)

    def answer(self, owner, request_id, action, *, question=None, answers=None):
        """Direct user input; answering a question never grants approval."""
        with self.store.lock:
            request = self.store.get(owner, 'interaction', request_id)
            if request['method'] != 'item/tool/requestUserInput' or request['state'] != 'pending':
                raise Conflict('No pending structured input')
            if request['expires'] <= time.time() or not self._live_interaction(owner, request):
                raise Conflict('Structured input is stale')
            questions = {q['id']: q for q in request['details']['questions']}
            draft = request.setdefault('draft', {})
            if action == 'edit':
                if question not in questions or not isinstance(answers, list) or not answers or any(not isinstance(a, str) or not a.strip() or len(a) > 8000 for a in answers):
                    raise ValueError('Invalid question or answer')
                q = questions[question]
                options = q.get('options')
                if options and not q.get('isOther') and any(a not in {o['label'] for o in options} for a in answers):
                    raise ValueError('Select an available option')
                draft[question] = {'answers': answers}
            elif action == 'back':
                request['current_question'] = max(0, request.get('current_question', 0) - 1)
            elif action in {'submit', 'cancel'}:
                if action == 'submit' and set(draft) != set(questions):
                    raise ValueError('Every question requires an answer')
                request.update(state='decided', response={'answers': draft if action == 'submit' else {}})
            else:
                raise ValueError('Unknown input action')
            return self.store.put(owner, 'interaction', request_id, request)

    def _interaction(self, owner, task_id, attempt, method, params):
        if method not in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval', 'item/tool/requestUserInput'}:
            raise Denied('Unsupported server interaction')
        is_input = method == 'item/tool/requestUserInput'
        denied = {'answers': {}} if is_input else {'decision': 'decline'}
        if is_input:
            questions = params.get('questions')
            if not isinstance(questions, list) or not questions or len(questions) > 20:
                return denied
            # Secret questions need a secure UI transport, never task text or SQLite.
            if any(not isinstance(q, dict) or not q.get('id') or q.get('isSecret') for q in questions):
                return denied
            if len({q['id'] for q in questions}) != len(questions):
                return denied
        request = {'id': str(uuid.uuid4()), 'task': task_id, 'attempt': attempt,
                   'thread': params.get('threadId'), 'turn': params.get('turnId'),
                   'item': params.get('itemId'), 'method': method,
                   'details': sanitized(params), 'state': 'pending',
                   'expires': time.time() + 300}
        with self.store.lock:
            current = self.store._attempt(owner, task_id, attempt)
            # SDK callbacks run on its sole reader: waiting for turn/start here
            # deadlocks. A request on this live, exact-thread dispatch can attest
            # the turn first. Pin once and verify the eventual response agrees.
            if (request['item'] and isinstance(request['thread'], str) and request['thread'] and
                    isinstance(request['turn'], str) and request['turn'] and attempt in self.submissions and
                    current['state'] == 'starting' and current['turn'] is None and
                    self.submissions.get(attempt) == request['thread'] == current['thread']):
                previous = self.store.db.execute('SELECT 1 FROM attempts WHERE task=? AND thread=? AND turn=? AND id<>?',
                    (task_id, request['thread'], request['turn'], attempt)).fetchone()
                if previous is None:
                    self.store.db.execute('UPDATE attempts SET turn=? WHERE id=?', (request['turn'], attempt))
            if not request['item'] or not self._live_interaction(owner, request):
                return denied
            # A duplicate callback cannot create another grant for the same item.
            if any((r['attempt'], r['method'], r['item']) == (attempt, method, request['item'])
                   for r in self.store.list(owner, 'interaction')):
                return denied
            self.store.put(owner, 'interaction', request['id'], request)
        self._emit(owner, task_id, attempt, 'input_required' if is_input else 'approval_required', request, notify=True)
        while not self.stop.wait(.02):
            with self.store.lock:
                current = self.store.get(owner, 'interaction', request['id'])
                if current['expires'] <= time.time() or not self._live_interaction(owner, current):
                    current.update(state='stale', response=denied)
                    self.store.put(owner, 'interaction', request['id'], current)
                    return denied
                if current['state'] == 'decided':
                    current['state'] = 'consumed'
                    self.store.put(owner, 'interaction', request['id'], current)
                    return current['response']
                if current['state'] != 'pending':
                    return denied
        return denied
