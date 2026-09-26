"""Deterministic unload/notification lock inversion probe, no native runtime."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import traceback
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermes_codex import plugin
from hermes_codex.service import Service
from test_plugin import Context
from test_worker import Transport, brief


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'project').mkdir()
        ctx = Context(root)
        ctx._manager = SimpleNamespace(_cli_ref=SimpleNamespace(session_id='local-one'))
        delivered = []
        ctx.inject_message = lambda *a, **kw: delivered.append((a, kw)) or True
        at_notify, release_notify, at_close = (threading.Event() for _ in range(3))
        hosts = []

        def factory(*args, **kwargs):
            notify = kwargs['notify']
            def blocked_notify(*args):
                at_notify.set()
                if not release_notify.wait(10):
                    raise TimeoutError('Probe did not release notification')
                return notify(*args)
            kwargs['notify'] = blocked_notify
            host = Service(*args, **kwargs, transport_factory=Transport)
            original_close = host.close
            def closing():
                at_close.set()
                original_close()
            host.close = closing
            hosts.append(host)
            return host

        with patch.object(plugin, 'profile_home', return_value=root), patch.object(plugin, 'current_scope', return_value=('owner', {'local_session_id': 'local-one'})), patch.object(plugin, 'Service', side_effect=factory), patch('hermes_codex.state.writer_directory', return_value=root / 'writers'):
            plugin.register(ctx)
            tool = ctx.tools['codex']['handler']
            proposal = json.loads(tool({'action': 'propose', 'payload': brief(root / 'project')}))['data']
            host = hosts[0]
            # Queue the terminal event before starting, avoiding sleep/poll races.
            original_factory = host.factory
            def completing(*args, **kwargs):
                client = original_factory(*args, **kwargs)
                call = client.call
                def invoke(method, params):
                    response = call(method, params)
                    if method == 'turn/start':
                        client.events.put({'method': 'turn/completed', 'params': {'threadId': params['threadId'], 'turn': response['turn'] | {'status': 'completed', 'items': []}}})
                    return response
                client.call = invoke
                return client
            host.factory = completing
            host.authorize('owner', proposal['id'])
            task = host.submit('owner', proposal['id'])
            assert at_notify.wait(5), 'Worker never reached notification'
            unload = threading.Thread(target=ctx.cleanups[0], daemon=True)
            unload.start()
            assert at_close.wait(5), 'Unload never entered service close'
            release_notify.set()
            unload.join(3)
            worker = host.threads[task['id']]
            print('plugin unload alive:', unload.is_alive(), 'worker alive:', worker.is_alive(), flush=True)
            if unload.is_alive() or worker.is_alive():
                for thread in (unload, worker):
                    frame = sys._current_frames().get(thread.ident)
                    if frame:
                        traceback.print_stack(frame)
                return 1
            assert not delivered, 'Unloaded registration still delivered a notification'
            assert not host.notify('owner', 'stale route'), 'Stale route survived unload'
            assert not json.loads(tool({'action': 'help'}))['success'], 'Unloaded tool revived routing'
            assert not json.loads(tool({'action': 'propose', 'payload': brief(root / 'project')}))['success'], 'Unloaded tool revived service'
            ctx.cleanups[0]()  # idempotent unload
            with patch('hermes_codex.state.writer_directory', return_value=root / 'writers'):
                reopened = Service(root / 'codex-worker', [str(root / 'project')], transport_factory=Transport)
                assert reopened.store.task('owner', task['id'])['state'] == 'completed'
                reopened.close()
            return 0


if __name__ == '__main__':
    sys.exit(main())
