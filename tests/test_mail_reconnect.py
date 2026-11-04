import contextlib
import io
import json
import os
from pathlib import Path
import pty
import select
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from gmail_consent import READONLY
from mail_readonly import MailSession, credential_root, run_terminal
from mail_reconnect import RecoveryConsent, activate_store, new_store, run_reconnect
import test_inbox_readonly
from test_jev_decisions import FakeDecider, summary_fixture


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_inbox_readonly.InboxProofTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        summary_fixture(f)
        self.legacy = f.runtime
        client = self.legacy / 'google-desktop-client.json'
        client.write_text(json.dumps({'installed': {
            'client_id': 'synthetic-client', 'client_secret': 'SYNTHETIC_SECRET_NOT_FOR_LOGS',
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token'}}))
        client.chmod(0o600)
        (self.legacy / 'gog/untouched-old-store').write_text('OLD_CIPHERTEXT_SENTINEL')
        self.oauth_calls = []
        self.query = {'client_id': 'synthetic-client', 'response_type': 'code', 'state': 'synthetic-state',
                      'redirect_uri': 'http://127.0.0.1:1234/oauth2/callback',
                      'scope': READONLY + ' openid email', 'include_granted_scopes': 'false'}
        self.redirect = 'http://127.0.0.1:1234/oauth2/callback?state=synthetic-state&code=HIDDEN_CODE'
        self.state_dir = f.home / 'recovery-session'
        self.state = None
        self.oauth_failure = False

    def oauth_runner(self, args, **kwargs):
        self.oauth_calls.append((args, dict(kwargs['env'])))
        self.assertEqual(kwargs['env']['GOG_KEYRING_PASSWORD'], self.fixture.password)
        self.assertNotIn(self.fixture.password, str(args))
        self.assertNotIn('GOG_ACCESS_TOKEN', kwargs['env'])
        self.assertIn('--enable-commands-exact', args)
        if 'credentials' in args:
            value = {'saved': True, 'client_secret_in_keyring': True}
        elif args[args.index('--step') + 1] == '1':
            self.assertIn('--force-consent', args)
            value = {'auth_url': 'https://accounts.google.com/o/oauth2/auth?' + urlencode(self.query)}
        else:
            value = {'stored': True, 'email': 'synthetic@example.invalid', 'services': ['gmail']}
            if self.oauth_failure:
                return SimpleNamespace(returncode=1, stderr='invalid_grant PRIVATE_OAUTH_ERROR', stdout='')
        return SimpleNamespace(returncode=0, stderr='PRIVATE_OAUTH_HINT', stdout=json.dumps(value))

    def make_state(self, *args, **kwargs):
        self.state = MailSession(*args, runner=self.fixture.run_fake, clock=lambda: self.fixture.now,
                                 decider=FakeDecider())
        return self.state

    def run_flow(self, *, redirect=None):
        output = io.StringIO()
        with (patch('mail_reconnect.hidden', side_effect=[self.fixture.password, self.fixture.password,
                                                        redirect or self.redirect]),
              patch('mail_reconnect.MailSession', side_effect=self.make_state),
              patch('mail_reconnect.RecoveryConsent', side_effect=lambda root, **kw: RecoveryConsent(
                  root, runner=self.oauth_runner, **kw)),
              patch.dict(os.environ, {'GOG_ACCESS_TOKEN': 'PRIVATE_ENV_SECRET'}),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(output)):
            rc = run_reconnect(self.legacy, self.fixture.config, self.state_dir)
        return rc, output.getvalue()

    def test_reconnect_validates_auth_then_reads_analyzes_and_selects_without_touching_old_store(self):
        before = {str(p.relative_to(self.legacy)): p.read_bytes() for p in self.legacy.rglob('*') if p.is_file()}
        rc, output = self.run_flow()
        self.assertEqual(rc, 0)
        selected = credential_root(self.legacy)
        self.assertNotEqual(selected, self.legacy)
        self.assertEqual(self.state.root, selected)
        for name, data in before.items():
            self.assertEqual((self.legacy / name).read_bytes(), data)
        self.assertFalse((selected / 'gog/untouched-old-store').exists())
        self.assertEqual(len(self.oauth_calls), 3)
        self.assertEqual(len(self.fixture.calls), 6)
        self.assertTrue(self.state.receipt['functional_passed'])
        self.assertTrue(self.state.receipt['recovery_store_selected'])
        self.assertTrue(self.state.receipt['oauth_reconnected'])
        self.assertIn(self.fixture.analysis['summary'], output)
        receipts = ''.join(p.read_text() for p in self.state_dir.glob('*.json'))
        for private in [self.fixture.password, self.redirect, 'HIDDEN_CODE', 'PRIVATE_OAUTH_HINT',
                        'PRIVATE_ENV_SECRET', self.fixture.private_marker, 'SYNTHETIC_SECRET_NOT_FOR_LOGS']:
            self.assertNotIn(private, output + receipts)
        self.assertEqual((self.legacy / 'readonly-credentials.json').stat().st_mode & 0o777, 0o600)

    def test_wrong_callback_state_exchanges_nothing_selects_nothing_and_reads_nothing(self):
        rc, _ = self.run_flow(redirect=self.redirect.replace('synthetic-state', 'wrong-state'))
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.oauth_calls), 2)
        self.assertFalse(self.fixture.calls)
        self.assertEqual(credential_root(self.legacy), self.legacy)

    def test_expanded_scope_url_is_rejected_before_consent_or_mail(self):
        self.query['scope'] += ' https://www.googleapis.com/auth/gmail.send'
        rc, _ = self.run_flow()
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.oauth_calls), 2)
        self.assertFalse(self.fixture.calls)
        self.assertEqual(credential_root(self.legacy), self.legacy)

    def test_saved_oauth_with_excess_stored_scopes_is_not_selected_and_no_refresh_or_mail(self):
        self.fixture.auth['accounts'][0]['scopes'].append('https://mail.google.com/')
        rc, _ = self.run_flow()
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(credential_root(self.legacy), self.legacy)

    def test_model_failure_preserves_validated_connection_but_not_success_or_analysis(self):
        self.fixture.model_rc = 1
        rc, output = self.run_flow()
        self.assertEqual(rc, 1)
        self.assertNotEqual(credential_root(self.legacy), self.legacy)
        self.assertFalse(self.state.receipt['functional_passed'])
        self.assertTrue(self.state.receipt['recovery_store_selected'])
        self.assertNotIn('Conexión readonly restablecida.', output)
        self.assertEqual(len(self.fixture.calls), 5)

    def test_oauth_failure_retains_category_not_private_error_and_never_reads(self):
        self.oauth_failure = True
        rc, output = self.run_flow()
        self.assertEqual(rc, 1)
        self.assertFalse(self.fixture.calls)
        self.assertEqual(credential_root(self.legacy), self.legacy)
        receipt = (self.state_dir / 'result.json').read_text()
        self.assertIn('oauth_code_expired_or_rejected', receipt)
        self.assertNotIn('PRIVATE_OAUTH_ERROR', output + receipt)

    def test_unvalidated_store_cannot_be_activated(self):
        root = new_store(self.legacy)
        state = self.make_state(root, self.fixture.config, self.state_dir)
        with self.assertRaises(ValueError):
            activate_store(self.legacy, root, state)
        self.assertEqual(credential_root(self.legacy), self.legacy)

    def test_pointer_traversal_duplicate_keys_and_symlinks_fail_closed(self):
        pointer = self.legacy / 'readonly-credentials.json'
        for raw in ['{"version":1,"store":"../gog"}',
                    '{"version":1,"version":1,"store":"recovery-' + 'a' * 24 + '"}']:
            pointer.write_text(raw)
            pointer.chmod(0o600)
            with self.assertRaises(ValueError): credential_root(self.legacy)
        pointer.unlink()
        pointer.symlink_to(self.legacy / 'gmail-account.pending')
        with self.assertRaises(ValueError): credential_root(self.legacy)

    def test_new_password_mismatch_starts_no_oauth_and_writes_no_store(self):
        with (patch('mail_reconnect.hidden', side_effect=['new-password-long-enough', 'different']),
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(run_reconnect(self.legacy, self.fixture.config, self.state_dir), 1)
        self.assertFalse((self.legacy / 'readonly-stores').exists())
        self.assertFalse(self.state_dir.exists())

    def test_terminal_no_longer_reprompts_after_integrity_failure(self):
        state = self.make_state(self.legacy, self.fixture.config, self.state_dir)
        self.fixture.auth['accounts'][0]['error'] = 'aes.KeyUnwrap: integrity check failed'
        with (patch('mail_readonly.getpass.getpass', return_value=self.fixture.password) as prompt,
              patch('mail_readonly.sys.stdin.isatty', return_value=True),
              patch('mail_readonly.sys.stdout.isatty', return_value=True),
              patch('mail_readonly.sys.stderr.isatty', return_value=True),
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
            # redirect streams are StringIO; patch their isatty after redirect.
            with (patch('mail_readonly.sys.stdout.isatty', return_value=True),
                  patch('mail_readonly.sys.stderr.isatty', return_value=True)):
                self.assertEqual(run_terminal(state), 1)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_private_terminal_hides_both_new_password_and_oauth_callback(self):
        pid, master = pty.fork()
        if pid == 0:
            try:
                with (patch('mail_reconnect.MailSession', side_effect=self.make_state),
                      patch('mail_reconnect.RecoveryConsent', side_effect=lambda root, **kw: RecoveryConsent(
                          root, runner=self.oauth_runner, **kw))):
                    rc = run_reconnect(self.legacy, self.fixture.config, self.state_dir)
                import sys
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(rc)
            except BaseException:
                os._exit(99)
        captured = b''
        pending = [(b'Nueva contrase', self.fixture.password),
                   (b'Repet', self.fixture.password),
                   (b'URL final de localhost (no se muestra): ', self.redirect)]
        deadline = time.monotonic() + 8
        cursor = 0
        try:
            while time.monotonic() < deadline:
                if not select.select([master], [], [], 0.1)[0]:
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                captured += chunk
                if pending and pending[0][0] in captured[cursor:]:
                    # Prompts finish with ': '; do not type while getpass is
                    # still preparing the terminal's echo-disabled state.
                    if not captured.endswith(b': '):
                        continue
                    _, response = pending.pop(0)
                    cursor = len(captured)
                    os.write(master, (response + '\n').encode())
            else:
                os.kill(pid, 9)
                self.fail('reconnect terminal did not complete')
        finally:
            os.close(master)
            _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        output = captured.decode().replace('\r\n', '\n')
        for secret in [self.fixture.password, self.redirect, 'HIDDEN_CODE', 'PRIVATE_OAUTH_HINT']:
            self.assertNotIn(secret, output)
        self.assertIn(self.fixture.analysis['summary'], output)

    def test_selected_store_cannot_switch_account_and_no_fallback_on_bad_selection(self):
        self.assertEqual(self.run_flow()[0], 0)
        root = credential_root(self.legacy)
        (root / 'gmail-account.pending').write_text('other@example.invalid')
        with self.assertRaises(ValueError): credential_root(self.legacy)


if __name__ == '__main__':
    unittest.main()
