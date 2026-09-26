"""Canonical policy checks; unrestricted access is never described as confined."""
from pathlib import Path
from .policy import workspace_path
from .state import Denied


def validate(brief, roots, allow_full_access=False):
    workspace = workspace_path(brief['workspace'], roots)
    writes = [workspace_path(p, roots) for p in brief['write_roots']]
    if len(set(writes)) != len(writes):
        raise ValueError('Write roots must be unique')
    if brief['sandbox'] == 'full-access':
        if not allow_full_access or writes != ['/'] or '/' not in roots or not brief['network']:
            raise Denied('Full access requires operator enablement, explicit host root / and unrestricted network authorization; it is NOT workspace confined')
    elif brief['sandbox'] == 'workspace-write':
        # Native workspaceWrite always includes cwd. Never claim narrower scope.
        if workspace not in writes or any(Path(workspace) not in Path(p).parents and p != workspace for p in writes):
            raise Denied('Workspace-write must authorize cwd; all extra roots must be within that workspace')
    return brief | {'workspace': workspace, 'write_roots': writes}


def identity(brief):
    result = {}
    for p in {brief['workspace'], *brief['write_roots']}:
        workspace_path(p, [p])
        info = Path(p).stat()
        result[p] = [info.st_dev, info.st_ino]
    return result


def thread_options(brief):
    options = {'cwd': brief['workspace'], 'approvalPolicy': 'on-request',
               'sandbox': 'danger-full-access' if brief['sandbox'] == 'full-access' else brief['sandbox']}
    if brief['sandbox'] == 'workspace-write':
        options['config'] = {'sandbox_workspace_write.writable_roots': brief['write_roots'],
                             'sandbox_workspace_write.network_access': brief['network'],
                             'sandbox_workspace_write.exclude_slash_tmp': True,
                             'sandbox_workspace_write.exclude_tmpdir_env_var': True}
    return options


def sandbox(brief):
    if brief['sandbox'] == 'read-only':
        return {'type': 'readOnly', 'networkAccess': False}
    if brief['sandbox'] == 'workspace-write':
        return {'type': 'workspaceWrite', 'writableRoots': brief['write_roots'], 'networkAccess': brief['network'],
                'excludeSlashTmp': True, 'excludeTmpdirEnvVar': True}
    if brief['sandbox'] == 'full-access':
        return {'type': 'dangerFullAccess'}
    raise Denied('Unknown sandbox policy')
