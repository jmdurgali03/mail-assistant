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
import time
from types import SimpleNamespace
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from gmail_consent import ConsentError, READONLY, ThreadingHTTPServer
from oneshot import BODY, SUBJECT, FIXTURE_QUERY, Ledger, OneShot, load_plan, oneshot_handler
import oneshot


class OneShotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir(mode=0o700)
        (self.runtime / 'gog').mkdir(mode=0o700)
        pending = self.runtime / 'gmail-account.pending'
        pending.write_text('synthetic@example.invalid')
        pending.chmod(0o600)
        self.state_dir = self.root / 'ledger'
        self.state_dir.mkdir(mode=0o700)
        self.config = self.root / 'analysis.json'
        template = Path('examples/analysis-config.owner.example.json').read_text()
        self.config.write_text(template.replace('/home/REPLACE_USER', str(Path.home())))
        self.config.chmod(0o600)
        self.plan = {'account': 'synthetic@example.invalid', 'message_id': 'abc1',
                     'telegram_account_id': 'bichi', 'telegram_chat_id': '123',
                     'telegram_user_id': '123', 'analysis_config': str(self.config),
                     'state_dir': str(self.state_dir), 'expires_at': int(time.time()) + 1200,
                     'recipient_confirmation': {'by': 'operator', 'channel': 'telegram',
                                                'chat_type': 'private', 'at_unix': int(time.time())}}
        self.plan_path = self.root / 'plan.json'
        self.calls = []
        self.password = 'synthetic-keyring-password'
        self.analysis = {'priority': None, 'topic': None, 'summary': 'Prueba segura recibida.',
                         'promotional': False, 'financial': False, 'financial_data': None}
        self.msg = {'id': 'abc1', 'labelIds': ['INBOX'], 'attachments': [],
                    'headers': {'subject': SUBJECT, 'from': 'PRIVATE_SENDER_NOT_PROPAGATED'},
                    'body': BODY}
        self.auth = {'accounts': [{'email': self.plan['account'], 'client': 'default',
                                  'auth': 'oauth', 'services': ['gmail'], 'scopes': [READONLY],
                                  'valid': True}]}
        self.model_fail = self.send_timeout = False
        self.receipt_chat = '123'
        self.search = {'messages': [{'id': 'abc1', 'subject': SUBJECT,
                                    'from': 'PRIVATE_SEARCH_METADATA'}], 'nextPageToken': ''}
        self.state = OneShot(self.plan, self.runtime, runner=self.runner)

    def runner(self, args, **kwargs):
        self.calls.append(list(args))
        if 'GOG_KEYRING_PASSWORD' in kwargs.get('env', {}):
            self.assertNotIn(self.password, args)
        if 'agent' in args:
            self.assertNotIn('GOG_KEYRING_PASSWORD', kwargs['env'])
            self.assertEqual(args[:3], ['openclaw', '--log-level', 'silent'])
            self.assertIn('--profile', args)
            self.assertEqual(args[args.index('--profile') + 1], 'bichi-mail-analysis')
            self.assertNotIn('PRIVATE_SENDER_NOT_PROPAGATED', kwargs['input'])
            self.assertNotIn('PRIVATE_SEARCH_METADATA', kwargs['input'])
            value = {'ok': not self.model_fail, 'status': 'error' if self.model_fail else 'ok',
                     'provider': 'openai', 'model': 'gpt-6.1-sol',
                     'toolSummary': {'calls': 0, 'tools': []}, 'final': json.dumps(self.analysis)}
        elif 'auth' in args:
            value = self.auth
        elif 'search' in args:
            self.assertEqual(args[-6:], ['gmail', 'messages', 'search', FIXTURE_QUERY, '--max', '2'])
            self.assertEqual(args[args.index('--enable-commands-exact') + 1], 'gmail.messages.search')
            self.assertEqual(kwargs['env']['GOG_KEYRING_PASSWORD'], self.password)
            for flag in ('--home', '--client', '--account', '--readonly', '--gmail-no-send'):
                self.assertIn(flag, args)
            value = self.search
        elif 'get' in args:
            self.assertEqual(args[-6:], ['gmail', 'get', 'abc1', '--format', 'full', '--sanitize-content'])
            for flag in ('--home', '--client', '--account', '--readonly', '--gmail-no-send'):
                self.assertIn(flag, args)
            self.assertEqual(args[args.index('--enable-commands-exact') + 1], 'gmail.get')
            value = {'message': self.msg}
        else:
            self.assertEqual(args[:3], ['openclaw', 'message', 'send'])
            self.assertNotIn('GOG_KEYRING_PASSWORD', kwargs['env'])
            self.assertEqual(args[args.index('--target') + 1], '123')
            self.assertTrue(self.state.ledger.path.exists())
            self.assertEqual(json.loads(self.state.ledger.path.read_text())['state'], 'inflight')
            if self.send_timeout:
                raise subprocess.TimeoutExpired(args, 60)
            value = {'action': 'send', 'channel': 'telegram', 'dryRun': False,
                     'payload': {'deliveryStatus': 'sent', 'result': {'messageId': '501',
                                                   'target': {'kind': 'chat', 'id': self.receipt_chat}}}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr='')

    def execute(self):
        form = {'password': [self.password]}
        try:
            self.state.execute(form)
        finally:
            self.assertEqual(form, {})

    def write_plan(self):
        self.plan_path.write_text(json.dumps(self.plan))
        self.plan_path.chmod(0o600)

    def load_plan(self):
        # Model a temporary checkout so the project-only state rule stays isolated.
        with patch.object(oneshot, '__file__', str(self.root / 'oneshot.py')):
            return load_plan(self.plan_path)

    def test_valid_plan_and_destination_confirmation_required(self):
        self.write_plan()
        self.state_dir.rename(self.root / '.oneshot-state')
        self.state_dir = self.root / '.oneshot-state'
        self.plan['state_dir'] = str(self.state_dir)
        self.write_plan()
        self.assertEqual(self.load_plan(), self.plan)
        for key, value in [('telegram_chat_id', '-123'), ('telegram_account_id', 'albert'),
                           ('telegram_chat_id', '456'), ('message_id', 'x;bad'),
                           ('recipient_confirmation', {}), ('expires_at', 0)]:
            with self.subTest(key=key):
                changed = dict(self.plan, **{key: value})
                self.plan_path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError): self.load_plan()
        self.write_plan()
        self.plan_path.chmod(0o644)
        with self.assertRaises(ValueError): self.load_plan()

    def test_owner_preflight_failure_stops_before_auth_or_gmail(self):
        self.model_fail = True
        with self.assertRaises(RuntimeError): self.state.preflight()
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(list(self.state_dir.iterdir()))
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(len(self.calls), 1)

    def model_envelope(self):
        return {'ok': True, 'status': 'ok', 'provider': 'openai', 'model': 'gpt-6.1-sol',
                'toolSummary': {'calls': 0, 'tools': []}, 'final': json.dumps(self.analysis)}

    def preflight_envelope(self, envelope, *, stderr='', returncode=0):
        self.calls.clear()
        def runner(args, **kwargs):
            result = self.runner(args, **kwargs)
            result.stdout, result.stderr = json.dumps(envelope), stderr
            result.returncode = returncode
            return result
        self.state = OneShot(self.plan, self.runtime, runner=runner)
        self.state.preflight()

    def assert_model_blocked(self, envelope, **kwargs):
        with self.assertRaisesRegex(RuntimeError, '^analysis failed; review pending$'):
            self.preflight_envelope(envelope, **kwargs)
        self.assertFalse(self.state.preflight_passed)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('agent', self.calls[0])
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(len(self.calls), 1)  # no auth, Gmail or Telegram after failure
        self.assertFalse(list(self.state_dir.iterdir()))

    def test_explicit_legacy_zero_with_optional_zero_bridge_passes(self):
        for extra in ({}, {'bridgeCalls': {'search': 0, 'describe': 0, 'call': 0}}):
            with self.subTest(extra=extra):
                self.preflight_envelope({**self.model_envelope(), **extra})
                self.assertTrue(self.state.preflight_passed)
                self.assertTrue(self.state.tool_summary_measured)
                self.assertEqual(len(self.calls), 1)
                self.assertFalse(list(self.state_dir.iterdir()))

    def test_tool_summary_requires_exact_typed_zero(self):
        bad = [None, False, 0, [], '', {}, {'calls': 0}, {'tools': []},
               {'calls': 0, 'tools': ['exec']}, {'calls': 0, 'tools': None},
               {'calls': 0, 'tools': [], 'failures': 0}]
        bad += [{'calls': count, 'tools': []} for count in
                (False, True, 0.0, '0', None, -1, 1, [], {})]
        for summary in bad:
            with self.subTest(summary=summary):
                self.assert_model_blocked({**self.model_envelope(), 'toolSummary': summary})

    def test_bridge_activity_or_ambiguous_shape_blocks_even_with_legacy_zero(self):
        zero = {'search': 0, 'describe': 0, 'call': 0}
        bad = [None, False, 0, [], '', {}, {'search': 0, 'call': 0},
               {**zero, 'unknown': 0}]
        bad += [{**zero, field: count} for field in zero
                for count in (1, -1, False, True, 0.0, '0', None, [], {})]
        for bridge in bad:
            with self.subTest(bridge=bridge):
                self.assert_model_blocked({**self.model_envelope(), 'bridgeCalls': bridge})

    def test_current_envelope_without_general_tool_counter_remains_blocked(self):
        envelope = self.model_envelope()
        del envelope['toolSummary']
        # Synthetic current shape, not a new inference or a measured live result.
        envelope.update(bridgeCalls={'search': 0, 'describe': 0, 'call': 0},
                        payloads=[{'text': envelope['final']}], assistantTurns=1,
                        codeModeEngaged=True, sessionId='synthetic', usage={}, costUsd=0)
        self.assert_model_blocked(envelope)
        for field in ('bridgeCalls', 'payloads', 'assistantTurns', 'codeModeEngaged'):
            with self.subTest(missing=field):
                self.assert_model_blocked({k: v for k, v in envelope.items() if k != field})


    def test_partial_measurement_or_terminal_evidence_remains_blocked(self):
        for summary in (None, {'calls': 0, 'tools': [], 'coverage': 'partial'},
                        {'calls': 1, 'tools': ['synthetic']},
                        {'calls': 0, 'tools': [], 'terminalSummary':
                         {'calls': 1, 'tools': ['native'], 'failures': 1}}):
            with self.subTest(summary=summary):
                self.assert_model_blocked({**self.model_envelope(), 'toolSummary': summary})

    def test_silent_command_does_not_bypass_stderr_or_process_errors(self):
        for stderr, code in [('PRIVATE_DIAGNOSTIC', 0), (' ', 0), ('', 1), ('', 2)]:
            with self.subTest(stderr=bool(stderr), code=code):
                self.assert_model_blocked(self.model_envelope(), stderr=stderr, returncode=code)

    def test_model_identity_status_schema_and_safe_summary_still_block(self):
        for change in ({'provider': 'other'}, {'model': 'other'}, {'ok': False},
                       {'ok': 1}, {'status': 'timeout'}, {'final': '{}'},
                       {'final': json.dumps({**self.analysis, 'extra': None})}):
            with self.subTest(change=change):
                self.assert_model_blocked({**self.model_envelope(), **change})
        for envelope in (None, [], '', 0):
            with self.subTest(envelope=envelope):
                self.assert_model_blocked(envelope)

    def test_safe_summary_validation_still_blocks(self):
        for summary in ('', 'OTP 123456'):
            with self.subTest(summary=summary):
                # This validation is outside the analyzer's sanitized wrapper.
                envelope = {**self.model_envelope(),
                            'final': json.dumps({**self.analysis, 'summary': summary})}
                with self.assertRaises(ValueError): self.preflight_envelope(envelope)
                self.assertFalse(self.state.preflight_passed)
                with self.assertRaises(ConsentError): self.execute()
                self.assertEqual(len(self.calls), 1)
                self.assertFalse(list(self.state_dir.iterdir()))

    def discovery_state(self, *, accepted=True):
        self.plan['message_id'] = None
        self.state = OneShot(self.plan, self.runtime, runner=self.runner,
                             accept_discovery_metadata=accepted)

    def test_discovery_requires_separate_opt_in_before_model_or_network(self):
        self.discovery_state(accepted=False)
        with self.assertRaises(ValueError): self.state.preflight()
        with self.assertRaises(ConsentError): self.execute()
        self.assertFalse(self.calls)
        self.assertFalse(list(self.state_dir.iterdir()))

    def test_discovery_unique_result_is_private_and_no_id_needed_in_plan(self):
        self.discovery_state()
        self.state.preflight()
        page = self.state.page('synthetic-csrf')
        self.assertIn('metadatos de hasta dos', page)
        self.execute()
        self.assertIsNone(self.plan['message_id'])  # original operator plan unchanged
        self.assertTrue(self.state.saved)
        self.assertEqual(sum('search' in c for c in self.calls), 1)
        self.assertEqual(sum('get' in c for c in self.calls), 1)
        self.assertEqual(sum('send' in c for c in self.calls), 1)
        for marker in [self.password, BODY, 'PRIVATE_SEARCH_METADATA', 'abc1']:
            self.assertNotIn(marker, self.state.ledger.path.read_text())
        self.assertEqual(self.state.ledger.path, Ledger(dict(self.plan, message_id='abc1')).path)

    def test_discovery_missing_ambiguous_paged_or_malformed_never_gets_body(self):
        candidate = {'id': 'abc1', 'subject': SUBJECT}
        bad = [None, [], {}, {'messages': [], 'nextPageToken': ''},
               {'messages': [candidate, candidate], 'nextPageToken': ''},
               {'messages': [candidate], 'nextPageToken': 'MORE_PRIVATE'},
               {'messages': [candidate]}, {'messages': None, 'nextPageToken': ''},
               {'messages': [None], 'nextPageToken': ''},
               {'messages': [{'id': 'https://mail.invalid/thread', 'subject': SUBJECT}], 'nextPageToken': ''},
               {'messages': [{'id': 'abc1', 'subject': 'Other'}], 'nextPageToken': ''}]
        for value in bad:
            with self.subTest(value=value):
                self.calls.clear()
                self.discovery_state()
                self.search = value
                self.state.preflight()
                with self.assertRaises(ConsentError): self.execute()
                self.assertEqual(sum('search' in c for c in self.calls), 1)
                self.assertFalse(any('get' in c or 'send' in c for c in self.calls))
                self.assertEqual(sum('agent' in c for c in self.calls), 1)
                self.assertFalse(list(self.state_dir.iterdir()))

    def test_discovery_transport_errors_are_sanitized_without_retry(self):
        for failure in ['stderr', 'exit', 'invalid-json', 'timeout']:
            with self.subTest(failure=failure):
                self.calls.clear()
                self.discovery_state()
                def runner(args, **kwargs):
                    result = self.runner(args, **kwargs)
                    if 'search' in args:
                        if failure == 'timeout': raise subprocess.TimeoutExpired(args, 45)
                        if failure == 'stderr': result.stderr = 'PRIVATE_SEARCH_METADATA'
                        if failure == 'exit': result.returncode = 1
                        if failure == 'invalid-json': result.stdout = 'PRIVATE_SEARCH_METADATA'
                    return result
                self.state.runner = runner
                self.state.preflight()
                with self.assertRaises(ConsentError) as error: self.execute()
                self.assertNotIn('PRIVATE_SEARCH_METADATA', str(error.exception))
                self.assertEqual(sum('search' in c for c in self.calls), 1)
                self.assertFalse(any('get' in c or 'send' in c for c in self.calls))
                self.assertFalse(list(self.state_dir.iterdir()))

    def test_discovery_scopes_gate_still_precedes_search(self):
        self.discovery_state()
        self.state.preflight()
        self.auth['accounts'][0]['scopes'].append('https://www.googleapis.com/auth/gmail.modify')
        with self.assertRaises(ConsentError): self.execute()
        self.assertFalse(any('search' in c or 'get' in c or 'send' in c for c in self.calls))

    def test_discovery_dedupe_blocks_existing_explicit_id_before_body(self):
        Ledger(self.plan).write('unknown', claim=True)
        self.discovery_state()
        self.state.preflight()
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(sum('search' in c for c in self.calls), 1)
        self.assertFalse(any('get' in c or 'send' in c for c in self.calls))

    def test_discovery_search_does_not_authorize_nonfixture_or_moved_mail(self):
        for change in [{'body': 'PRIVATE_NON_FIXTURE'}, {'labelIds': ['SENT']},
                       {'labelIds': []}, {'attachments': [{'id': 'attachment'}]}]:
            with self.subTest(change=change):
                self.calls.clear()
                self.discovery_state()
                self.state.preflight()
                original = dict(self.msg)
                self.msg.update(change)
                with self.assertRaises(ConsentError): self.execute()
                self.msg = original
                self.assertFalse(any('send' in c for c in self.calls))
                self.assertEqual(sum('agent' in c for c in self.calls), 1)
                self.assertFalse(list(self.state_dir.iterdir()))

    def test_discovery_deadline_after_search_blocks_full_read(self):
        self.discovery_state()
        self.state.preflight()
        def runner(args, **kwargs):
            value = self.runner(args, **kwargs)
            if 'search' in args: self.state.deadline = 0
            return value
        self.state.runner = runner
        with self.assertRaises(ConsentError): self.execute()
        self.assertFalse(any('get' in c or 'send' in c for c in self.calls))
        self.assertFalse(list(self.state_dir.iterdir()))

    def test_success_one_read_one_send_sanitized_durable_receipt(self):
        self.state.preflight()
        self.execute()
        self.assertTrue(self.state.saved)
        self.assertEqual(sum('get' in c for c in self.calls), 1)
        self.assertEqual(sum('send' in c for c in self.calls), 1)
        self.assertEqual(sum('agent' in c for c in self.calls), 2)
        receipt = json.loads(self.state.ledger.path.read_text())
        self.assertEqual((receipt['state'], receipt['receipt']), ('sent', '501'))
        self.assertFalse(receipt['effective_scopes_verified'])
        self.assertEqual(self.state.ledger.path.stat().st_mode & 0o777, 0o600)
        persisted = self.state.ledger.path.read_text()
        for secret in [self.password, BODY, 'PRIVATE_SENDER_NOT_PROPAGATED', 'abc1', 'synthetic@example.invalid']:
            self.assertNotIn(secret, persisted)
        self.assertFalse((self.runtime / 'gmail-keyring-verification.json').exists())
        before = len(self.calls)
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(len(self.calls), before)

    def test_dedupe_across_restart_and_route_drift(self):
        self.state.preflight()
        self.execute()
        before = len(self.calls)
        for plan in [self.plan, dict(self.plan, telegram_chat_id='456', telegram_user_id='456'),
                     dict(self.plan, account=self.plan['account'].upper())]:
            restarted = OneShot(plan, self.runtime, runner=self.runner)
            with self.assertRaises(ValueError): restarted.preflight()
        self.assertEqual(len(self.calls), before)

    def test_timeout_unknown_and_never_retried(self):
        self.state.preflight()
        self.send_timeout = True
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(json.loads(self.state.ledger.path.read_text())['state'], 'unknown')
        self.assertFalse(self.state.saved)
        with self.assertRaises(ValueError): self.state.ledger.available()

    def test_receipt_must_confirm_same_private_chat(self):
        self.state.preflight()
        self.receipt_chat = '-999'
        with self.assertRaises(ConsentError): self.execute()
        self.assertEqual(json.loads(self.state.ledger.path.read_text())['state'], 'unknown')
        self.assertEqual(sum('send' in c for c in self.calls), 1)

    def test_scope_or_account_mismatch_stops_before_read(self):
        self.state.preflight()
        self.auth['accounts'][0]['scopes'].append('https://www.googleapis.com/auth/gmail.send')
        with self.assertRaises(ConsentError): self.execute()
        self.assertFalse(any('get' in c or 'send' in c for c in self.calls))
        self.assertFalse(list(self.state_dir.iterdir()))

    def test_only_exact_safe_fixture_without_attachments(self):
        for change in [{'body': 'OTP 123456'}, {'headers': {'subject': 'Other'}},
                       {'attachments': [{'id': 'not-read'}]}, {'labelIds': ['SENT']},
                       {'id': 'abc2'}]:
            with self.subTest(change=change):
                self.state = OneShot(self.plan, self.runtime, runner=self.runner)
                self.state.preflight()
                original = dict(self.msg)
                self.msg.update(change)
                with self.assertRaises(ConsentError): self.execute()
                self.msg = original
                self.assertFalse(any('send' in c for c in self.calls))
                self.assertFalse(list(self.state_dir.iterdir()))

    def test_deadline_before_send_leaves_no_claim(self):
        self.state.preflight()
        original = self.state.analyzer.analyze
        def slow(*args):
            value = original(*args)
            self.state.deadline = 0
            return value
        self.state.analyzer.analyze = slow
        with self.assertRaises(ConsentError): self.execute()
        self.assertFalse(list(self.state_dir.iterdir()))
        self.assertFalse(any('send' in c for c in self.calls))

    def test_unsafe_or_incompatible_analysis_has_no_send(self):
        self.state.preflight()
        self.analysis['summary'] = 'OTP 123456'
        with self.assertRaises(ConsentError): self.execute()
        self.assertFalse(any('send' in c for c in self.calls))
        self.assertFalse(list(self.state_dir.iterdir()))

    def test_existing_claim_exclusive_and_corrupt_claim_fail_closed(self):
        self.state.ledger.write('inflight', claim=True)
        other = Ledger(self.plan)
        with self.assertRaises(FileExistsError): other.write('inflight', claim=True)
        self.state.ledger.path.write_text('')
        with self.assertRaises(ValueError): other.available()

    def test_http_routes_replay_and_native_form(self):
        self.state.preflight()
        server = ThreadingHTTPServer(('127.0.0.1', 0), oneshot_handler(self.state, 'https://private.invalid/'))
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
        headers = {'Cookie': cookie, 'Origin': 'null', 'Sec-Fetch-Site': 'same-origin',
                   'Content-Type': 'application/x-www-form-urlencoded'}
        form = urlencode({'csrf': csrf, 'password': self.password})
        for path, changes, expected in [('/run', {'Origin': 'https://evil.invalid'}, 403),
                                        ('/run', {'Cookie': ''}, 403), ('/verify', {}, 404),
                                        ('/start', {}, 404)]:
            conn.request('POST', path, form, dict(headers, **changes))
            response = conn.getresponse()
            self.assertEqual(response.status, expected)
            response.read()
        conn.request('POST', '/run', form, headers)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertNotIn(self.password, response.read().decode())
        before = len(self.calls)
        conn.request('POST', '/run', form, headers)
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        self.assertEqual(len(self.calls), before)

    def test_local_cli_preflight_no_network_and_scope_opt_in_blocks_serve(self):
        # Use a temporary project checkout, not the real project's ledger.
        import shutil
        for name in ['oneshot.py', 'gmail_unlock.py', 'gmail_consent.py', 'pipeline.py', 'assistant.py']:
            shutil.copyfile(Path(__file__).resolve().parents[1] / "src" / name, self.root / name)
        self.state_dir.rename(self.root / '.oneshot-state')
        self.state_dir = self.root / '.oneshot-state'
        self.plan['state_dir'] = str(self.state_dir)
        self.write_plan()
        command = [sys.executable, str(self.root / 'oneshot.py'), '--plan', str(self.plan_path)]
        result = subprocess.run(command + ['preflight'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)['network_checked'])
        # Even with no portal URL configured, scope-risk rejection happens first.
        result = subprocess.run(command + ['serve'], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)['blocked'])
        self.assertFalse(list(self.state_dir.iterdir()))

        # Discovery plans also validate locally without invoking any provider.
        self.plan['message_id'] = None
        self.write_plan()
        result = subprocess.run(command + ['preflight'], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        opted_in = command + ['--accept-fixture-discovery-metadata']
        result = subprocess.run(opted_in + ['preflight'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)['network_checked'])
        self.assertFalse(list(self.state_dir.iterdir()))

    def test_process_simulation_uses_fake_executables_and_dedupes_restart(self):
        script = self.root / 'fake_transport.py'
        responses = {'analysis': {'ok': True, 'status': 'ok', 'provider': 'openai',
                                 'toolSummary': {'calls': 0, 'tools': []},
                                 'model': 'gpt-6.1-sol', 'final': json.dumps(self.analysis)},
                     'auth': self.auth, 'mail': {'message': self.msg}, 'search': self.search,
                     'send': {'action': 'send', 'channel': 'telegram', 'dryRun': False,
                              'payload': {'deliveryStatus': 'sent', 'result': {'chatId': '123', 'messageId': '888'}}}}
        script.write_text('import sys,json,os\n'
                          'args=sys.argv[1:]\n'
                          'responses=' + repr(responses) + '\n'
                          'if "agent" in args:\n'
                          ' assert "GOG_KEYRING_PASSWORD" not in os.environ\n'
                          ' assert "Prueba controlada" in sys.stdin.read()\n'
                          ' key="analysis"\n'
                          'elif "auth" in args: key="auth"\n'
                          'elif "search" in args: key="search"\n'
                          'elif "get" in args: key="mail"\n'
                          'else:\n'
                          ' assert "GOG_KEYRING_PASSWORD" not in os.environ\n'
                          ' key="send"\n'
                          'print(json.dumps(responses[key]))\n')
        def process(args, **kwargs):
            self.calls.append(args)
            return subprocess.run([sys.executable, str(script)] + args[1:], **kwargs)
        self.plan['message_id'] = None
        self.state = OneShot(self.plan, self.runtime, runner=process, accept_discovery_metadata=True)
        self.state.preflight()
        self.execute()
        self.assertEqual(json.loads(self.state.ledger.path.read_text())['receipt'], '888')
        restarted = OneShot(self.plan, self.runtime, runner=process, accept_discovery_metadata=True)
        restarted.preflight()
        with self.assertRaises(ConsentError): restarted.execute({'password': [self.password]})
        self.assertEqual(sum('search' in c for c in self.calls), 2)
        self.assertEqual(sum('get' in c for c in self.calls), 1)
        self.assertEqual(sum('send' in c for c in self.calls), 1)




if __name__ == '__main__':
    unittest.main()
