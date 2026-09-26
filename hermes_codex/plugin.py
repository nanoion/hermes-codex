"""Supported Hermes plugin registration; authority is not part of tool input."""
import hashlib
import json
import threading
from pathlib import Path

from .service import Service, sanitized
from .controls import CONTROL_FIELDS
from .presentation import error_info, open_login, open_device_login
from .state import Denied, Conflict, encoded


def profile_home():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()).resolve()


def current_scope():
    from gateway.session_context import get_session_env
    fields = {key: get_session_env('HERMES_SESSION_' + key, '')
              for key in ('ID', 'KEY', 'PLATFORM', 'SOURCE', 'USER_ID', 'CHAT_ID', 'THREAD_ID', 'SCOPE_ID', 'PROFILE')}
    if not fields['ID'] or not (fields['SOURCE'] or fields['PLATFORM']):
        raise Denied('Trusted Hermes session context required')
    if fields['PLATFORM'] not in ('', 'cli', 'tui', 'desktop', 'local') and not fields['USER_ID']:
        raise Denied('Trusted user identity required on messaging surfaces')
    fields['home'] = str(profile_home())
    route = fields['KEY']
    local = {'cli', 'tui', 'desktop', 'local'}
    if not route and (fields['PLATFORM'] in local or not fields['PLATFORM'] and fields['SOURCE'] in local):
        route = {'local_session_id': fields['ID']}
    return hashlib.sha256(encoded(fields).encode()).hexdigest(), route


def envelope(fn, locale=lambda: 'en'):
    try:
        return encoded({'success': True, 'code': 'ok', 'data': sanitized(fn())})
    except Exception as exc:
        try:
            language = locale()
        except Exception:
            language = 'en'
        return encoded({'success': False, **error_info(exc, language)})


def register(ctx):
    home = profile_home()
    roots = ctx.get_config('allowed_roots', [])
    if not isinstance(roots, list) or any(not isinstance(x, str) or not Path(x).is_absolute() for x in roots):
        raise ValueError('allowed_roots must contain absolute directory paths')
    lock = threading.RLock()
    host = None
    unloaded = False
    routes = {}

    def notify(owner, message):
        with lock:
            entry = routes.get(owner)
            if unloaded or entry is None or profile_home() != home:
                return False
            route, captured_cli = entry
            cli = getattr(getattr(ctx, '_manager', None), '_cli_ref', None)
            if isinstance(route, dict):
                # Installed Hermes routes no-key injection to this CLI instance.
                # Pin both instance and session; never deliver to a switched tab.
                if cli is None or cli is not captured_cli or getattr(cli, 'session_id', None) != route['local_session_id']:
                    return False
                return bool(ctx.inject_message(message, role='user'))
            # Hermes gives a CLI precedence over session_key: do not misroute a
            # gateway completion if the context has subsequently attached a CLI.
            return bool(route and cli is None and ctx.inject_message(message, role='user', session_key=route))

    def service():
        nonlocal host
        with lock:
            if unloaded:
                raise Denied('Plugin registration has been unloaded')
            if host is None:
                login_command = ctx.get_config('login_command', None)
                device_command = ctx.get_config('device_login_command', None)
                host = Service(home / 'codex-worker', roots, notify=notify, desktop_command=ctx.get_config('desktop_command', None), model_variants=ctx.get_config('model_variants', {}), provider_profiles=ctx.get_config('provider_profiles', {}),
                               login_presenter=(lambda url: open_login(url, login_command)) if login_command else None,
                               device_login_presenter=(lambda data: open_device_login(data, device_command)) if device_command else None,
                               allow_full_access=ctx.get_config('allow_full_access', False),
                               attachment_roots=ctx.get_config('attachment_roots', []),
                               host_account_homes=ctx.get_config('host_account_homes', {}))
            return host

    def scope():
        if profile_home() != home:
            raise Denied('Plugin called from a different Hermes profile')
        owner, route = current_scope()
        with lock:
            if unloaded:
                raise Denied('Plugin registration has been unloaded')
            routes[owner] = (route, getattr(getattr(ctx, '_manager', None), '_cli_ref', None))
        return owner

    def close():
        nonlocal host, unloaded
        with lock:
            unloaded = True
            closing, host = host, None
            routes.clear()
        # Completing workers acquire this registration lock in notify().
        # Disable delivery/new hosts atomically, then join without the lock.
        if closing:
            closing.close()

    actions = ['help', 'propose', 'submit', 'status', 'events', 'result', 'result-page', 'cancel', 'review', 'interactions', 'queue', 'control-status', 'inspect', 'present', 'diagnostics']

    def dispatch(args):
        if not isinstance(args, dict) or set(args) - {'action', 'id', 'payload', 'cursor'}:
            raise ValueError('Unexpected tool arguments; ownership is runtime-derived')
        action = args.get('action')
        if action not in actions:
            raise ValueError('Unknown action; authorization and approvals require /codex-user')
        owner = scope()
        if action == 'help':
            return {'actions': actions, 'direct_user': ['/codex-user authorize <proposal-id>', '/codex-user decide <request-id> <decision>', '/codex-user control {action,payload}', '/codex-user plan {id,action,revision?}', '/codex-user native-review {task,target,delivery}', '/codex-user followup {task,key,text}', '/codex-user input {id,action,question?,answers?}', '/codex-user queue {task,action,id?}'],
                    'user_controls': {k: {'required': sorted(v[0]), 'optional': sorted(v[1])} for k, v in CONTROL_FIELDS.items()},
                    'readiness': 'Full parity is not delivered; authenticated worker setup requires explicit authorization.'}
        if action == 'diagnostics':
            from .runtime import capabilities
            return capabilities()
        svc = service()
        if action == 'present':
            payload = args.get('payload', {})
            if not isinstance(payload, dict) or not {'kind'} <= payload.keys() or payload.keys() - {'kind', 'page'}:
                raise ValueError('Presentation requires kind and optional page')
            return svc.present(owner, payload['kind'], args['id'], page=payload.get('page'))
        if action == 'inspect':
            payload = args['payload']
            if not isinstance(payload, dict) or set(payload) != {'operation', 'arguments'} or payload['operation'] not in {'threads', 'models', 'settings', 'snapshot', 'providers', 'recovery'}:
                raise Denied('Only read-only inspections are model-facing')
            return svc.control(owner, payload['operation'], payload['arguments'])
        if action == 'control-status':
            return svc.control_status(owner, args['id'])
        if action == 'propose':
            return svc.propose(owner, args['payload'])
        if action == 'result-page':
            payload = args.get('payload', {})
            if not isinstance(payload, dict) or payload.keys() - {'attempt'}:
                raise ValueError('Result page accepts only an attempt selector')
            return svc.result_page(owner, args['id'], attempt=payload.get('attempt'), cursor=args.get('cursor', 0))
        if action == 'events':
            return svc.events(owner, args['id'], args.get('cursor', 0))
        if action == 'interactions':
            return svc.store.list(owner, 'interaction')
        if action == 'queue':
            return svc.queue_items(owner, args['id'])
        if action == 'review':
            payload = args['payload']
            svc.store.review(owner, args['id'], payload['decision'], payload['evidence'])
            return svc.status(owner, args['id'])
        return getattr(svc, action)(owner, args['id'])

    def locale():
        return host.settings(scope()).get('locale', 'en') if host else 'en'

    def tool(args, **kwargs):
        return envelope(lambda: dispatch(args), locale)

    def user_command(raw_args):
        def run():
            owner = scope()
            if raw_args.startswith('show '):
                payload = json.loads(raw_args[5:])
                if not isinstance(payload, dict) or not {'kind', 'id'} <= payload.keys() or payload.keys() - {'kind', 'id', 'page'}:
                    raise ValueError('Show requires kind/id and optional page')
                return service().present(owner, payload['kind'], payload['id'], page=payload.get('page'))
            if raw_args.startswith('native-review '):
                payload = json.loads(raw_args[14:])
                if not isinstance(payload, dict) or set(payload) != {'task', 'target', 'delivery'}:
                    raise ValueError('Native review requires task, target, delivery')
                return service().native_review(owner, payload['task'], payload['target'], payload['delivery'])
            if raw_args.startswith('plan '):
                payload = json.loads(raw_args[5:])
                if not isinstance(payload, dict) or not {'id', 'action'} <= payload.keys() or payload.keys() - {'id', 'action', 'revision'}:
                    raise ValueError('Plan requires id, action, optional revision')
                return service().plan_action(owner, payload['id'], payload['action'], payload.get('revision'))
            if raw_args.startswith('control '):
                payload = json.loads(raw_args[8:])
                if not isinstance(payload, dict) or set(payload) != {'action', 'payload'}:
                    raise ValueError('Control requires action and payload')
                return service().control(owner, payload['action'], payload['payload'])
            if raw_args.startswith('followup '):
                payload = json.loads(raw_args[9:])
                if not isinstance(payload, dict) or set(payload) != {'task', 'key', 'text'}:
                    raise ValueError('Follow-up requires task, key, text')
                return service().followup(owner, payload['task'], payload['key'], payload['text'])
            if raw_args.startswith('queue '):
                payload = json.loads(raw_args[6:])
                if not isinstance(payload, dict) or set(payload) - {'task', 'action', 'id'}:
                    raise ValueError('Queue requires task, action, optional id')
                return service().queue_action(owner, payload['task'], payload['action'], payload.get('id'))
            if raw_args.startswith('input '):
                payload = json.loads(raw_args[6:])
                if not isinstance(payload, dict) or set(payload) - {'id', 'action', 'question', 'answers'}:
                    raise ValueError('Invalid structured input arguments')
                return service().answer(owner, payload['id'], payload['action'], question=payload.get('question'), answers=payload.get('answers'))
            words = raw_args.split()
            if len(words) == 2 and words[0] == 'authorize':
                return service().authorize(owner, words[1])
            if len(words) == 3 and words[0] == 'decide':
                return service().decide(owner, words[1], words[2])
            raise ValueError('Use authorize <proposal-id> or decide <request-id> <accept|acceptForSession|decline|cancel>')
        return envelope(run, locale)

    ctx.register_tool(name='codex', toolset='codex', schema={
        'name': 'codex', 'description': 'Manage scoped Codex worker assignments. Worker completion requires separate manager review. Cannot grant user authorization.',
        'parameters': {'type': 'object', 'additionalProperties': False, 'required': ['action'],
                       'properties': {'action': {'type': 'string', 'enum': actions}, 'id': {'type': 'string'},
                                      'payload': {'type': 'object'}, 'cursor': {'type': 'integer', 'minimum': 0}}}}, handler=tool)
    ctx.register_command('codex-user', user_command, description='Direct-user authorization and live approval decisions', args_hint='<action> <id> [decision]')
    ctx.on_unload(close)
