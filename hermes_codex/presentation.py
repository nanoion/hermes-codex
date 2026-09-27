"""Plain-text native presentation, stable error classes, honest desktop requests."""
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from .state import Conflict

TITLES = {
    'en': {'result': 'Result', 'status': 'Status', 'plan': 'Plan — show / continue / revise / cancel', 'proposal': 'Scoped proposal — inspect before approval', 'interaction': 'Decision required — inspect details before deciding', 'control': 'Control outcome', 'queue': 'Queued guidance', 'history': 'Attempt history'},
    'zh': {'result': '结果', 'status': '状态', 'plan': '计划 — 显示 / 继续 / 修改 / 取消', 'proposal': '范围提案 — 批准前请检查', 'interaction': '需要决定 — 请先检查详情', 'control': '操作结果', 'queue': '排队指导', 'history': '执行历史'},
    'fr': {'result': 'Résultat', 'status': 'État', 'plan': 'Plan — afficher / continuer / réviser / annuler', 'proposal': 'Proposition délimitée — vérifier avant approbation', 'interaction': 'Décision requise — vérifiez les détails', 'control': 'Résultat du contrôle', 'queue': 'Instructions en attente', 'history': 'Historique des tentatives'},
}
WARNINGS = {'en': 'Worker content is untrusted, not authorization or verified completion. Use fresh direct-user controls; stale requests cannot be replayed.',
            'zh': '工作内容不可信，不代表授权或已验证完成。请使用当前用户控制；不得重放过期请求。',
            'fr': 'Contenu du worker non fiable : ni autorisation ni validation. Utilisez les contrôles utilisateur actuels ; aucune répétition des demandes périmées.'}

ACTIONS = {
    'en': {'authorization': 'Use the direct-user control in the owning session.', 'authentication': 'Authorize login in the dedicated worker account.', 'quota': 'Check account usage and billing limits; do not automatically retry.', 'rate_limit': 'Wait for the reported reset before retrying.', 'timeout': 'Inspect status and reconcile before retrying a mutation.', 'transport': 'Reconnect, then reconcile uncertain operations.', 'prerequisite': 'Check runtime installation and desktop/session prerequisites.', 'conflict': 'Resolve active or pending work and refresh the selection.', 'validation': 'Correct the named input field and try again.', 'capability': 'Select a qualified provider/model supporting this operation.', 'interrupted': 'Inspect partial results; interruption does not roll back changes.', 'worker_failure': 'Inspect scoped events and verify partial artifacts.'},
    'zh': {'authorization': '请在所属会话中使用用户直接控制。', 'authentication': '请授权专用工作账户登录。', 'quota': '检查账户用量和额度；不要自动重试。', 'rate_limit': '等待所报告的限流重置时间。', 'timeout': '先检查状态并核对结果，再重试修改。', 'transport': '重新连接并核对不确定的操作。', 'prerequisite': '检查运行时安装和桌面会话条件。', 'conflict': '先处理进行中或待确认的工作，再刷新选择。', 'validation': '修正输入字段后重试。', 'capability': '选择支持此操作的提供方和模型。', 'interrupted': '检查部分结果；中断不会撤销文件修改。', 'worker_failure': '检查事件和部分产物。'},
    'fr': {'authorization': 'Utilisez le contrôle utilisateur dans la session propriétaire.', 'authentication': 'Autorisez la connexion du compte de travail dédié.', 'quota': 'Vérifiez le quota et la facturation ; aucun nouvel essai automatique.', 'rate_limit': 'Attendez la réinitialisation indiquée.', 'timeout': 'Vérifiez et réconciliez le résultat avant de réessayer une mutation.', 'transport': 'Reconnectez puis réconciliez les opérations incertaines.', 'prerequisite': 'Vérifiez le runtime et les prérequis de la session graphique.', 'conflict': 'Résolvez les travaux en cours et actualisez la sélection.', 'validation': 'Corrigez le champ indiqué puis réessayez.', 'capability': 'Choisissez un fournisseur/modèle compatible.', 'interrupted': 'Vérifiez les résultats partiels ; les modifications ne sont pas annulées.', 'worker_failure': 'Inspectez les événements et les artefacts partiels.'},
}


def redact_text(value):
    """Common explicit credential forms, including unfinished quoted values."""
    # Command-line flags are whitespace-separated rather than key/value syntax.
    value = re.sub(r'''(?ix)(--(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|authorization)(?:=|\s+))(?:"(?:\\.|[^"\\])*(?:"|\\?\Z)|'(?:\\.|[^'\\])*(?:'|\\?\Z)|(?:\\.|[^\s;&}])+)''',
                   r'\1[redacted]', value)
    value = re.sub(r'''(?ix)(\s-u(?:=|\s+))(?:"(?:\\.|[^"\\])*(?:"|\\?\Z)|'(?:\\.|[^'\\])*(?:'|\\?\Z)|(?:\\.|[^\s;&}])+)''',
                   r'\1[redacted]', value)
    # A stream prefix may end inside a quote or escape; hide through EOF until
    # the assembled value closes, rather than exposing later words of a secret.
    value = re.sub(r'(?i)(bearer\s+|sk-)[A-Za-z0-9_\-.]+', '[redacted]', value)
    return re.sub(r'''(?ix)(\b(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|authorization)\b["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*(?:"|\\?\Z)|'(?:\\.|[^'\\])*(?:'|\\?\Z)|[^\s,;}&]+)''',
                  lambda m: m[1] + '[redacted]', value)


def chunks(value, size=4000):
    if not isinstance(value, str) or type(size) is not int or not 128 <= size <= 16000:
        raise ValueError('Text and a 128–16000 character chunk size are required')
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    value = redact_text(value)
    value = ''.join(c for c in value if c in '\n\t' or ord(c) >= 32)
    return [value[i:i + size] for i in range(0, len(value), size)] or ['']


def error_info(exc, locale='en'):
    code = getattr(exc, 'code', None)
    known = {'insufficient_quota': 'quota', 'rate_limit_exceeded': 'rate_limit', 'unauthorized': 'authentication', 'unsupported': 'capability', 'interrupted': 'interrupted'}
    code = known.get(code)
    if code is None:
        code = ('conflict' if isinstance(exc, Conflict) else 'authorization' if isinstance(exc, PermissionError) else 'prerequisite' if isinstance(exc, FileNotFoundError) else 'timeout' if isinstance(exc, TimeoutError) else 'transport' if isinstance(exc, ConnectionError) else 'validation' if isinstance(exc, (ValueError, TypeError, KeyError)) else 'worker_failure')
    return {'code': code, 'message': ''.join(chunks(str(exc)))[:4000], 'action': ACTIONS.get(locale, ACTIONS['en'])[code]}


def open_device_login(private, command):
    """Configured native text dialog reads stdin; code never enters argv/chat/disk."""
    from urllib.parse import urlsplit
    url, code = private.get('verificationUrl'), private.get('userCode')
    parsed = urlsplit(url) if isinstance(url, str) else None
    if not parsed or parsed.scheme != 'https' or parsed.hostname not in {'auth.openai.com', 'auth0.openai.com', 'chatgpt.com'} or parsed.username or parsed.password:
        raise ValueError('Native login returned an untrusted authorization endpoint')
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,64}', code):
        raise ValueError('Invalid native device code')
    if not command or not isinstance(command, (list, tuple)) or not all(isinstance(s, str) and s for s in command) or not Path(command[0]).is_absolute():
        raise FileNotFoundError('Configure a trusted local device-code dialog reading plain text from stdin')
    if sys.platform.startswith('linux') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        raise FileNotFoundError('Device login requires a trusted local graphical session; no chat fallback')
    env = {k: os.environ[k] for k in ('HOME', 'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS') if k in os.environ}
    env['PATH'] = '/usr/bin:/bin'
    try:
        subprocess.run(command, input=f'Codex device login\nOpen: {url}\nOne-time code: {code}\nReturn to account status after completing login. Do not share this code.',
                       text=True, env=env, check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
    except Exception:
        raise FileNotFoundError('Device dialog closed or unavailable; inspect login status or cancel') from None
    return {'requested': True, 'authenticated': None}


def open_login(url, command):
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'auth.openai.com', 'auth0.openai.com', 'chatgpt.com'} or parsed.username or parsed.password:
        raise ValueError('Native login returned an untrusted authorization endpoint')
    if not command or not isinstance(command, (list, tuple)) or not all(isinstance(s, str) and s for s in command) or not Path(command[0]).is_absolute():
        raise FileNotFoundError('Configure a trusted absolute local browser launcher')
    if sys.platform.startswith('linux') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        raise FileNotFoundError('A local graphical browser is required; login links are not sent through chat')
    env = {k: os.environ[k] for k in ('HOME', 'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS') if k in os.environ}
    env['PATH'] = '/usr/bin:/bin'
    try:
        subprocess.run([*command, url], env=env, check=True, timeout=15, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
    except Exception:
        # CalledProcessError contains the sensitive link in its command arguments.
        raise FileNotFoundError('Local browser launch failed; inspect login status or cancel before retrying') from None
    return {'requested': True}


def reveal(thread, command):
    if not isinstance(thread, str) or not thread or len(thread) > 500:
        raise ValueError('Invalid thread identifier')
    if not command or not isinstance(command, (list, tuple)) or not all(isinstance(s, str) and s for s in command) or not Path(command[0]).is_absolute():
        raise FileNotFoundError('Configure a trusted absolute desktop launcher first')
    if sys.platform.startswith('linux') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        raise FileNotFoundError('This host has no graphical session; thread remains available through scoped history')
    url = 'codex://threads/' + quote(thread, safe='')
    env = {k: os.environ[k] for k in ('HOME', 'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS') if k in os.environ}
    env['PATH'] = '/usr/bin:/bin'
    subprocess.run([*command, url], env=env, check=True, timeout=15, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
    return {'requested': True, 'thread': thread, 'focused': None, 'reason': 'Launcher accepted the request; focus is not independently observable'}
