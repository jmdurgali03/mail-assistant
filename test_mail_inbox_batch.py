import contextlib
import copy
import io
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gmail_consent import ConsentError
from mail_inbox_batch import InboxBatchSession, QUERY, handoff, run_terminal, main
import test_inbox_readonly
from test_jev_decisions import FakeDecider, summary_fixture


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.f = test_inbox_readonly.InboxProofTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        summary_fixture(self.f)
        self.pages = {'': {'messages': [self.row('abc1')], 'nextPageToken': None}}
        self.messages = {mid: {**copy.deepcopy(self.f.message), 'id': mid} for mid in ('abc1', 'abc2', 'abc3')}
        self.events, self.deciders = [], []
        self.fail_decider = False
        self.state = InboxBatchSession(self.f.runtime, self.f.config, self.f.home / 'batch',
                                      runner=self.run_fake, clock=lambda: self.f.now,
                                      decider_factory=self.decider, wall_clock=lambda: 1000)

    def row(self, mid, labels=None, **kwargs):
        return {'id': mid, 'labels': labels if labels is not None else ['INBOX'],
                'subject': 'Proyecto', 'from': 'Persona sintética', **kwargs}

    def decider(self):
        value = FakeDecider(error='timeout' if self.fail_decider else None)
        original = value.decide
        def decide(prepared):
            self.events.append('jev')
            return original(prepared)
        value.decide = decide
        self.deciders.append(value)
        return value

    def run_fake(self, args, **kwargs):
        if 'agent' in args:
            self.events.append('prepare' if kwargs['input'].startswith('ETAPA_PREPARAR_GPT') else 'compose')
            return self.f.run_fake(args, **kwargs)
        command = args[args.index('--enable-commands-exact') + 1]
        if command == 'auth.list':
            self.events.append('auth')
            return self.f.run_fake(args, **kwargs)
        self.f.calls.append((list(args), {**kwargs, 'env': dict(kwargs['env'])}))
        self.assertIn('--readonly', args)
        self.assertIn('--gmail-no-send', args)
        self.assertEqual(kwargs['env']['GOG_KEYRING_PASSWORD'], self.f.password)
        self.assertNotIn(self.f.password, str(args))
        if command == 'gmail.messages.search':
            self.events.append('page')
            self.assertEqual(args[args.index('search') + 1], QUERY)
            self.assertNotIn('unread', QUERY)
            self.assertNotIn('--include-body', args)
            self.assertNotIn('--all', args)
            token = next((arg.split('=', 1)[1] for arg in args if arg.startswith('--page=')), '')
            value = self.pages[token]
        elif command == 'gmail.get':
            self.events.append('body')
            self.assertEqual(args[-3:], ['--format', 'full', '--sanitize-content'])
            value = {'message': self.messages[args[args.index('get') + 1]]}
        else:
            self.fail('mutation, delivery, attachment or unknown command')
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(value))

    def execute(self):
        form = {'password': [self.f.password]}
        try:
            self.state.execute(form)
        finally:
            self.assertEqual(form, {})

    def records(self):
        return [json.loads(line) for line in (self.state.state_dir / 'analysis.jsonl').read_text().splitlines()]

    def counts(self, **expected):
        for key, value in expected.items():
            self.assertEqual(self.state.receipt[key], value, key)
        for key in ('mail_mutation_invocations', 'attachment_fetch_invocations', 'delivery_invocations'):
            self.assertEqual(self.state.receipt[key], 0)

    def test_all_pages_dedupe_read_and_unread_sent_inbox_exact_order_and_counts(self):
        self.pages = {'': {'messages': [self.row('abc1', ['INBOX', 'UNREAD']), self.row('abc2')],
                           'nextPageToken': 'PRIVATE_PAGE_TOKEN'},
                      'PRIVATE_PAGE_TOKEN': {'messages': [self.row('abc2'), self.row('abc3', ['INBOX', 'SENT'])]}}
        self.messages['abc3']['labelIds'] = ['INBOX', 'SENT']
        self.execute()
        self.assertEqual(self.events, ['auth', 'auth', 'page', 'page'] + ['body', 'prepare', 'jev', 'compose'] * 3)
        self.counts(snapshot_unique=3, snapshot_rows=4, snapshot_duplicates=1, processed=3, analyzed=3,
                    metadata_search_invocations=2, body_get_invocations=3, model_invocations=6, jev_invocations=3)
        self.assertTrue(self.state.receipt['functional_passed'])
        self.assertEqual([r['message_id'] for r in self.records()], ['abc1', 'abc2', 'abc3'])
        for record in self.records():
            self.assertEqual(record['analysis']['priority'], 'media')
            self.assertEqual(record['analysis']['summary'], self.f.analysis['summary'])
        before = len(self.f.calls)
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(before, len(self.f.calls))
        self.assertEqual(len(self.records()), 3)

    def test_empty_inbox_is_complete_without_body_or_providers(self):
        self.pages['']['messages'] = []
        self.execute()
        self.counts(snapshot_unique=0, processed=0, body_get_invocations=0, model_invocations=0, jev_invocations=0)
        self.assertTrue(self.state.receipt['batch_complete'])
        self.assertTrue(self.state.receipt['functional_passed'])
        self.assertEqual(self.records(), [])

    def test_social_excluded_in_query_and_unexpected_listing_before_body(self):
        self.assertIn('-category:social', QUERY)
        self.pages['']['messages'] = [self.row('abc2', ['INBOX', 'CATEGORY_SOCIAL']), self.row('abc1')]
        self.execute()
        self.assertEqual([r['message_id'] for r in self.records()], ['abc1'])
        self.counts(body_get_invocations=1, model_invocations=2, jev_invocations=1)

    def test_direct_social_selection_rejected_before_body_or_models(self):
        from mail_inbox_batch import InboxItemSession
        item = InboxItemSession(self.f.runtime, self.f.config, self.f.home / 'direct-promo',
                                runner=lambda *args, **kwargs: self.fail('no transport for Social'),
                                decider=FakeDecider())
        with self.assertRaises(ValueError):
            item.read_selected(self.row('abc2', ['INBOX', 'CATEGORY_SOCIAL']), self.f.password,
                               'synthetic@example.invalid')
        self.assertEqual(item.receipt['body_get_invocations'], 0)
        self.assertEqual(item.receipt['model_invocations'], 0)

    def test_promotions_excluded_in_query_and_unexpected_listing_before_body(self):
        self.assertIn('-category:promotions', QUERY)
        self.pages['']['messages'] = [self.row('abc2', ['INBOX', 'CATEGORY_PROMOTIONS']), self.row('abc1')]
        self.execute()
        self.assertEqual([r['message_id'] for r in self.records()], ['abc1'])
        self.counts(body_get_invocations=1, model_invocations=2, jev_invocations=1)

    def test_direct_promotion_selection_rejected_before_body_or_models(self):
        from mail_inbox_batch import InboxItemSession
        item = InboxItemSession(self.f.runtime, self.f.config, self.f.home / 'direct-promo',
                                runner=lambda *args, **kwargs: self.fail('no transport for Promotions'),
                                decider=FakeDecider())
        with self.assertRaises(ValueError):
            item.read_selected(self.row('abc2', ['INBOX', 'CATEGORY_PROMOTIONS']), self.f.password,
                               'synthetic@example.invalid')
        self.assertEqual(item.receipt['body_get_invocations'], 0)
        self.assertEqual(item.receipt['model_invocations'], 0)

    def test_terminal_null_page_after_300_rows_is_complete_before_analysis(self):
        # Synthetic Go-style nil slice, not a captured mailbox response. Headers
        # protect these 300 messages so this test cannot invoke either provider.
        for number, token in enumerate(('', 'PAGE_TWO', 'PAGE_THREE')):
            self.pages[token] = {
                'messages': [self.row(f'{number * 100 + i + 1:x}', subject='Código de acceso OTP')
                             for i in range(100)],
                'nextPageToken': ('PAGE_TWO', 'PAGE_THREE', 'PAGE_FOUR')[number]}
        self.pages['PAGE_FOUR'] = {'messages': None, 'nextPageToken': ''}
        self.execute()
        self.counts(metadata_search_invocations=4, snapshot_rows=300, snapshot_unique=300,
                    processed=300, authentication_protected_count=300,
                    body_get_invocations=0, model_invocations=0, jev_invocations=0)
        self.assertTrue(self.state.receipt['snapshot_complete'])
        self.assertTrue(self.state.receipt['batch_complete'])
        self.assertEqual(self.events, ['auth', 'auth'] + ['page'] * 4)

    def test_explicit_terminal_null_empty_inbox(self):
        self.pages[''] = {'messages': None, 'nextPageToken': None}
        self.execute()
        self.counts(snapshot_unique=0, processed=0, body_get_invocations=0,
                    model_invocations=0, jev_invocations=0)
        self.assertTrue(self.state.receipt['snapshot_complete'])
        self.assertEqual(self.records(), [])

    def test_failed_page_never_analyzes_incomplete_snapshot_or_retries(self):
        self.pages['']['nextPageToken'] = 'PRIVATE_PAGE_TOKEN'
        self.pages['PRIVATE_PAGE_TOKEN'] = subprocess.TimeoutExpired('PRIVATE_COMMAND', 45)
        with self.assertRaises(ConsentError): self.execute()
        self.counts(metadata_search_invocations=2, snapshot_unique=1, processed=0, body_get_invocations=0)
        self.assertFalse(self.state.receipt['snapshot_complete'])
        self.assertFalse((self.state.state_dir / 'analysis.jsonl').exists())
        with self.assertRaises(ConsentError): self.execute()
        self.counts(metadata_search_invocations=2)

    def test_repeated_page_token_stops_before_any_body(self):
        self.pages['']['nextPageToken'] = 'PRIVATE_TOKEN'
        self.pages['PRIVATE_TOKEN'] = {'messages': [], 'nextPageToken': 'PRIVATE_TOKEN'}
        with self.assertRaises(ConsentError): self.execute()
        self.counts(metadata_search_invocations=2, body_get_invocations=0)

    def test_opaque_token_is_bound_as_a_value_not_an_option(self):
        self.pages['']['nextPageToken'] = '--include-body'
        self.pages['--include-body'] = {'messages': []}
        self.execute()
        command = next(args for args, _ in self.f.calls if '--page=--include-body' in args)
        self.assertNotIn('--include-body', command)

    def test_completed_item_receipt_failure_discards_summary(self):
        from mail_inbox_batch import InboxItemSession
        original = InboxItemSession._phase
        failed = False
        def phase(item, value):
            nonlocal failed
            if value == 'completed' and item is not self.state and not failed:
                failed = True
                raise OSError('PRIVATE_FILESYSTEM_FAILURE')
            return original(item, value)
        with patch.object(InboxItemSession, '_phase', phase):
            self.execute()
        self.counts(needs_review_count=1, analyzed=0)
        self.assertNotIn('analysis', self.records()[0])

    def test_malformed_page_and_candidate_fail_closed(self):
        self.pages['']['messages'] = [self.row('abc1', body='UNEXPECTED_BODY')]
        with self.assertRaises(ConsentError): self.execute()
        self.counts(body_get_invocations=0, model_invocations=0)

    def test_partial_body_error_continues_with_no_substitution_or_retry(self):
        self.pages['']['messages'] = [self.row('abc1'), self.row('abc2')]
        self.messages['abc1']['labelIds'] = ['SENT']  # Left Inbox after snapshot.
        self.execute()
        self.counts(snapshot_unique=2, processed=2, analyzed=1, needs_review_count=1,
                    body_get_invocations=2, model_invocations=2, jev_invocations=1)
        self.assertEqual(self.state.phase, 'completed_with_review')
        self.assertFalse(self.state.receipt['functional_passed'])
        self.assertNotIn('analysis', self.records()[0])
        self.assertEqual(self.records()[1]['status'], 'ok')

    def test_body_timeout_is_one_attempt_then_next_message(self):
        self.pages['']['messages'] = [self.row('abc1'), self.row('abc2')]
        self.messages['abc1'] = subprocess.TimeoutExpired('PRIVATE_ERROR', 45)
        self.execute()
        self.counts(body_get_invocations=2, needs_review_count=1, analyzed=1)
        self.assertEqual(self.events.count('body'), 2)

    def test_authentication_headers_skip_body_and_both_providers(self):
        self.pages['']['messages'] = [self.row('abc1', subject='Código de acceso OTP')]
        self.execute()
        self.counts(authentication_protected_count=1, body_get_invocations=0,
                    model_invocations=0, jev_invocations=0, analyzed=0)
        self.assertEqual(self.records()[0]['status'], 'authentication_protected')

    def test_authentication_body_never_leaks_otp_or_reaches_providers(self):
        self.messages['abc1']['body'] = 'Tu código de acceso OTP es 123456'
        self.execute()
        self.counts(authentication_protected_count=1, body_get_invocations=1,
                    model_invocations=0, jev_invocations=0)
        self.assertNotIn('123456', (self.state.state_dir / 'analysis.jsonl').read_text())

    def test_invalid_preparation_skips_jev_and_final_gpt_without_retry(self):
        self.f.preparation['priority'] = 'alta'
        self.execute()
        self.counts(needs_review_count=1, model_invocations=1, jev_invocations=0)
        self.assertEqual(self.records()[0]['failure'], 'preparation_needs_review')
        self.assertNotIn('analysis', self.records()[0])

    def test_jev_failure_has_no_fallback_or_composition(self):
        self.fail_decider = True
        self.execute()
        self.counts(needs_review_count=1, model_invocations=1, jev_invocations=1)
        self.assertEqual(self.records()[0]['failure'], 'decision_needs_review')

    def test_altered_jev_decision_or_unbacked_final_quote_rejected(self):
        self.f.rendering['decisions']['priority'] = 'alta'
        self.f.envelope['final'] = json.dumps(self.f.rendering)
        self.execute()
        self.counts(needs_review_count=1, model_invocations=2, jev_invocations=1)
        self.assertEqual(self.records()[0]['failure'], 'analysis_needs_review')

    def test_invented_final_quote_is_not_persisted_as_a_summary(self):
        self.f.rendering['sentences'][0]['text'] = 'INVENTED_PRIVATE_ASSERTION'
        self.f.envelope['final'] = json.dumps(self.f.rendering)
        self.execute()
        self.counts(needs_review_count=1, model_invocations=2, jev_invocations=1)
        self.assertNotIn('INVENTED_PRIVATE_ASSERTION', (self.state.state_dir / 'analysis.jsonl').read_text())

    def test_expiry_within_one_item_does_not_retry_or_prevent_next_item(self):
        self.pages['']['messages'] = [self.row('abc1'), self.row('abc2')]
        original = self.state.runner
        def runner(args, **kwargs):
            result = original(args, **kwargs)
            if 'agent' in args and self.f.now == 0:
                self.f.now = 601
            return result
        self.state.runner = runner
        self.execute()
        self.counts(needs_review_count=1, analyzed=1, model_invocations=3, jev_invocations=1,
                    body_get_invocations=2)

    def test_stage_timeout_does_not_repeat_gpt_or_mail(self):
        original = self.state.runner
        def runner(args, **kwargs):
            if 'agent' in args:
                raise subprocess.TimeoutExpired('PRIVATE_ERROR', 150)
            return original(args, **kwargs)
        self.state.runner = runner
        self.execute()
        self.counts(needs_review_count=1, model_invocations=1, jev_invocations=0,
                    body_get_invocations=1)

    def test_stderr_review_preserved_across_items(self):
        self.execute()
        self.assertTrue(self.state.receipt['diagnostic_review_required'])
        self.assertNotIn(self.f.stderr, (self.state.state_dir / 'result.json').read_text())

    def test_receipts_are_content_free_and_handoff_is_minimized_and_private(self):
        self.execute()
        for path in self.state.state_dir.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
            if path.is_file() and path.name != 'analysis.jsonl':
                data = path.read_text()
                for marker in ('abc1', self.f.private_marker, self.f.password, self.f.analysis['summary'],
                               'synthetic@example.invalid', self.f.stderr):
                    self.assertNotIn(marker, data)
        payload = (self.state.state_dir / 'analysis.jsonl').read_text()
        for marker in (self.f.private_marker, self.f.password, 'PRIVATE_RECIPIENT_NOT_PROPAGATED',
                       'NOT_DOWNLOADED.pdf', 'Persona sintética'):
            self.assertNotIn(marker, payload)
        self.assertEqual(self.state.receipt['output_expires_at'], 87400)

    def test_failed_unlock_does_not_query_mail_or_providers(self):
        self.f.auth['accounts'][0]['error'] = 'aes.KeyUnwrap: integrity check failed'
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(self.events, ['auth'])
        self.counts(metadata_search_invocations=0, body_get_invocations=0)

    def test_preflight_only_no_network_or_claim(self):
        self.state.local_preflight()
        self.assertEqual(self.events, [])
        self.assertFalse(self.state.state_dir.exists())

    def test_expired_before_auth_no_calls(self):
        self.f.now = 601
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(self.events, [])

    def test_handoff_access_expiration_purge_and_symlink_rejection(self):
        self.execute()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(handoff(self.state.state_dir, 'show', now=lambda: 1001), 0)
        self.assertEqual(json.loads(stdout.getvalue())['status'], 'ok')
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(handoff(self.state.state_dir, 'show', now=lambda: 87400), 1)
        self.assertFalse((self.state.state_dir / 'analysis.jsonl').exists())
        target = self.f.home / 'private-target'
        target.write_text('DO_NOT_READ')
        target.chmod(0o600)
        (self.state.state_dir / 'analysis.jsonl').symlink_to(target)
        with self.assertRaises(ValueError): handoff(self.state.state_dir, 'show')
        self.assertEqual(target.read_text(), 'DO_NOT_READ')

    def test_explicit_purge_preserves_receipts(self):
        self.execute()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(handoff(self.state.state_dir, 'purge'), 0)
        self.assertFalse((self.state.state_dir / 'analysis.jsonl').exists())
        self.assertTrue((self.state.state_dir / 'result.json').exists())

    def test_noninteractive_terminal_never_prompts_or_executes(self):
        with contextlib.redirect_stderr(io.StringIO()), patch('mail_inbox_batch.getpass.getpass') as prompt:
            self.assertEqual(run_terminal(self.state), 2)
        prompt.assert_not_called()
        self.assertEqual(self.events, [])

    def test_cli_blocks_live_before_password_until_transport_scope_resolved(self):
        with patch('sys.argv', ['mail_inbox_batch.py', 'run', '--session', 'inbox-' + 'a' * 24]), \
             patch('mail_inbox_batch.InboxBatchSession', return_value=self.state), \
             patch('mail_inbox_batch.credential_root', return_value=self.f.runtime), \
             patch('mail_inbox_batch.__file__', str(self.f.home / 'mail_inbox_batch.py')), \
             patch('mail_inbox_batch.getpass.getpass') as prompt, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 2)
        prompt.assert_not_called()
        self.assertEqual(self.events, [])
        self.assertFalse(self.state.state_dir.exists())

    def test_real_getpass_pty_unlock_once_without_echo_or_private_terminal_output(self):
        pid, master = pty.fork()
        if pid == 0:
            try:
                rc = run_terminal(self.state)
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
                    os.write(master, (self.f.password + '\n').encode())
                    sent = True
            else:
                os.kill(pid, 9)
                self.fail('terminal did not complete')
        finally:
            os.close(master)
            _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        output = captured.decode()
        for marker in (self.f.password, self.f.private_marker, self.f.stderr, self.f.analysis['summary']):
            self.assertNotIn(marker, output)
        self.assertEqual(output.count('Contraseña del almacén (no se muestra): '), 1)
        self.assertIn('Directorio privado para Bichi:', output)
        self.assertEqual(self.records()[0]['status'], 'ok')
        receipt = json.loads((self.state.state_dir / 'result.json').read_text())
        self.assertEqual(receipt['auth_local_invocations'], 1)
        self.assertEqual(receipt['model_invocations'], 2)


if __name__ == '__main__':
    unittest.main()
