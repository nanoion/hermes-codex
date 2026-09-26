"""SDK-owned isolated OAuth accounts. Never copy, parse, or return credentials."""
import hashlib
import time

from .state import Conflict, Denied, encoded


def public(record):
    return {k: record.get(k) for k in ('id', 'name', 'provider', 'state', 'identity', 'expires')}


def account_home(service, owner, account):
    record = service.store.get(owner, 'account', account)
    if record.get('host_alias'):
        from pathlib import Path
        from .policy import workspace_path
        path = service.host_account_homes.get(record['host_alias'])
        if path is None:
            raise Denied('Named host account was removed from operator configuration')
        canonical = workspace_path(path, [path])
        if canonical != record.get('host_home'):
            raise Denied('Named host account path changed; use a new alias, never retarget saved tasks')
        return Path(canonical)
    return service.home / 'accounts' / hashlib.sha256(owner.encode()).hexdigest() / record['id']


def idle(service, owner):
    with service.store.lock:
        if service.store.db.execute("SELECT 1 FROM tasks WHERE owner=? AND state IN ('pending','starting','running','cancelling','unknown') LIMIT 1", (owner,)).fetchone():
            raise Conflict('Account changes are blocked by active or uncertain work')
        for kind, states in {'interaction': {'pending', 'decided'}, 'plan': {'pending'}, 'queue': {'queued', 'uncertain_steer'}, 'attachment': {'staged'}, 'goal': {'active', 'unknown', 'applying'}}.items():
            if any(v.get('state') in states for v in service.store.list(owner, kind)):
                raise Conflict('Resolve pending ' + kind + ' before changing accounts')


def control(service, owner, job, p):
    from .controls import text
    operation = p['operation']
    provider = service.settings(owner)['provider']
    if operation == 'list':
        return {'data': [public(a) | {'selected': a['id'] == service.selected_account(owner)} for a in service.store.list(owner, 'account') if a['provider'] == provider], 'source': 'saved isolated account metadata; status may be stale'}
    if operation not in {'login', 'continue', 'status', 'refresh', 'cancel', 'switch', 'sync-host'}:
        raise ValueError('Account operation must be login, continue, status, refresh, cancel, switch or list')
    name = text(p.get('name'), 'Account name', 100)
    key = hashlib.sha256(encoded([provider, name]).encode()).hexdigest()
    try:
        record = service.store.get(owner, 'account', key)
    except Denied:
        if operation != 'login':
            if operation != 'sync-host' or name not in service.host_account_homes:
                raise
        record = {'id': key, 'name': name, 'provider': provider, 'state': 'new', 'identity': None}
    if operation in {'login', 'switch', 'refresh', 'sync-host'}:
        idle(service, owner)
    if operation == 'sync-host':
        if name not in service.host_account_homes or record.get('state') in {'pending', 'starting'}:
            raise Denied('Only an operator-configured named host home can be synchronized while idle')
        from .policy import workspace_path
        path = workspace_path(service.host_account_homes[name], [service.host_account_homes[name]])
        if record.get('host_home', path) != path:
            raise Denied('Named host account path changed; use a new alias')
        record.update(host_alias=name, host_home=path)
        service.store.put(owner, 'account', key, record)
    if record.get('host_alias') and operation == 'login':
        raise Denied('Host-linked accounts are read/switch only; log in through a dedicated account instead')
    method = p.get('method', 'browser')
    if method not in {'browser', 'device'} or 'method' in p and operation != 'login':
        raise ValueError('Login method must be browser or device, on login only')
    presenter = service.device_login_presenter if method == 'device' else service.login_presenter
    if operation == 'login':
        if record['state'] in {'pending', 'starting', 'unknown', 'ready'}:
            return public(record)  # repeated command cannot start another login
        if presenter is None:
            raise FileNotFoundError('A trusted local browser login presenter is required; credentials cannot pass through chat')
        if any(a['state'] in {'pending', 'starting'} for a in service.store.list(owner, 'account')):
            raise Conflict('Finish or cancel the outstanding login first')
        record.update(state='starting', expires=time.time() + 900, method=method)
        service.store.put(owner, 'account', key, record)
        client = service.factory(account_home(service, owner, key), approval_handler=service._deny_control)
        service.account_clients[(owner, key)] = client
        service._mutating(owner, job)
        try:
            native_type = 'chatgptDeviceCode' if method == 'device' else 'chatgpt'
            response = client.call('account/login/start', {'type': native_type})
            if response.get('type') != native_type or not response.get('loginId') or not (response.get('verificationUrl') and response.get('userCode') if method == 'device' else response.get('authUrl')):
                raise Conflict('Unexpected native OAuth response; no credential fallback is permitted')
            record.update(state='pending', login_id=response['loginId'])
            service.store.put(owner, 'account', key, record)
            # Keep the link in memory only, and only deliver to trusted local UI.
            private = {k: response[k] for k in ('verificationUrl', 'userCode')} if method == 'device' else response['authUrl']
            service.login_links[(owner, key)] = private
            presenter(private)
            return public(record)
        except Exception:
            record['state'] = 'unknown'
            service.store.put(owner, 'account', key, record)
            service.login_links.pop((owner, key), None)
            raise Conflict('Login outcome is uncertain; inspect status or cancel before retrying') from None
    if record['state'] == 'pending' and record['expires'] <= time.time():
        operation = 'cancel'
        expired = True
    else:
        expired = False
    client = service.account_clients.get((owner, key))
    if operation == 'continue':
        presenter = service.device_login_presenter if record.get('method') == 'device' else service.login_presenter
        if record['state'] != 'pending' or (owner, key) not in service.login_links or presenter is None:
            raise Conflict('Login cannot be continued after expiry or runtime loss; inspect status first')
        try:
            presenter(service.login_links[(owner, key)])
        except Exception:
            raise Conflict('Secure login display unavailable; inspect status or cancel') from None
        return public(record)
    if operation == 'cancel':
        if record['state'] not in {'pending', 'unknown', 'stale'}:
            raise Conflict('No pending login to cancel')
        if client and record.get('login_id'):
            service._mutating(owner, job)
            ack = client.call('account/login/cancel', {'loginId': record['login_id']})
            if ack.get('status') not in {'canceled', 'cancelled', 'notFound'}:
                raise Conflict('Native cancellation was not confirmed')
        if client:
            client.close()
        service.account_clients.pop((owner, key), None)
        service.login_links.pop((owner, key), None)
        record.update(state='expired' if expired else 'cancelled')
        service.store.put(owner, 'account', key, record)
        return public(record)
    owned_client = client is None
    if client is None:
        client = service.factory(account_home(service, owner, key), approval_handler=service._deny_control)
    try:
        account = client.call('account/read', {'refreshToken': operation == 'refresh'})
        identity = account.get('account')
        record['identity'] = {k: identity[k] for k in ('type', 'email', 'planType') if k in identity} if identity else None
        if identity is not None or not account.get('requiresOpenaiAuth', True):
            record['state'] = 'ready'
            service.login_links.pop((owner, key), None)
        elif record['state'] != 'pending':
            record['state'] = 'unauthenticated'
        service.store.put(owner, 'account', key, record)
        if operation == 'switch':
            if record['state'] != 'ready':
                raise Denied('Selected account is not authenticated; no switch was made')
            with service.store.transaction():
                previous = service.selected_account(owner)
                try:
                    selection = service.store.get(owner, 'selection', 'current')
                except Denied:
                    selection = {}
                service.store.put(owner, 'account-selection', encoded([provider, previous]), selection)
                try:
                    restored = service.store.get(owner, 'account-selection', encoded([provider, key]))
                except Denied:
                    restored = {}
                service.store.put(owner, 'selected-account', provider, {'id': key, 'provider': provider})
                service.store.put(owner, 'selection', 'current', restored)
        return public(record)
    finally:
        if owned_client:
            client.close()
