"""Trusted Hermes boundary tested without installing into any live profile."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class Context:
    def __init__(self, root):
        self.root = root
        self.tools = {}
        self.commands = {}
        self.sections = {}
        self.cleanups = []

    def get_config(self, key, default=None):
        return {'allowed_roots': [str(self.root / 'project')]}.get(key, default)

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def register_system_prompt_section(self, id, content, **kwargs):
        self.sections[id] = content

    def on_unload(self, handler):
        self.cleanups.append(handler)

    def inject_message(self, *args, **kwargs):
        return False


class PluginTests(unittest.TestCase):
    def test_tools_never_grant_user_authority_or_accept_forged_scope(self):
        import hermes_codex
        self.assertTrue(hasattr(hermes_codex, 'register'), 'Supported plugin registration missing')
        from hermes_codex import plugin
        from test_worker import brief, Transport
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'project').mkdir()
            ctx = Context(root)
            registry = patch('hermes_codex.state.writer_directory', return_value=root / 'writers')
            registry.start()
            self.addCleanup(registry.stop)
            with patch.object(plugin, 'profile_home', return_value=root), patch.object(plugin, 'current_scope', return_value=('owner', 'session')), patch.object(plugin, 'Service', side_effect=lambda *a, **kw: hermes_codex.Service(*a, **kw, transport_factory=Transport)):
                hermes_codex.register(ctx)
                try:
                    tool = ctx.tools['codex']['handler']
                    self.assertNotIn('codex_worker', ctx.tools)
                    self.assertNotIn('approve', ctx.tools)
                    help_result = json.loads(tool({'action': 'help'}))
                    self.assertIn('result-page', help_result['data']['actions'])
                    self.assertIn('thread-confirm', help_result['data'].get('user_controls', {}), 'Control discovery missing')
                    self.assertFalse(json.loads(tool({'action': 'inspect', 'payload': {'operation': 'settings-set', 'arguments': {}}}))['success'])
                    self.assertFalse(json.loads(tool({'action': 'control', 'payload': {'action': 'settings-set'}}))['success'])
                    control = json.loads(ctx.commands['codex-user']('control ' + json.dumps({'action': 'threads', 'payload': {}})))
                    self.assertTrue(control['success'], control)
                    import time
                    for _ in range(100):
                        state = json.loads(tool({'action': 'control-status', 'id': control['data']['id']}))
                        if state.get('data', {}).get('state') == 'completed':
                            break
                        time.sleep(.01)
                    self.assertEqual(state['data']['result']['data'], [])
                    bad = json.loads(ctx.commands['codex-user']('control ' + json.dumps({'action': 'threads', 'payload': {'owner': 'forged'}})))
                    self.assertFalse(bad['success'])
                    result = json.loads(tool({'action': 'propose', 'payload': brief(root / 'project')}))
                    self.assertTrue(result['success'], result)
                    proposal = result['data']
                    self.assertFalse(json.loads(tool({'action': 'authorize', 'id': proposal['id']}))['success'])
                    self.assertFalse(json.loads(tool({'action': 'submit', 'id': proposal['id'], 'owner': 'forged'}))['success'])
                    self.assertFalse(json.loads(tool({'action': 'submit', 'id': proposal['id']}))['success'])
                    authorized = json.loads(ctx.commands['codex-user']('authorize ' + proposal['id']))
                    self.assertTrue(authorized['success'], authorized)
                    dispatched = json.loads(tool({'action': 'submit', 'id': proposal['id']}))
                    self.assertTrue(dispatched['success'], dispatched)
                    task_id = dispatched['data']['id']
                    queued = json.loads(ctx.commands['codex-user']('followup ' + json.dumps({'task': task_id, 'key': 'next', 'text': 'Inspect more'})))
                    self.assertTrue(queued['success'], queued)
                    self.assertTrue(json.loads(tool({'action': 'queue', 'id': task_id}))['success'])
                    with patch.object(plugin, 'current_scope', return_value=('other-owner', 'other-session')):
                        denied = json.loads(tool({'action': 'status', 'id': task_id}))
                        self.assertFalse(denied['success'])
                        self.assertEqual(denied['code'], 'authorization')
                finally:
                    for close in ctx.cleanups:
                        close()
