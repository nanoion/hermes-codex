import unittest
from unittest.mock import patch


class PresentationTests(unittest.TestCase):
    def test_localized_actionable_errors_and_lossless_safe_chunks(self):
        import hermes_codex
        self.assertTrue(hasattr(hermes_codex, 'presentation'), 'Localized presentation missing')
        from hermes_codex.presentation import error_info, chunks
        for locale in ('en', 'zh', 'fr'):
            for exception, code in [(PermissionError('auth token sk-secret'), 'authorization'), (TimeoutError(), 'timeout'), (ConnectionError(), 'transport'), (FileNotFoundError(), 'prerequisite'), (ValueError(), 'validation')]:
                result = error_info(exception, locale)
                self.assertEqual(result['code'], code)
                self.assertTrue(result['action'])
                self.assertNotIn('sk-secret', str(result))
        content = ('hello <script>not executable</script>\n' * 2000) + '\x1b[31m bearer abc123'
        parts = chunks(content, 1024)
        self.assertTrue(all(len(p) <= 1024 for p in parts))
        self.assertEqual(''.join(parts).count('hello'), 2000)
        self.assertNotIn('abc123', ''.join(parts))
        self.assertNotIn('\x1b', ''.join(parts))

    def test_sensitive_key_variants_are_redacted_but_usage_is_visible(self):
        from hermes_codex.service import sanitized
        values = {k: 'private-value' for k in ('api_key', 'access_token', 'authUrl', 'userCode', 'secret_access_key', 'Password')}
        values['tokensUsed'] = 123
        result = sanitized(values)
        self.assertNotIn('private-value', str(result))
        self.assertEqual(result['tokensUsed'], 123)

    def test_login_link_uses_trusted_local_browser_and_never_leaks(self):
        from hermes_codex import presentation
        self.assertTrue(hasattr(presentation, 'open_login'), 'Secure OAuth presenter missing')
        import subprocess
        url = 'https://auth.openai.com/oauth/authorize?state=private-value'
        with patch.dict('os.environ', {'DISPLAY': ':1'}, clear=True), patch('subprocess.run', side_effect=subprocess.CalledProcessError(1, ['/usr/bin/xdg-open', url])):
            with self.assertRaises(FileNotFoundError) as raised:
                presentation.open_login(url, ['/usr/bin/xdg-open'])
            self.assertNotIn('private-value', str(raised.exception))
            with self.assertRaises(ValueError):
                presentation.open_login('https://attacker.invalid/', ['/usr/bin/xdg-open'])

    def test_desktop_headless_and_exact_encoded_thread_request(self):
        import hermes_codex
        self.assertTrue(hasattr(hermes_codex, 'presentation'), 'Desktop boundary missing')
        from hermes_codex.presentation import reveal
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(FileNotFoundError):
                reveal('thread-1', ['/usr/bin/xdg-open'])
        with patch.dict('os.environ', {'DISPLAY': ':1', 'SECRET': 'must-not-leak'}, clear=True), patch('subprocess.run') as run:
            run.return_value.returncode = 0
            result = reveal('thread/with space', ['/usr/bin/xdg-open'])
            self.assertTrue(result['requested'])
            self.assertIsNone(result['focused'])
            self.assertEqual(run.call_args.args[0][-1], 'codex://threads/thread%2Fwith%20space')
            self.assertNotIn('SECRET', run.call_args.kwargs['env'])
