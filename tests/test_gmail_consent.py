import contextlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from urllib.parse import urlencode

from gmail_consent import Consent, ConsentError, READONLY, handler_for, ThreadingHTTPServer


class ConsentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, content in {
            'google-desktop-client.json': json.dumps({'installed': {
                'client_id': 'test-client', 'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                'token_uri': 'https://oauth2.googleapis.com/token'}}),
            'gmail-account.pending': 'test@example.invalid',
        }.items():
            p = self.root / name
            p.write_text(content)
            p.chmod(0o600)
        self.query = {'client_id': 'test-client', 'response_type': 'code', 'state': 'test-state',
                      'redirect_uri': 'http://127.0.0.1:1234/oauth2/callback',
                      'scope': READONLY + ' openid email', 'include_granted_scopes': 'false'}
        self.url = 'https://accounts.google.com/o/oauth2/auth?' + urlencode(self.query)
        self.redirect = 'http://127.0.0.1:1234/oauth2/callback?state=test-state&code=hidden-code'
        self.calls = []
        self.now = 0
        self.identity = 'test@example.invalid'
        self.password = 'synthetic-password-only'
        self.state = Consent(self.root, self.run_gog, lambda: self.now)

    def run_gog(self, args, **kwargs):
        self.calls.append((args, dict(kwargs['env'])))
        if 'credentials' in args:
            value = {'saved': True, 'client_secret_in_keyring': True}
        elif args[args.index('--step') + 1] == '1':
            value = {'auth_url': self.url}
        else:
            value = {'stored': True, 'email': self.identity, 'services': ['gmail']}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))

    def begin(self):
        self.state.begin(self.password, self.password)

    def test_complete_only_auth_readonly_and_sanitized_receipt(self):
        self.begin()
        self.state.finish(self.redirect)
        self.assertTrue(self.state.saved)
        self.assertIsNone(self.state.password)
        self.assertEqual(len(self.calls), 3)
        for args, env in self.calls[1:]:
            self.assertEqual(args[args.index('--services') + 1], 'gmail')
            self.assertEqual(args[args.index('--gmail-scope') + 1], 'readonly')
            self.assertIn('--readonly', args)
            self.assertIn('--gmail-no-send', args)
            self.assertEqual(env['GOG_KEYRING_BACKEND'], 'file')
            self.assertEqual(env['GOG_KEYRING_PASSWORD'], self.password)
        receipt = (self.root / 'gmail-remote-consent-receipt.json').read_text()
        for sensitive in [self.password, 'hidden-code', 'test@example.invalid', 'test-state']:
            self.assertNotIn(sensitive, receipt)
        self.assertFalse(json.loads(receipt)['messages_read'])

    def test_reject_expanded_scopes_wrong_domain_client_and_incremental(self):
        for key, value in [('scope', READONLY + ' https://www.googleapis.com/auth/gmail.send'),
                           ('client_id', 'other'), ('include_granted_scopes', 'true'),
                           ('redirect_uri', 'https://evil.invalid/callback')]:
            with self.subTest(key=key):
                q = dict(self.query, **{key: value})
                self.url = 'https://accounts.google.com/o/oauth2/auth?' + urlencode(q)
                with self.assertRaises(ConsentError): self.begin()
                self.assertIsNone(self.state.password)
        self.url = 'https://evil.invalid/?' + urlencode(self.query)
        with self.assertRaises(ConsentError): self.begin()

    def test_wrong_state_or_callback_never_exchanges(self):
        for redirect in [self.redirect.replace('test-state', 'wrong-state'),
                         self.redirect.replace('127.0.0.1', 'evil.invalid')]:
            self.begin()
            before = len(self.calls)
            with self.assertRaises(ConsentError): self.state.finish(redirect)
            self.assertEqual(before, len(self.calls))
            self.assertIsNone(self.state.password)

    def test_duplicate_oauth_parameters_rejected_before_exchange(self):
        self.url += '&scope=' + READONLY
        with self.assertRaises(ConsentError):
            self.begin()
        self.assertIsNone(self.state.password)
        self.url = 'https://accounts.google.com/o/oauth2/auth?' + urlencode(self.query)
        for suffix in ('&code=other-code', '&state=test-state', '#unexpected'):
            self.begin()
            before = len(self.calls)
            with self.assertRaises(ConsentError):
                self.state.finish(self.redirect + suffix)
            self.assertEqual(len(self.calls), before)

    def test_expiry_drops_password_and_refuses_exchange(self):
        self.begin()
        self.now = 601
        with self.assertRaises(ConsentError): self.state.finish(self.redirect)
        self.assertIsNone(self.state.password)
        self.assertEqual(len(self.calls), 2)

    def test_wrong_identity_has_no_receipt(self):
        self.begin()
        self.identity = 'other@example.invalid'
        with self.assertRaises(ConsentError): self.state.finish(self.redirect)
        self.assertFalse(self.state.saved)
        self.assertFalse((self.root / 'gmail-remote-consent-receipt.json').exists())

    def test_password_confirmation_and_permissions_before_subprocess(self):
        with self.assertRaises(ConsentError): self.state.begin(self.password, 'mismatch')
        (self.root / 'google-desktop-client.json').chmod(0o644)
        with self.assertRaises(ConsentError): self.begin()
        self.assertFalse(self.calls)

    def test_http_csrf_origin_cookie_and_hidden_redirect(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.state, 'https://private.invalid:443/'))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
        self.addCleanup(connection.close)
        connection.request('GET', '/')
        response = connection.getresponse()
        set_cookie = response.getheader('Set-Cookie')
        cookie = set_cookie.split(';')[0]
        for attribute in ('Secure', 'HttpOnly', 'SameSite=None', 'Partitioned'):
            self.assertIn(attribute, set_cookie)
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        self.assertEqual(response.getheader('Referrer-Policy'), 'no-referrer')
        page = response.read().decode()
        csrf = page.split('name="csrf" value="')[1].split('"')[0]
        headers = {'Origin': 'https://private.invalid:443', 'Cookie': cookie,
                   'Content-Type': 'application/x-www-form-urlencoded'}
        form = {'csrf': csrf, 'password': self.password, 'confirmation': self.password}
        for change in [{'Origin': 'https://evil.invalid'}, {'Cookie': ''}]:
            connection.request('POST', '/start', urlencode(form), dict(headers, **change))
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            self.assertIn('Volver', response.read().decode())
        connection.request('POST', '/start', urlencode(dict(form, csrf='wrong')), headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        self.assertFalse(self.calls)
        connection.request('POST', '/start', urlencode(form), headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        page = response.read().decode()
        self.assertIn('Abrir consentimiento de Google', page)
        self.assertNotIn(self.password, page)
        self.assertIn('type="password" name="redirect"', page)
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            connection.request('POST', '/finish', urlencode({'csrf': csrf, 'redirect': self.redirect}), headers)
            response = connection.getresponse()
            body = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn('Consentimiento guardado', body)
        self.assertNotIn('hidden-code', body + output.getvalue())

    def test_http_no_referrer_form_requires_same_origin_metadata_cookie_and_csrf(self):
        # Native form navigation under the portal's no-referrer policy sends
        # Origin: null, unlike an HTTP client that manually supplies Origin.
        server = ThreadingHTTPServer(('127.0.0.1', 0),
                                     handler_for(self.state, 'https://private.invalid/'))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
        self.addCleanup(connection.close)
        connection.request('GET', '/')
        response = connection.getresponse()
        cookie = response.getheader('Set-Cookie').split(';')[0]
        csrf = response.read().decode().split('name="csrf" value="')[1].split('"')[0]
        form = {'csrf': csrf, 'password': self.password, 'confirmation': self.password}
        headers = {'Origin': 'null', 'Sec-Fetch-Site': 'same-origin', 'Cookie': cookie,
                   'Content-Type': 'application/x-www-form-urlencoded'}
        for change, body in [
            ({'Sec-Fetch-Site': ''}, form),
            ({'Sec-Fetch-Site': 'cross-site'}, form),
            ({'Sec-Fetch-Site': 'same-site'}, form),
            ({'Sec-Fetch-Site': 'none'}, form),
            ({'Origin': ''}, form),
            ({'Origin': 'https://evil.invalid'}, form),
            ({'Cookie': ''}, form),
            ({}, dict(form, csrf='wrong')),
        ]:
            with self.subTest(change=change, csrf_valid=body['csrf'] == csrf):
                connection.request('POST', '/start', urlencode(body), dict(headers, **change))
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
        self.assertFalse(self.calls)
        connection.request('POST', '/start', urlencode(form), headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn('Abrir consentimiento de Google', response.read().decode())
        self.assertEqual(len(self.calls), 2)


if __name__ == '__main__':
    unittest.main()
