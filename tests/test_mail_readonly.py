import contextlib
import io
import json
import os
import pty
import select
import time
import unittest
from unittest.mock import patch

from gmail_consent import ConsentError
from mail_readonly import MailSession, auth_error_category, run_terminal, MESSAGES
import test_inbox_readonly
from test_jev_decisions import FakeDecider, summary_fixture


class MailSessionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_inbox_readonly.InboxProofTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        summary_fixture(f)
        self.decider = FakeDecider()
        self.state = MailSession(f.runtime, f.config, f.home / 'session',
                                 runner=f.run_fake, clock=lambda: f.now, decider=self.decider)

    def execute(self):
        form = {'password': [self.fixture.password]}
        try:
            self.state.execute(form)
        finally:
            self.assertEqual(form, {})

    def test_unlock_continues_directly_to_real_reader_and_analyzer_interfaces(self):
        self.execute()
        self.assertTrue(self.state.saved)
        self.assertEqual(self.state.phase, 'completed')
        self.assertEqual(self.state.analysis['summary'], self.fixture.analysis['summary'])
        self.assertEqual(len(self.fixture.calls), 6)
        self.assertTrue(self.state.receipt['analysis_available'])
        self.assertEqual(self.state.receipt['decision']['status'], 'ok')
        self.assertEqual(self.state.receipt['decision']['model'], 'jev-1.13.0')
        self.assertEqual(self.state.receipt['summary_provider'], 'openai')
        artifacts = ''.join(p.read_text() for p in self.state.state_dir.iterdir())
        for marker in (self.fixture.password, self.fixture.private_marker, self.fixture.stderr,
                       self.fixture.analysis['summary'], 'synthetic@example.invalid'):
            self.assertNotIn(marker, artifacts)

    def test_jev_failure_is_needs_review_without_gpt_fallback_mail_retry_or_private_leak(self):
        self.decider.error = 'timeout'
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(self.state.phase, 'needs_review')
        self.assertTrue(self.state.receipt['needs_review'])
        self.assertEqual(self.state.receipt['model_invocations'], 1)
        self.assertEqual(self.state.receipt['body_get_invocations'], 1)
        self.assertIsNone(self.state.analysis)
        before = len(self.fixture.calls)
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(len(self.fixture.calls), before)
        self.assertEqual(self.decider.metadata['invocations'], 1)

    def test_gpt_cannot_override_jev_classifications(self):
        self.fixture.envelope['final'] = json.dumps(self.fixture.analysis)
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(self.state.phase, 'needs_review')
        self.assertFalse(self.state.receipt['functional_passed'])
        self.assertEqual(self.decider.metadata['invocations'], 1)

    def test_financial_jev_flag_drives_extraction_and_evidence_validation(self):
        original = self.decider.decide
        def financial(prepared):
            return {**original(prepared), 'financial': True}
        self.decider.decide = financial
        fields = {key: None for key in ('account', 'amount', 'currency', 'date', 'category')}
        self.fixture.rendering['decisions']['financial'] = True
        self.fixture.envelope['final'] = json.dumps(self.fixture.rendering)
        self.execute()
        self.assertTrue(self.state.analysis['financial'])
        self.assertEqual(self.state.analysis['priority'], 'media')
        prompt = self.fixture.calls[-1][1]['input']
        self.assertIn('NO el clasificador', prompt)
        self.assertIn('DECISIONES_JEV', prompt)

    def test_gpt_invented_financial_evidence_requires_review(self):
        original = self.decider.decide
        self.decider.decide = lambda prepared: {**original(prepared), 'financial': True}
        fields = {key: None for key in ('account', 'amount', 'currency', 'date', 'category')}
        fields['amount'] = {'value': '123.45', 'evidence': 'INVENTED_EVIDENCE'}
        self.fixture.preparation['financial_data'] = fields
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(self.state.phase, 'needs_review')
        self.assertIsNone(self.state.analysis)

    def test_authentication_protection_never_calls_jev_or_gpt(self):
        self.fixture.listing['messages'][0]['subject'] = 'Código de acceso OTP'
        self.execute()
        self.assertEqual(self.decider.metadata['invocations'], 0)
        self.assertEqual(self.state.receipt['model_invocations'], 0)
        self.assertTrue(self.state.receipt['authentication_protected'])

    def test_private_keyunwrap_error_classified_without_persisting_it_and_no_mail(self):
        self.fixture.auth['accounts'][0]['error'] = 'read token PRIVATE: aes.KeyUnwrap(): integrity check failed'
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(self.state.failure, 'keyring_password_mismatch_or_corrupt_entry')
        self.assertFalse(self.state.attempted)
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertNotIn('PRIVATE', (self.state.state_dir / 'result.json').read_text())
        self.assertIn('No repitas', MESSAGES[self.state.failure])

    def test_human_unlock_correction_retries_only_auth_and_then_continues(self):
        self.fixture.auth['accounts'][0]['error'] = 'aes.KeyUnwrap: integrity check failed'
        with self.assertRaises(ConsentError): self.execute()
        del self.fixture.auth['accounts'][0]['error']
        self.execute()
        self.assertTrue(self.state.saved)
        self.assertEqual(self.state.auth_attempts, 2)
        self.assertEqual(len(self.fixture.calls), 7)
        self.assertEqual(self.state.receipt['body_get_invocations'], 1)
        self.assertEqual(len(self.state.receipt['auth_attempts']), 2)

    def test_failed_model_never_replays_mail_on_another_post(self):
        self.fixture.model_rc = 1
        with self.assertRaises(ConsentError): self.execute()
        with self.assertRaises(ConsentError): self.execute()
        self.assertTrue(self.state.attempted)
        self.assertEqual(len(self.fixture.calls), 5)

    def test_completed_session_rejects_repeat_without_erasing_success(self):
        self.execute()
        before = (self.state.state_dir / 'result.json').read_bytes()
        with self.assertRaises(ConsentError): self.execute()
        self.assertTrue(self.state.saved)
        self.assertEqual((self.state.state_dir / 'result.json').read_bytes(), before)
        self.assertEqual(len(self.fixture.calls), 6)

    def test_local_scope_rejection_stops_before_refresh_or_mail(self):
        self.fixture.auth['accounts'][0]['scopes'].append('https://mail.google.com/')
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertFalse(self.state.receipt['analysis_available'])

    def test_refresh_failure_classified_and_no_mail(self):
        original = self.state.runner
        def runner(args, **kwargs):
            if '--check' in args:
                self.fixture.auth['accounts'][0].update(valid=False, error='invalid_grant: PRIVATE')
            return original(args, **kwargs)
        self.state.runner = runner
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(self.state.failure, 'oauth_grant_expired_or_revoked')
        self.assertEqual(len(self.fixture.calls), 2)
        self.assertNotIn('PRIVATE', (self.state.state_dir / 'result.json').read_text())

    def test_password_retry_limit_and_expiration(self):
        self.fixture.auth['accounts'][0]['error'] = 'aes.KeyUnwrap: integrity check failed'
        for _ in range(4):
            with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(len(self.fixture.calls), 3)
        self.fixture.now = 601
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(len(self.fixture.calls), 3)

    def test_error_categories_are_closed_and_never_return_private_text(self):
        cases = {'PRIVATE': 'unclassified', 'no TTY: GOG_KEYRING_PASSWORD': 'keyring_unlock_required',
                 'invalid_rapt PRIVATE': 'google_reauthentication_required',
                 'permission denied PRIVATE': 'keyring_permission_denied',
                 'deadline exceeded PRIVATE': 'auth_timeout'}
        for message, category in cases.items():
            self.assertEqual(auth_error_category(message), category)

    def test_terminal_rejects_non_tty_before_requesting_password_or_running_commands(self):
        with contextlib.redirect_stderr(io.StringIO()), patch('mail_readonly.getpass.getpass') as prompt:
            self.assertEqual(run_terminal(self.state), 2)
        prompt.assert_not_called()
        self.assertFalse(self.fixture.calls)

    def test_actual_getpass_pty_does_not_echo_password_and_returns_analysis(self):
        # Real terminal/getpass, fixture CLI/model only; no live credentials/network.
        pid, master = pty.fork()
        if pid == 0:
            try:
                rc = run_terminal(self.state)
                import sys
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(rc)
            except BaseException:
                os._exit(99)
        captured = b''
        sent = False
        deadline = time.monotonic() + 8
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
                if not sent and b'(no se muestra): ' in captured:
                    os.write(master, (self.fixture.password + '\n').encode())
                    sent = True
            else:
                os.kill(pid, 9)
                self.fail('terminal did not complete')
        finally:
            os.close(master)
            _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        output = captured.decode().replace('\r\n', '\n')
        self.assertNotIn(self.fixture.password, output)
        self.assertNotIn(self.fixture.private_marker, output)
        self.assertNotIn(self.fixture.stderr, output)
        self.assertIn(self.fixture.analysis['summary'], output)


if __name__ == '__main__':
    unittest.main()
