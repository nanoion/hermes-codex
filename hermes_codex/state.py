"""Transactional task ownership and worker/manager evidence separation.

The caller must obtain owner from trusted runtime context, never tool input.
No SDK/network work occurs inside a database transaction.
"""
import json
import os
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Conflict(ValueError):
    pass


class Denied(PermissionError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def writer_directory():
    # Resolve by OS identity, never an inherited HOME/environment override.
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/hermes-codex-writers'


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise Denied('State database must not be a symlink')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=2)
        self.path = str(path.resolve())
        self.db.row_factory = sqlite3.Row
        # Same-UID hosts share one durable registry independently of profile/HOME.
        # SQLite attached rollback journals atomically commit claim + task state.
        directory = writer_directory()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            self.db.close()
            raise Denied('Unsafe shared writer registry directory')
        registry = directory / 'claims.sqlite3'
        fd = os.open(registry, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        os.close(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            self.db.close()
            raise Denied('Unsafe shared writer registry')
        self.db.execute('ATTACH DATABASE ? AS writers', (str(registry),))
        self.db.execute('CREATE TABLE IF NOT EXISTS writers.claims(store TEXT, task TEXT, roots TEXT NOT NULL, mode TEXT NOT NULL, PRIMARY KEY(store,task))')
        self.lock = threading.RLock()
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, workspace TEXT NOT NULL,
          idem TEXT NOT NULL, brief TEXT NOT NULL, writing INTEGER NOT NULL,
          state TEXT NOT NULL, review_state TEXT NOT NULL, created REAL NOT NULL,
          UNIQUE(owner,idem));
        CREATE TABLE IF NOT EXISTS attempts(
          id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
          thread TEXT, turn TEXT, state TEXT NOT NULL, cancel_reason TEXT,
          result TEXT, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS task_paths(
          task TEXT PRIMARY KEY REFERENCES tasks(id), identity TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reviews(
          id INTEGER PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
          owner TEXT NOT NULL, decision TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL);
        ''')

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise

    def close(self):
        with self.lock:
            self.db.close()

    def _task(self, owner, task_id):
        row = self.db.execute('SELECT * FROM tasks WHERE id=? AND owner=?', (task_id, owner)).fetchone()
        if row is None:
            raise Denied('Task unavailable in this ownership scope')
        return dict(row)

    def task(self, owner, task_id):
        with self.lock:
            result = self._task(owner, task_id)
            result['brief'] = json.loads(result['brief'])
            result['attempts'] = [dict(x) for x in self.db.execute('SELECT * FROM attempts WHERE task=? ORDER BY created,id', (task_id,))]
            for attempt in result['attempts']:
                attempt['result'] = json.loads(attempt['result']) if attempt['result'] else None
            return result

    def submit(self, owner, workspace, key, brief, writing, *, paths=None):
        from .access import identity
        observed = identity({'workspace': workspace, 'write_roots': brief.get('write_roots', [])})
        if paths is not None and paths != observed:
            raise Denied('Authorized filesystem roots changed')
        payload = encoded(brief)
        with self.transaction():
            old = self.db.execute('SELECT * FROM tasks WHERE owner=? AND idem=?', (owner, key)).fetchone()
            if old:
                if (old['brief'], old['workspace'], old['writing']) != (payload, workspace, int(writing)):
                    raise Conflict('Idempotency key already binds a different assignment')
                self.check_paths(owner, old['id'])
                return self.task(owner, old['id'])
            task_id = str(uuid.uuid4())
            self.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)',
                            (task_id, owner, workspace, key, payload, int(writing), 'pending', 'not_ready', time.time()))
            self.db.execute('INSERT INTO task_paths VALUES(?,?)', (task_id, encoded(observed)))
            return self.task(owner, task_id)

    def check_paths(self, owner, task_id):
        from .access import identity
        with self.lock:
            task = self._task(owner, task_id)
            brief = json.loads(task['brief'])
            row = self.db.execute('SELECT identity FROM task_paths WHERE task=?', (task_id,)).fetchone()
            if row is None or json.loads(row[0]) != identity({'workspace': task['workspace'], 'write_roots': brief.get('write_roots', [])}):
                raise Denied('Authorized filesystem identity unavailable or changed; create a new authorized assignment')

    def start(self, owner, task_id):
        with self.transaction():
            task = self._task(owner, task_id)
            if task['state'] != 'pending':
                raise Conflict('Task is not pending')
            self.check_paths(owner, task_id)
            self.claim_writer(task)
            attempt = str(uuid.uuid4())
            self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)',
                            (attempt, task_id, None, None, 'starting', None, None, time.time()))
            self.db.execute("UPDATE tasks SET state='starting' WHERE id=?", (task_id,))
            return attempt

    def claim_writer(self, task, mode='attempt'):
        """Must run within transaction; unknown claims NEVER expire by PID/timeout."""
        if not task['writing']:
            return
        brief = json.loads(task['brief']) if isinstance(task['brief'], str) else task['brief']
        roots = [str(Path(p).resolve(strict=True)) for p in brief.get('write_roots', []) or [task['workspace']]]
        for row in self.db.execute('SELECT * FROM writers.claims'):
            if row['store'] == self.path and row['task'] == task['id']:
                continue
            if any(Path(a) == Path(b) or Path(a) in Path(b).parents or Path(b) in Path(a).parents for a in roots for b in json.loads(row['roots'])):
                raise Conflict('Workspace has active or uncertain writing work in another task/store')
        self.db.execute("INSERT INTO writers.claims VALUES(?,?,?,?) ON CONFLICT(store,task) DO UPDATE SET mode=CASE WHEN excluded.mode='goal' THEN 'goal' ELSE mode END",
                        (self.path, task['id'], encoded(roots), mode))

    def release_writer(self, task_id, *, include_goal=False):
        self.db.execute("DELETE FROM writers.claims WHERE store=? AND task=? AND (mode='attempt' OR ?)", (self.path, task_id, include_goal))

    def _attempt(self, owner, task_id, attempt_id):
        self._task(owner, task_id)
        row = self.db.execute('SELECT * FROM attempts WHERE id=? AND task=?', (attempt_id, task_id)).fetchone()
        if row is None:
            raise Denied('Attempt unavailable in this task')
        return dict(row)

    def bind(self, owner, task_id, attempt_id, thread, turn):
        with self.transaction():
            attempt = self._attempt(owner, task_id, attempt_id)
            if attempt['state'] != 'starting':
                raise Conflict('Attempt cannot be rebound')
            self.db.execute("UPDATE attempts SET thread=?,turn=?,state='running' WHERE id=?", (thread, turn, attempt_id))
            self.db.execute("UPDATE tasks SET state='running' WHERE id=?", (task_id,))

    def finish(self, owner, task_id, attempt_id, state, result):
        if state not in {'completed', 'failed', 'cancelled'}:
            raise ValueError('Invalid terminal state')
        with self.transaction():
            attempt = self._attempt(owner, task_id, attempt_id)
            if attempt['state'] not in {'running', 'starting', 'cancelling', 'unknown'}:
                raise Conflict('Attempt already terminal')
            self.db.execute('UPDATE attempts SET state=?,result=? WHERE id=?', (state, encoded(result), attempt_id))
            self.db.execute("UPDATE tasks SET state=?,review_state='pending_review' WHERE id=?", (state, task_id))
            self.release_writer(task_id)

    def recover(self):
        """Call once after obtaining exclusive service ownership, never per tool call."""
        with self.transaction():
            self.db.execute("UPDATE attempts SET state='unknown' WHERE state IN ('starting','running','cancelling')")
            self.db.execute("UPDATE tasks SET state='unknown' WHERE state IN ('starting','running','cancelling')")

    def cancel(self, owner, task_id, reason):
        with self.transaction():
            task = self._task(owner, task_id)
            if task['state'] == 'pending':
                self.db.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (task_id,))
            elif task['state'] in {'starting', 'running', 'unknown', 'cancelling'}:
                self.db.execute("UPDATE tasks SET state='cancelling' WHERE id=?", (task_id,))
                self.db.execute("UPDATE attempts SET state='cancelling',cancel_reason=? WHERE task=? AND state IN ('starting','running','unknown','cancelling')", (reason, task_id))
            else:
                raise Conflict('Task already terminal')
            return self.task(owner, task_id)

    def review(self, owner, task_id, decision, evidence):
        if decision not in {'accepted', 'changes_requested', 'rejected'}:
            raise ValueError('Invalid review decision')
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('Review requires criterion evidence')
        with self.transaction():
            task = self._task(owner, task_id)
            if task['review_state'] != 'pending_review':
                raise Conflict('Task is not awaiting manager review')
            criteria = json.loads(task['brief']).get('criteria', [])
            if decision == 'accepted':
                if task['state'] != 'completed':
                    raise Conflict('Failed or cancelled assignment cannot be accepted as completed')
                if len(evidence) != len(criteria) or {e.get('criterion') for e in evidence} != set(criteria):
                    raise ValueError('Every acceptance criterion needs evidence')
                if any(e.get('status') != 'passed' or not e.get('evidence') for e in evidence):
                    raise ValueError('Acceptance requires independently verified passing evidence')
            self.db.execute('INSERT INTO reviews(task,owner,decision,evidence,created) VALUES(?,?,?,?,?)',
                            (task_id, owner, decision, encoded(evidence), time.time()))
            self.db.execute('UPDATE tasks SET review_state=? WHERE id=?', (decision, task_id))
