import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.parse import urlencode

from gmail_consent import ConsentError, READONLY, ThreadingHTTPServer
from gmail_unlock import Verification, verification_handler


class UnlockTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.root.chmod(0o700)
        (self.root / 'gog').mkdir(mode=0o700)
        account = self.root / 'gmail-account.pending'
        account.write_text('synthetic@example.invalid')
        account.chmod(0o600)
        self.now, self.calls = 0, []
        self.password = 'synthetic-password-not-real'
        self.row = {'email': 'synthetic@example.invalid', 'client': 'default', 'auth': 'oauth',
                    'services': ['gmail'], 'scopes': [READONLY, 'openid', 'email'], 'valid': True}
        self.result = {'accounts': [self.row]}
        self.state = Verification(self.root, runner=self.run_gog, clock=lambda: self.now)

    def run_gog(self, args, **kwargs):
        self.calls.append((args, dict(kwargs['env'])))
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.result))

    def verify(self):
        form = {'password': [self.password]}
        try:
            self.state.verify(form)
        finally:
            self.assertEqual(form, {})

    def test_local_only_sanitized_receipt_and_no_password_retention(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.verify()
        args, env = self.calls[0]
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(args[-2:], ['auth', 'list'])
        self.assertIn('--readonly', args)
        self.assertIn('--gmail-no-send', args)
        self.assertNotIn(self.password, str(args))
        self.assertEqual(env['GOG_KEYRING_PASSWORD'], self.password)
        path = self.root / 'gmail-keyring-verification.json'
        receipt = path.read_text()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(json.loads(receipt)['effective_scopes_verified'])
        self.assertFalse(json.loads(receipt)['refresh_checked'])
        for secret in [self.password, self.row['email']]:
            self.assertNotIn(secret, receipt + output.getvalue() + repr(vars(self.state)))
        self.assertTrue(self.state.saved)
        with self.assertRaises(ConsentError): self.verify()
        self.assertEqual(len(self.calls), 1)

    def test_remote_explicit_and_only_after_local_policy(self):
        self.state.check_google = True
        self.verify()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1][0][-2:], ['--check', '--timeout=15s'])
        receipt = json.loads((self.root / 'gmail-keyring-verification.json').read_text())
        self.assertTrue(receipt['refresh_checked'])
        self.assertFalse(receipt['effective_scopes_verified'])

    def test_wrong_account_client_service_scopes_and_malformed_fail_closed(self):
        for patch in [{'email': 'other@example.invalid'}, {'client': 'other'},
                      {'services': ['gmail', 'drive']}, {'auth': 'service_account'},
                      {'scopes': [READONLY, 'https://www.googleapis.com/auth/gmail.send']},
                      {'scopes': []}, {'scopes': None}, {'error': 'private-error'}]:
            with self.subTest(patch=patch):
                self.state = Verification(self.root, check_google=True, runner=self.run_gog)
                self.result = {'accounts': [dict(self.row, **patch)]}
                before = len(self.calls)
                with self.assertRaises(ConsentError): self.verify()
                self.assertEqual(len(self.calls), before + 1)
                self.assertFalse((self.root / 'gmail-keyring-verification.json').exists())
        for result in [[], None, {'accounts': []}, {'accounts': [self.row, self.row]}]:
            self.state = Verification(self.root, runner=self.run_gog)
            self.result = result
            with self.assertRaises(ConsentError): self.verify()

    def test_remote_invalid_refresh_no_receipt(self):
        self.state.check_google = True
        self.row['valid'] = False
        with self.assertRaises(ConsentError): self.verify()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse((self.root / 'gmail-keyring-verification.json').exists())

    def test_expiry_attempt_limit_and_permissions_before_subprocess(self):
        self.now = 600
        with self.assertRaises(ConsentError): self.verify()
        self.now = 0
        (self.root / 'gmail-account.pending').chmod(0o644)
        for _ in range(4):
            with self.assertRaises(ConsentError): self.verify()
        self.assertEqual(self.state.attempts, 3)
        self.assertFalse(self.calls)

    def test_symlink_and_directory_permissions_rejected(self):
        account = self.root / 'gmail-account.pending'
        account.rename(self.root / 'actual')
        account.symlink_to(self.root / 'actual')
        with self.assertRaises(ConsentError): self.verify()
        self.assertFalse(self.calls)
        account.unlink()
        (self.root / 'actual').rename(account)
        (self.root / 'gog').chmod(0o755)
        with self.assertRaises(ConsentError): self.verify()
        self.assertFalse(self.calls)

    def test_subprocess_failures_redacted_and_environment_password_removed(self):
        for failure in [subprocess.TimeoutExpired(['hidden'], 1, output=self.password),
                        ValueError(self.password), OSError(self.password)]:
            observed = []
            def broken(args, **kwargs):
                observed.append(kwargs['env'])
                raise failure
            self.state = Verification(self.root, runner=broken)
            with self.assertRaises(ConsentError) as error: self.verify()
            self.assertNotIn(self.password, str(error.exception))
            self.assertNotIn('GOG_KEYRING_PASSWORD', observed[0])
            self.assertFalse(self.state.saved)

    def test_http_native_form_security_and_no_oauth_routes(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0),
                                     verification_handler(self.state, 'https://private.invalid/'))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
        self.addCleanup(conn.close)
        conn.request('GET', '/')
        response = conn.getresponse()
        cookie = response.getheader('Set-Cookie').split(';')[0]
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        csrf = response.read().decode().split('name="csrf" value="')[1].split('"')[0]
        headers = {'Origin': 'null', 'Sec-Fetch-Site': 'same-origin', 'Cookie': cookie,
                   'Content-Type': 'application/x-www-form-urlencoded'}
        form = urlencode({'csrf': csrf, 'password': self.password})
        for change, body, path, expected in [
            ({'Origin': 'https://evil.invalid'}, form, '/verify', 403),
            ({'Sec-Fetch-Site': ''}, form, '/verify', 403),
            ({'Sec-Fetch-Site': 'cross-site'}, form, '/verify', 403),
            ({'Cookie': ''}, form, '/verify', 403),
            ({}, form.replace(csrf, 'bad'), '/verify', 403),
            ({}, form + '&password=other', '/verify', 403),
            ({}, form, '/start', 404), ({}, form, '/finish', 404),
        ]:
            conn.request('POST', path, body, dict(headers, **change))
            response = conn.getresponse()
            self.assertEqual(response.status, expected)
            response.read()
        self.assertFalse(self.calls)
        conn.request('POST', '/verify', form, headers)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        page = response.read().decode()
        self.assertIn('Comprobación terminada', page)
        self.assertNotIn(self.password, page)
        self.assertEqual(len(self.calls), 1)

    def test_reject_tokenized_or_invalid_public_url(self):
        for url in ['http://private.invalid', 'https://private.invalid/#secret',
                    'https://user:secret@private.invalid', 'https://private.invalid/?token=x']:
            with self.assertRaises(ValueError): verification_handler(self.state, url)

    def test_real_subprocess_boundary_with_synthetic_executable(self):
        script = self.root / 'fake_gog.py'
        script.write_text('import json, os, sys\n'
                          'assert "GOG_ACCESS_TOKEN" not in os.environ\n'
                          'assert os.environ["GOG_KEYRING_PASSWORD"] not in sys.argv\n'
                          'assert sys.stdin.read() == ""\n'
                          'print(' + repr(json.dumps(self.result)) + ')\n')
        def process(args, **kwargs):
            return subprocess.run([sys.executable, str(script)] + args[1:], **kwargs)
        self.state = Verification(self.root, runner=process)
        from unittest.mock import patch
        with patch.dict(os.environ, {'GOG_ACCESS_TOKEN': 'synthetic-bypass',
                                     'HTTPS_PROXY': 'http://untrusted.invalid'}):
            self.verify()
        self.assertTrue(self.state.saved)


if __name__ == '__main__':
    unittest.main()
