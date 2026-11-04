import http.client
import json
from pathlib import Path
import re
import subprocess
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from auth_local_once import LocalAuthOnce, auth_handler
from gmail_consent import ConsentError, ThreadingHTTPServer
import test_inbox_readonly as inbox_tests


class LocalAuthTests(unittest.TestCase):
    setUp = inbox_tests.InboxProofTests.setUp
    run_fake = inbox_tests.InboxProofTests.run_fake
    execute = inbox_tests.InboxProofTests.execute

    def new_state(self):
        return LocalAuthOnce(self.runtime, self.config, self.home / 'auth-proof',
                             runner=self.run_fake, clock=lambda: self.now,
                             authorized_local_auth_once=True)

    def test_one_local_call_durable_reservation_and_zero_other_operations(self):
        original = self.state.runner
        def runner(args, **kwargs):
            self.assertTrue((self.state.state_dir / 'attempt.json').exists())
            prior = json.loads((self.state.state_dir / 'result.json').read_text())
            self.assertEqual(prior['auth_local_reservations'], 1)
            self.assertEqual(prior['phase'], 'reserved_before_invocation')
            self.assertNotIn('--check', args)
            self.assertEqual(args[-2:], ['auth', 'list'])
            return original(args, **kwargs)
        self.state.runner = runner
        self.execute()
        self.assertTrue(self.state.saved)
        self.assertEqual(len(self.calls), 1)
        receipt = json.loads((self.state.state_dir / 'result.json').read_text())
        self.assertTrue(receipt['local_auth_validated'])
        self.assertEqual(receipt['auth_local_invocations'], 1)
        for key in ('auth_refresh_invocations', 'metadata_search_invocations', 'body_get_invocations',
                    'model_invocations', 'mail_mutation_invocations', 'delivery_invocations'):
            self.assertEqual(receipt[key], 0)
        for state in (self.state, self.new_state()):
            with self.assertRaises(ConsentError): self.execute(state)
        self.assertEqual(len(self.calls), 1)

    def test_missing_authorization_is_local_only(self):
        self.state.authorized_local_auth_once = False
        with self.assertRaises(ConsentError): self.execute()
        self.assertFalse(self.calls)
        self.assertFalse(self.state.state_dir.exists())

    def test_other_commands_and_model_are_forbidden_before_runner(self):
        for command, args, counter in [('auth.list', ['auth', 'list', '--check'], 'auth_local_invocations'),
                                        ('gmail.get', ['gmail', 'get', 'abc1'], 'body_get_invocations')]:
            with self.assertRaises(ValueError): self.state._gog(self.password, 'x', command, args, counter)
        with self.assertRaises(ValueError): self.state._model_run([])
        self.assertFalse(self.calls)

    def test_failure_is_classified_private_and_consumed(self):
        fixture = json.loads(Path('fixtures/gog-auth-list-v0.46.0.json').read_text())
        self.auth = fixture['unreadable_token']
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.state.receipt['auth_diagnostics'][0]['outcome'], 'validation_reported_error')
        original = (self.state.state_dir / 'result.json').read_bytes()
        with self.assertRaises(ConsentError): self.execute(self.new_state())
        self.assertEqual((self.state.state_dir / 'result.json').read_bytes(), original)
        artifacts = ''.join(p.read_text() for p in self.state.state_dir.iterdir())
        for marker in (self.password, 'synthetic@example.invalid', 'integrity check', 'synthetic diagnostic'):
            self.assertNotIn(marker, artifacts)
        self.assertEqual(self.state.state_dir.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in self.state.state_dir.iterdir()))

    def test_reservation_write_failure_prevents_auth_and_preserves_claim(self):
        with patch.object(self.state, '_persist_receipt', side_effect=OSError('private-path')):
            with self.assertRaises(ConsentError): self.execute()
        self.assertFalse(self.calls)
        self.assertTrue((self.state.state_dir / 'attempt.json').exists())
        with self.assertRaises(ConsentError): self.execute(self.new_state())
        self.assertFalse(self.calls)

    def test_timeout_no_retries_or_secret_in_receipt(self):
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            raise subprocess.TimeoutExpired(self.password, 45, output=self.password, stderr=self.password)
        self.state.runner = runner
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(self.state.receipt['auth_diagnostics'][0]['outcome'], 'timeout')
        self.assertNotIn(self.password, (self.state.state_dir / 'result.json').read_text())
        with self.assertRaises(ConsentError): self.execute(self.new_state())
        self.assertEqual(len(calls), 1)

    def test_concurrent_instances_share_durable_budget(self):
        states = [self.state, self.new_state()]
        results = []
        def run(state):
            try: self.execute(state); results.append('ok')
            except ConsentError: results.append('blocked')
        threads = [threading.Thread(target=run, args=(s,)) for s in states]
        for t in threads: t.start()
        for t in threads: t.join(timeout=5)
        self.assertEqual(sorted(results), ['blocked', 'ok'])
        self.assertEqual(len(self.calls), 1)

    def test_private_native_form_and_only_local_route(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), auth_handler(self.state, 'https://auth.invalid/'))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection(*server.server_address)
        self.addCleanup(conn.close)
        conn.request('GET', '/')
        response = conn.getresponse()
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        cookie = response.getheader('Set-Cookie').split(';')[0]
        page = response.read().decode();csrf = re.search('name="csrf" value="([^"]+)"', page)[1]
        body = urlencode({'csrf': csrf, 'password': self.password})
        headers = {'Cookie': cookie, 'Origin': 'null', 'Sec-Fetch-Site': 'same-origin',
                   'Content-Type': 'application/x-www-form-urlencoded'}
        for route, extra, status in [('/run', {}, 404), ('/verify-local', {'Origin':'https://evil.invalid'}, 403),
                                      ('/verify-local', {}, 200), ('/verify-local', {}, 400)]:
            conn.request('POST', route, body, {**headers, **extra})
            response = conn.getresponse();self.assertEqual(response.status, status);response.read()
        self.assertEqual(len(self.calls), 1)

    def test_expired_form_no_auth_or_claim(self):
        self.now = 601
        with self.assertRaises(ConsentError): self.execute()
        self.assertFalse(self.calls)
        self.assertFalse(self.state.state_dir.exists())
