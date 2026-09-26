"""Run with the installed Hermes Python; temporary profile only, no model calls."""
import os
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
# Load only the project's pinned SDK/dependencies, never a credential directory.
sys.path.append(str(PROJECT / '.venv/lib/python3.11/site-packages'))

with tempfile.TemporaryDirectory(prefix='hermes-codex-registration-') as directory:
    os.environ['HERMES_HOME'] = directory
    os.environ['HERMES_PROFILE'] = 'isolated-test'
    from hermes_cli.plugins import PluginManager, PluginContext, PluginManifest
    from gateway.session_context import set_session_vars, clear_session_vars
    from hermes_codex import register
    from tools.registry import registry
    manager = PluginManager(scope_key=directory)
    ctx = PluginContext(PluginManifest(name='hermes-codex', path=str(PROJECT)), manager)
    # Disposable workspace + offline transport: exercise the real Hermes CLI
    # injection API without installing a profile plugin or launching a model.
    from unittest.mock import patch
    from types import SimpleNamespace
    import queue
    import time
    sys.path.insert(0, str(PROJECT / 'tests'))
    from test_worker import Transport, brief
    from hermes_codex.service import Service
    import hermes_codex.plugin as plugin
    cli = SimpleNamespace(session_id='registration-test', _agent_running=False,
                          _pending_input=queue.Queue(), _interrupt_queue=queue.Queue())
    manager._cli_ref = cli
    original_config = ctx.get_config
    ctx.get_config = lambda key, default=None: [directory] if key == 'allowed_roots' else original_config(key, default)
    register(ctx)
    assert 'codex-user' in manager._plugin_commands
    tokens = set_session_vars(source='cli', session_id='registration-test', profile='isolated-test')
    try:
        import json
        result = json.loads(ctx.dispatch_tool('codex', {'action': 'help'}))
        assert result['success'], result
        print('Actual PluginContext registration and scoped tool dispatch: PASS')
        print('Registered direct-user command:', 'codex-user')
        hosts = []
        def factory(*a, **kw):
            hosts.append(Service(*a, **kw, transport_factory=Transport))
            return hosts[-1]
        with patch.object(plugin, 'Service', side_effect=factory), patch('hermes_codex.state.writer_directory', return_value=Path(directory) / 'writers'):
            proposal = json.loads(ctx.dispatch_tool('codex', {'action': 'propose', 'payload': brief(directory)}))
            assert proposal['success'], proposal
            service = hosts[0]
            try:
                owner, _ = plugin.current_scope()
                service.authorize(owner, proposal['data']['id'])
                task = service.submit(owner, proposal['data']['id'])
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and service.status(owner, task['id'])['state'] != 'running':
                    time.sleep(.01)
                attempt = service.status(owner, task['id'])['attempts'][-1]
                service.clients[task['id']].events.put({'method': 'turn/completed', 'params': {
                    'threadId': attempt['thread'], 'turn': {'id': attempt['turn'], 'status': 'completed', 'items': []}}})
                service.threads[task['id']].join(3)
                message = cli._pending_input.get_nowait()
                assert task['id'] in message and 'completed' in message
                notifications = [e for e in service.events(owner, task['id']) if e['type'] == 'notification']
                assert notifications[-1]['payload']['accepted_for_delivery'] is True
                assert notifications[-1]['payload']['delivered'] is None
                cli.session_id = 'switched-session'
                assert service.notify(owner, 'must not cross session') is False
                assert cli._pending_input.empty()
                print('Actual PluginContext no-key CLI completion delivery and stale-session denial: PASS')
            finally:
                service.close()
        print('Offline worker transport only; no native SDK worker or persistent profile modified.')
    finally:
        clear_session_vars(tokens)
