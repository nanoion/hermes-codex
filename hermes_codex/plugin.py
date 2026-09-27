"""Supported Hermes plugin registration; authority is not part of tool input."""
import hashlib
import json
import threading
import uuid
from pathlib import Path

from .service import Service, sanitized
from .controls import CONTROL_FIELDS
from .presentation import error_info, open_login, open_device_login
from .state import Denied, Conflict, encoded


def profile_home():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()).resolve()


_SESSION_FIELDS = ('ID', 'KEY', 'PLATFORM', 'SOURCE', 'USER_ID', 'CHAT_ID', 'CHAT_TYPE',
                   'THREAD_ID', 'SCOPE_ID', 'PROFILE', 'MESSAGE_ID')
_LEGACY_FIELDS = ('ID', 'KEY', 'PLATFORM', 'SOURCE', 'USER_ID', 'CHAT_ID',
                  'THREAD_ID', 'SCOPE_ID', 'PROFILE')
_LOCAL_SURFACES = {'cli', 'tui', 'desktop', 'local'}


def _session_fields():
    from gateway.session_context import get_session_env
    return {key: get_session_env('HERMES_SESSION_' + key, '') for key in _SESSION_FIELDS}


def _route(fields):
    route = fields['KEY']
    if not route and fields['ID'] and (
        fields['PLATFORM'] in _LOCAL_SURFACES
        or not fields['PLATFORM'] and fields['SOURCE'] in _LOCAL_SURFACES
    ):
        route = {'local_session_id': fields['ID']}
    return route


def _legacy_owner(fields):
    if not fields['ID'] or not (fields['SOURCE'] or fields['PLATFORM']):
        return None
    legacy = {key: fields[key] for key in _LEGACY_FIELDS}
    legacy['home'] = str(profile_home())
    return hashlib.sha256(encoded(legacy).encode()).hexdigest()


def _scope_from_fields(fields):
    surface = fields['PLATFORM'] or fields['SOURCE']
    local = surface in _LOCAL_SURFACES
    if local:
        owner = _legacy_owner(fields)
        if owner is None:
            raise Denied('Trusted Hermes session context required')
        return owner, _route(fields)
    if not surface or not fields['USER_ID'] or not (fields['CHAT_ID'] or fields['SCOPE_ID']):
        raise Denied('Trusted user and chat identity required on messaging surfaces')
    identity = {'version': 2, 'home': str(profile_home()), 'PLATFORM': surface,
                **{key: fields[key] for key in ('CHAT_TYPE', 'CHAT_ID', 'THREAD_ID',
                                                'SCOPE_ID', 'USER_ID', 'PROFILE')}}
    return hashlib.sha256(encoded(identity).encode()).hexdigest(), _route(fields)


def current_scope():
    return _scope_from_fields(_session_fields())


def _select_owner(store, canonical, legacy):
    if legacy and legacy != canonical and store.has_owner(legacy):
        store.migrate_owner(legacy, canonical)
    return canonical


def _remember_route(routes, owner, route, captured_cli):
    """Never let a keyless invocation erase a validated async delivery route."""
    if route or owner not in routes:
        routes[owner] = (route, captured_cli)


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

    def scope(svc=None):
        if profile_home() != home:
            raise Denied('Plugin called from a different Hermes profile')
        owner, route = current_scope()
        if svc is not None:
            try:
                legacy = _legacy_owner(_session_fields())
            except ImportError:
                legacy = None
            owner = _select_owner(svc.store, owner, legacy)
        with lock:
            if unloaded:
                raise Denied('Plugin registration has been unloaded')
            _remember_route(routes, owner, route,
                            getattr(getattr(ctx, '_manager', None), '_cli_ref', None))
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

    user_control_operations = ('show', 'native-review', 'plan', 'control', 'followup',
                               'queue', 'input', 'authorize', 'decide')
    user_control_contracts = {
        'show': ({'kind', 'id'}, {'page'}),
        'native-review': ({'task', 'target', 'delivery'}, set()),
        'plan': ({'id', 'action'}, {'revision'}),
        'control': ({'action', 'payload'}, set()),
        'followup': ({'task', 'key', 'text'}, set()),
        'queue': ({'task', 'action'}, {'id'}),
        'input': ({'id', 'action'}, {'question', 'answers'}),
        'authorize': ({'id'}, set()),
        'decide': ({'id', 'decision'}, set()),
    }
    actions = ['help', 'list', 'propose', 'submit', 'status', 'events', 'result', 'result-page', 'cancel', 'review', 'interactions', 'queue', 'control-status', 'inspect', 'present', 'diagnostics', 'user-control']

    def dispatch_user_control(svc, owner, payload):
        if not isinstance(payload, dict) or set(payload) != {'operation', 'arguments'} or not isinstance(payload['arguments'], dict):
            raise ValueError('User control requires operation and arguments')
        operation, arguments = payload['operation'], payload['arguments']

        def fields(required, optional=()):
            if not set(required) <= arguments.keys() or arguments.keys() - set(required) - set(optional):
                raise ValueError(f'Invalid {operation} fields')

        if operation == 'show':
            fields({'kind', 'id'}, {'page'})
            return svc.present(owner, arguments['kind'], arguments['id'], page=arguments.get('page'))
        if operation == 'native-review':
            fields({'task', 'target', 'delivery'})
            return svc.native_review(owner, arguments['task'], arguments['target'], arguments['delivery'])
        if operation == 'plan':
            fields({'id', 'action'}, {'revision'})
            return svc.plan_action(owner, arguments['id'], arguments['action'], arguments.get('revision'))
        if operation == 'control':
            fields({'action', 'payload'})
            return svc.control(owner, arguments['action'], arguments['payload'])
        if operation == 'followup':
            fields({'task', 'key', 'text'})
            return svc.followup(owner, arguments['task'], arguments['key'], arguments['text'])
        if operation == 'queue':
            fields({'task', 'action'}, {'id'})
            return svc.queue_action(owner, arguments['task'], arguments['action'], arguments.get('id'))
        if operation == 'input':
            fields({'id', 'action'}, {'question', 'answers'})
            return svc.answer(owner, arguments['id'], arguments['action'], question=arguments.get('question'), answers=arguments.get('answers'))
        if operation == 'authorize':
            fields({'id'})
            return svc.authorize(owner, arguments['id'])
        if operation == 'decide':
            fields({'id', 'decision'})
            return svc.decide(owner, arguments['id'], arguments['decision'])
        raise ValueError('Unknown user control operation')

    def dispatch(args):
        if not isinstance(args, dict) or set(args) - {'action', 'id', 'payload', 'cursor'}:
            raise ValueError('Unexpected tool arguments; ownership is runtime-derived')
        action = args.get('action')
        if action not in actions:
            raise ValueError('Unknown action')
        owner = scope()
        if action == 'help':
            return {'actions': actions, 'model_user_control_operations': user_control_operations,
                    'direct_user': ['/codex approve [proposal-id]', '/codex status [task-id]', '/codex continue [task-id] <instruction>', '/codex result [task-id] [page-token]', '/codex cancel [task-id]', '/codex decide [request-id] <decision>', '/codex-user <advanced-action>'],
                    'user_control_envelope': {'action': 'user-control', 'payload': {'operation': '<operation>', 'arguments': '<operation-specific object>'}},
                    'user_controls': {k: {'required': sorted(v[0]), 'optional': sorted(v[1])} for k, v in user_control_contracts.items()},
                    'control_operations': {k: {'required': sorted(v[0]), 'optional': sorted(v[1])} for k, v in CONTROL_FIELDS.items()},
                    'examples': {
                        'authorize': {'action': 'user-control', 'payload': {'operation': 'authorize', 'arguments': {'id': '<proposal-id>'}}},
                        'followup': {'action': 'user-control', 'payload': {'operation': 'followup', 'arguments': {'task': '<task-id>', 'key': '<idempotency-key>', 'text': '<instruction>'}}},
                        'input-edit': {'action': 'user-control', 'payload': {'operation': 'input', 'arguments': {'id': '<request-id>', 'action': 'edit', 'question': '<question-id>', 'answers': ['<answer>']}}},
                        'input-submit': {'action': 'user-control', 'payload': {'operation': 'input', 'arguments': {'id': '<request-id>', 'action': 'submit'}}}},
                    'sequencing': ['authorize a proposal before submit', 'edit every structured-input question before submit', 'poll control-status for asynchronous control operations'],
                    'readiness': 'User controls are model-accessible when requested in conversation; downstream scope and policy checks still apply.'}
        if action == 'diagnostics':
            from .runtime import capabilities
            return capabilities()
        svc = service()
        owner = scope(svc)
        if action == 'user-control':
            return dispatch_user_control(svc, owner, args.get('payload'))
        if action == 'list':
            tasks = [{key: item.get(key) for key in ('id', 'state', 'review_state', 'workspace', 'created')}
                     for item in svc.store.tasks(owner)[:20]]
            proposals = [{'id': item['id'], 'workspace': item.get('brief', {}).get('workspace'),
                          'sandbox': item.get('brief', {}).get('sandbox')}
                         for item in svc.store.list(owner, 'proposal') if not item.get('authorized')]
            interactions = [{key: item.get(key) for key in ('id', 'task', 'method', 'state')}
                            for item in svc.store.list(owner, 'interaction') if item.get('state') == 'pending']
            return {'tasks': tasks, 'pending_proposals': proposals, 'pending_interactions': interactions}
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
        return host.settings(scope(host)).get('locale', 'en') if host else 'en'

    def tool(args, **kwargs):
        return envelope(lambda: dispatch(args), locale)

    def user_command(raw_args):
        def run():
            svc = service()
            owner = scope(svc)
            prefixes = {
                'show ': 'show', 'native-review ': 'native-review', 'plan ': 'plan',
                'control ': 'control', 'followup ': 'followup', 'queue ': 'queue', 'input ': 'input',
            }
            for prefix, operation in prefixes.items():
                if raw_args.startswith(prefix):
                    return dispatch_user_control(svc, owner, {
                        'operation': operation,
                        'arguments': json.loads(raw_args[len(prefix):]),
                    })
            words = raw_args.split()
            if len(words) == 2 and words[0] == 'authorize':
                return dispatch_user_control(svc, owner, {
                    'operation': 'authorize', 'arguments': {'id': words[1]},
                })
            if len(words) == 3 and words[0] == 'decide':
                return dispatch_user_control(svc, owner, {
                    'operation': 'decide', 'arguments': {'id': words[1], 'decision': words[2]},
                })
            raise ValueError('Use authorize <proposal-id> or decide <request-id> <accept|acceptForSession|decline|cancel>')
        return envelope(run, locale)

    def easy_command(raw_args):
        """Human-facing direct-user shortcuts; IDs are optional only when selection is unambiguous."""
        usage = ('Use `/codex status`, `/codex approve`, `/codex continue <instruction>`, '
                 '`/codex result`, `/codex cancel`, or `/codex list`.')

        def render(title, value):
            value = sanitized(value)
            if isinstance(value, dict) and isinstance(value.get('text'), str):
                suffix = f"\nPage cursor: {value['next']}" if value.get('next') is not None else ''
                return f"**{title}**\n{value['text']}{suffix}"
            fields = []
            if isinstance(value, list):
                for event in value[:20]:
                    if not isinstance(event, dict):
                        continue
                    detail = event.get('payload')
                    detail = encoded(detail)[:500] if detail else ''
                    fields.append(f"- `{event.get('cursor', '?')}` {event.get('type', 'event')}: {detail}")
            elif isinstance(value, dict):
                for key in ('id', 'task', 'state', 'review_state', 'summary', 'next_cursor'):
                    if value.get(key) is not None:
                        fields.append(f"- {key.replace('_', ' ')}: `{value[key]}`")
                if isinstance(value.get('events'), list):
                    fields.append(f"- events: {len(value['events'])}")
            return f"**{title}**\n" + ('\n'.join(fields) if fields else 'Done.')

        def run():
            svc = service()
            owner = scope(svc)
            raw = raw_args.strip()
            words = raw.split()
            command = words[0].lower() if words else ''
            tasks = svc.store.tasks(owner)

            def looks_like_task_id(value):
                try:
                    return str(uuid.UUID(value)) == value.lower()
                except (ValueError, AttributeError):
                    return False

            def task(explicit=None, candidates=None):
                candidates = tasks if candidates is None else candidates
                if explicit:
                    selected = next((item for item in tasks if item['id'] == explicit), None)
                    if selected is None:
                        raise Denied('Task unavailable in this scope')
                    return selected
                if not candidates:
                    raise Denied('No eligible Codex task exists in this chat')
                if len(candidates) > 1:
                    return None
                return candidates[0]

            def choose(command_name, candidates):
                return f"**Choose a task**\n" + '\n'.join(
                    f"- `/codex {command_name} {item['id']}` — {item['state']}" for item in candidates)

            if command in {'', 'list'}:
                pending = [p for p in svc.store.list(owner, 'proposal') if not p.get('authorized')]
                lines = ['**Codex**']
                if tasks:
                    current = tasks[0]
                    lines.append(f"- current task: `{current['id']}` — {current['state']}")
                else:
                    lines.append('- no task yet — tell Hermes what you want Codex to do')
                if pending:
                    lines.append(f"- pending proposal: {len(pending)} — `/codex approve`")
                lines.append('- commands: `status`, `approve`, `continue`, `result`, `cancel`, `list`')
                return '\n'.join(lines)
            if command == 'help':
                return usage
            if command == 'approve':
                proposal_id = words[1] if len(words) == 2 else None
                if len(words) > 2:
                    raise ValueError('Use `/codex approve [proposal-id]`')
                proposals = svc.store.list(owner, 'proposal')
                candidates = [p for p in proposals if not p.get('authorized')]
                if proposal_id:
                    selected = next((p for p in proposals if p['id'] == proposal_id), None)
                    if selected is None:
                        raise Denied('Proposal unavailable in this scope')
                elif len(candidates) == 1:
                    selected = candidates[0]
                elif not candidates:
                    raise Denied('No proposal is waiting for approval')
                else:
                    return '**Choose a proposal**\n' + '\n'.join(f"- `/codex approve {p['id']}`" for p in candidates)
                svc.authorize(owner, selected['id'])
                return render('Codex started', svc.submit(owner, selected['id']))
            if command == 'status':
                if len(words) > 2:
                    raise ValueError('Use `/codex status [task-id]`')
                selected = task(words[1] if len(words) == 2 else None) if len(words) == 2 else (tasks[0] if tasks else task())
                return render('Codex status', svc.status(owner, selected['id']))
            if command == 'continue':
                rest = raw[len(words[0]):].strip()
                explicit = words[1] if len(words) > 1 and looks_like_task_id(words[1]) else None
                candidates = [item for item in tasks if item['state'] not in {'unknown', 'cancelling', 'cancelled', 'failed'}]
                if explicit:
                    selected = task(explicit)
                    if selected not in candidates:
                        raise Conflict('This task must be reconciled or recovered before continuing')
                    rest = raw.split(None, 2)[2] if len(words) > 2 else ''
                else:
                    selected = task(candidates=candidates)
                    if selected is None:
                        return choose('continue', candidates)
                if not rest:
                    raise ValueError('Use `/codex continue [task-id] <instruction>`')
                message_id = _session_fields()['MESSAGE_ID']
                key = hashlib.sha256(encoded([owner, selected['id'], message_id or rest]).encode()).hexdigest()
                return render('Codex follow-up', svc.followup(owner, selected['id'], key, rest))
            if command in {'result', 'events'}:
                args = words[1:]
                explicit = None
                if args and any(item['id'] == args[0] for item in tasks):
                    explicit = args.pop(0)
                elif command == 'events' and args and looks_like_task_id(args[0]):
                    explicit = args.pop(0)
                selected = task(explicit) if explicit else (tasks[0] if tasks else task())
                if command == 'result':
                    if len(args) > 1:
                        raise ValueError('Use `/codex result [task-id] [page-token]`')
                    value = svc.present(owner, 'result', selected['id'], page=args[0] if args else None)
                else:
                    if len(args) > 1:
                        raise ValueError('Use `/codex events [task-id] [cursor]`')
                    cursor = int(args[0]) if args else 0
                    value = svc.events(owner, selected['id'], cursor)
                return render('Codex result' if command == 'result' else 'Codex events', value)
            if command == 'cancel':
                if len(words) > 2:
                    raise ValueError('Use `/codex cancel [task-id]`')
                candidates = [item for item in tasks if item['state'] in {'pending', 'starting', 'running', 'cancelling', 'unknown'}]
                selected = task(words[1], candidates) if len(words) == 2 else task(candidates=candidates)
                if selected is None:
                    return choose('cancel', candidates)
                return render('Codex cancellation', svc.cancel(owner, selected['id'], 'direct user /codex cancel'))
            if command == 'decide':
                decisions = {'accept', 'acceptForSession', 'decline', 'cancel'}
                interactions = [i for i in svc.store.list(owner, 'interaction') if i.get('state') == 'pending']
                if len(words) == 2 and words[1] in decisions and len(interactions) == 1:
                    request, decision = interactions[0]['id'], words[1]
                elif len(words) == 3 and words[2] in decisions:
                    request, decision = words[1], words[2]
                else:
                    raise ValueError('Use `/codex decide [request-id] <accept|acceptForSession|decline|cancel>`')
                return render('Codex decision', svc.decide(owner, request, decision))
            raise ValueError(usage)

        try:
            return run()
        except Exception as exc:
            try:
                language = locale()
            except Exception:
                language = 'en'
            info = error_info(exc, language)
            return f"❌ {info['message']}\n{info.get('action', usage)}"

    guidance = (
        'Use the `codex` tool automatically when the user asks to inspect, start, monitor, continue, '
        'review, authorize, answer, open, focus, or otherwise control Codex. Keep internal action names, '
        'IDs when unambiguous, JSON, and `/codex-user` commands out of the reply. Use `user-control` '
        'to execute every formerly direct-user operation—including control/open, followup, authorize, '
        'decide, plan, queue, input, and native review—when the user’s conversation clearly requests '
        'or confirms it. For asynchronous controls, poll `control-status` with a bounded wait; for worker '
        'tasks, use `status`, `events`, and `result-page`, then summarize the evidence. The exact envelope is '
        '`{"action":"user-control","payload":{"operation":"<operation>","arguments":{...}}}`. Call `help` '
        'for each operation’s required and optional fields. Authorize before submit; for structured input, edit '
        'every question before submit. Do not merely print a slash command for the user to copy. Prefer the '
        'current scoped task/thread when unambiguous and ask a short choice only when multiple candidates remain.'
    )
    ctx.register_system_prompt_section('codex.workflow', guidance, position='after_memory')
    ctx.register_tool(name='codex', toolset='codex', schema={
        'name': 'codex', 'description': 'Use Codex conversationally: inspect threads, propose scoped work, monitor tasks, retrieve results, continue work, and invoke user controls requested in conversation through `user-control`. Ownership remains runtime-derived.',
        'parameters': {'type': 'object', 'additionalProperties': False, 'required': ['action'],
                       'properties': {'action': {'type': 'string', 'enum': actions}, 'id': {'type': 'string'},
                                      'payload': {'type': 'object', 'description': 'For action=user-control, use exactly {operation, arguments}. operation is show, native-review, plan, control, followup, queue, input, authorize, or decide; arguments uses the required/optional fields returned by help. Example authorization: {"operation":"authorize","arguments":{"id":"<proposal-id>"}}. Authorize before submit. Structured input requires one edit per question followed by submit.'},
                                      'cursor': {'type': 'integer', 'minimum': 0}}}}, handler=tool)
    ctx.register_command('codex', easy_command, description='Simple Codex status, approval and task controls', args_hint='[status|approve|continue|result|cancel|list]')
    ctx.register_command('codex-user', user_command, description='Advanced compatibility controls for direct-user authorization', args_hint='<action> <id> [decision]')
    ctx.on_unload(close)
