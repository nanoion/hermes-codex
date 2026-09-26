"""SDK-owned transport. No shell launch templates or model-facing RPC escape hatch."""
import os
import sys
import threading
from pathlib import Path

from openai_codex.client import CodexClient, CodexConfig
from openai_codex.generated import v2_all as models

# Audited operations only; generated response validation stays on every SDK call.
OPERATIONS = {
    'account/read': 'GetAccountResponse', 'account/rateLimits/read': 'GetAccountRateLimitsResponse',
    'account/login/start': 'LoginAccountResponse', 'account/login/cancel': 'CancelLoginAccountResponse',
    'account/logout': 'LogoutAccountResponse', 'model/list': 'ModelListResponse',
    'thread/start': 'ThreadStartResponse', 'thread/resume': 'ThreadResumeResponse',
    'thread/read': 'ThreadReadResponse', 'thread/list': 'ThreadListResponse',
    'thread/name/set': 'ThreadSetNameResponse', 'thread/archive': 'ThreadArchiveResponse',
    'thread/unarchive': 'ThreadUnarchiveResponse', 'turn/start': 'TurnStartResponse',
    'turn/steer': 'TurnSteerResponse', 'turn/interrupt': 'TurnInterruptResponse',
    'review/start': 'ReviewStartResponse', 'thread/goal/get': 'ThreadGoalGetResponse',
    'thread/goal/set': 'ThreadGoalSetResponse', 'thread/goal/clear': 'ThreadGoalClearResponse',
}


def capabilities():
    platform = sys.platform
    available = platform.startswith('linux') or platform == 'darwin'
    return {'platform': platform, 'implementation_available': available, 'full_platform_acceptance': False,
            'prerequisites': ['Python 3.11+', 'openai-codex==0.157.0', 'POSIX flock, dirfd and O_NOFOLLOW', 'local filesystem with SQLite rollback-journal locking'],
            'desktop': 'requires configured launcher and graphical session',
            'status': 'Linux native non-model checks available; macOS unqualified; Windows native host blocked (POSIX primitives required), WSL requires separate qualification'}


class Runtime:
    def __init__(self, home, *, approval_handler):
        if not capabilities()['implementation_available']:
            raise FileNotFoundError('Native worker host is not qualified for this platform; inspect capabilities')
        home = Path(home)
        if home.is_symlink():
            raise PermissionError('Worker home must not be a symlink')
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        # SDK env overlays os.environ, so explicitly blank every inherited key.
        env = dict.fromkeys(os.environ, '')
        env.update(HOME=str(home), CODEX_HOME=str(home), PATH='/usr/bin:/bin')
        self.client = CodexClient(CodexConfig(cwd=str(home), env=env,
            config_overrides=('shell_environment_policy.inherit="none"',)), approval_handler=approval_handler)
        self.closed = False
        self.close_lock = threading.Lock()
        try:
            self.client.start()
            self.identity = self._bounded(self.client.initialize, 15).model_dump(mode='json', by_alias=True)
        except BaseException:
            self.close()
            raise

    def _bounded(self, operation, timeout):
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise ValueError('SDK timeout must be positive and at most 300 seconds')
        expired = threading.Event()
        def abort():
            expired.set()
            self.close()
        timer = threading.Timer(timeout, abort)
        timer.daemon = True
        timer.start()
        try:
            result = operation()
            if expired.is_set():
                raise TimeoutError('SDK operation timed out; reconcile mutations before retry')
            return result
        except Exception:
            if expired.is_set():
                raise TimeoutError('SDK operation timed out; reconcile mutations before retry') from None
            raise
        finally:
            timer.cancel()

    def call(self, method, params, *, timeout=30):
        if method not in OPERATIONS:
            raise PermissionError('SDK operation is not exposed')
        return self._bounded(lambda: self.client.request(method, params, response_model=getattr(models, OPERATIONS[method])), timeout).model_dump(mode='json', by_alias=True)

    def next_event(self):
        event = self.client.next_notification()
        payload = event.payload.model_dump(mode='json', by_alias=True)
        return {'method': event.method, 'params': payload.get('params', payload)}

    def close(self):
        # SDK 0.157.0 close leaves read handles open; close our owned handles only.
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
            process = self.client._proc
        self.client.close()
        if process:
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
