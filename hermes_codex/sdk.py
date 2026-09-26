"""Pinned SDK qualification boundary; not a worker-execution implementation.

No authenticated work, login, goal activation, or arbitrary protocol calls are
available here. The SDK owns transport; this module never replaces its protocol.
"""
import importlib.metadata
import os
from pathlib import Path

from openai_codex.client import CodexClient, CodexConfig
from openai_codex.generated.v2_all import CommandExecResponse, GetAccountResponse


class SDK:
    def __init__(self, home: Path):
        if home.is_symlink():
            raise PermissionError('SDK home must not be a symlink')
        self.home = home.resolve(strict=True)
        if any(self.home.iterdir()):
            raise PermissionError('Qualification requires an empty isolated SDK home')
        self.home.chmod(0o700)
        # CodexConfig.env is a merge, NOT an allowlist. Mask every ambient key.
        env = {key: '' for key in os.environ}
        env.update(HOME=str(self.home), CODEX_HOME=str(self.home), PATH='/usr/bin:/bin')
        self.client = CodexClient(CodexConfig(env=env, cwd=str(self.home)), approval_handler=self._deny)
        self.metadata = None

    @staticmethod
    def _deny(method, params):
        if method in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}:
            return {'decision': 'decline'}
        raise PermissionError('Interactive server request has no authorized handler')

    def __enter__(self):
        try:
            self.client.start()
            self.metadata = self.client.initialize()
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        process = self.client._proc
        self.client.close()
        if process:
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()

    def __exit__(self, *args):
        self.close()

    def call(self, method, params):
        # Narrow diagnostics-only allowlist, not an unsupported-feature shim.
        if method != 'account/read' or params != {'refreshToken': False}:
            raise PermissionError('Qualification does not authorize this SDK operation')
        return self.client.request(method, params, response_model=GetAccountResponse)

    def health(self):
        account = self.call('account/read', {'refreshToken': False})
        return {
            'sdk_version': importlib.metadata.version('openai-codex'),
            'runtime_package_version': importlib.metadata.version('openai-codex-cli-bin'),
            'runtime_user_agent': self.metadata.userAgent,
            'authenticated': account.account is not None,
            'execution_ready': False,
            'reason': 'Qualification-only boundary; worker integration not delivered',
        }

    def qualify_sandbox(self):
        """Run two fixed, non-model commands in an empty disposable home.

        This cannot establish all worker permission controls; it demonstrates
        only local read-only command execution and denial of a file write.
        """
        sentinel = self.home / 'read-only-must-not-write'
        results = {}
        for name, command in (
            ('read_only_noop', ['/usr/bin/true']),
            ('read_only_write', ['/usr/bin/touch', str(sentinel)]),
        ):
            response = self.client.request('command/exec', {
                'command': command, 'cwd': str(self.home),
                'sandboxPolicy': {'type': 'readOnly', 'networkAccess': False},
                'timeoutMs': 10000, 'outputBytesCap': 2048,
            }, response_model=CommandExecResponse)
            results[name] = response.model_dump(mode='json')
        results['sentinel_exists'] = sentinel.exists()
        return results
