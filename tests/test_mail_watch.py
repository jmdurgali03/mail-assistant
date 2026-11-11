from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import pty
import select
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from assistant import Assistant
from mail_watch import (CONTRACT, ContinuousItem, ReadTransport, Watch, WatchError,
                        load_password, normalize, validate_config)
import test_mail_readonly
from watch_setup import store

NOW = datetime(2026, 10, 25, 22, 0, tzinfo=timezone.utc)
CFG = {'account': 'synthetic@example.invalid', 'watch_contract': CONTRACT,
       'analysis_batch_limit': 3, 'telegram_account_id': 'default',
       'telegram_chat_id': '123', 'telegram_user_id': '123',
       'analysis_model': 'openai/gpt-6.1-sol', 'analysis_profile': 'bichi-mail-analysis',
       'incoming_notifications': 'individual_all', 'initial_sync_mode': 'baseline',
       'live_reads_enabled': True, 'analysis_enabled': True, 'delivery_enabled': True,
       'job_enabled': True, 'correction_bridge_enabled': False}


def metadata(mid='abc1', at=NOW):
    return {'id': mid, 'threadId': mid, 'subject': 'Proyecto', 'from': 'Persona sintética',
            'labels': ['INBOX', 'UNREAD'], 'internalDateIso': at.isoformat()}


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'watch.sqlite3'
        self.app = Assistant(self.db, CFG)
        self.addCleanup(lambda: self.app.close())
        self.now = NOW
        self.watch = Watch(self.app, clock=lambda: self.now)
        self.watch.arm(NOW - timedelta(seconds=1))
        self.pages = {'': {'messages': [metadata()]}}
        self.calls = []
        self.reader = ReadTransport(Path(self.tmp.name), CFG['account'], 'synthetic-password-long', self.run_fake)

    def run_fake(self, args, **kwargs):
        self.calls.append((args, {**kwargs, 'env': dict(kwargs['env'])}))
        page = next((arg[7:] for arg in args if arg.startswith('--page=')), '')
        return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(self.pages[page]))

    def capture(self):
        return self.watch.capture(self.reader, self.now)

    def test_excludes_history_sender_date_and_future(self):
        self.pages['']['messages'] += [metadata('a11', NOW - timedelta(days=10)),
                                       metadata('a12', NOW + timedelta(minutes=1))]
        self.pages['']['messages'][1]['date'] = NOW.isoformat()
        self.capture()
        self.assertEqual(self.watch.status()['events'], {'queued': 1})
        self.assertIn(' after:', ' '.join(self.calls[0][0]))
        self.assertIn('--readonly', self.calls[0][0])
        self.assertNotIn('--all', self.calls[0][0])

    def test_social_query_and_returned_label_excluded_before_queue(self):
        promo = metadata('abc2')
        promo['labels'].append('CATEGORY_SOCIAL')
        self.pages['']['messages'].append(promo)
        self.assertEqual(self.capture(), 1)
        self.assertIn('-category:social', ' '.join(self.calls[0][0]))
        self.assertEqual(self.watch.status()['events'], {'queued': 1})
        self.assertIsNone(self.app.db.execute("SELECT 1 FROM mail WHERE id='abc2'").fetchone())

    def test_existing_social_never_construct_reader_or_recover_notice(self):
        self.capture()
        facts = normalize(metadata())
        facts['labels'].append('CATEGORY_SOCIAL')
        for state in ('queued', 'processing'):
            with self.app.db:
                self.app.db.execute('UPDATE mail SET facts=?', (json.dumps(facts),))
                self.app.db.execute('UPDATE watch_events SET state=?', (state,))
            result = self.watch.process(lambda _: self.fail('no body/model factory for Social'))
            self.assertEqual(result, {'processed': 0, 'needs_review': 0})
            self.assertEqual(self.watch.status()['events'], {'excluded': 1})
            self.assertEqual(self.watch.status()['outbox'], {})

    def test_existing_ready_social_cancelled_without_mutating_delivery_history(self):
        self.capture()
        facts = normalize(metadata())
        facts['labels'].append('CATEGORY_SOCIAL')
        with self.app.db:
            self.app.db.execute('UPDATE mail SET facts=?', (json.dumps(facts),))
        for state in ('ready', 'inflight', 'unknown', 'sent'):
            with self.app.db:
                self.app.db.execute('DELETE FROM outbox')
                self.app.db.execute("INSERT INTO outbox VALUES ('incoming:abc1','incoming','old',?,?,NULL,NULL)",
                                    (self.watch.box.route(), state))
            self.watch.exclude_categories()
            self.assertEqual(self.watch.status()['outbox'], {'cancelled' if state == 'ready' else state: 1})

    def test_analysis_cooldown_still_cancels_old_social_before_delivery(self):
        self.capture()
        self.watch._notice('abc1', 'Old notice')
        facts = normalize(metadata())
        facts['labels'].append('CATEGORY_SOCIAL')
        with self.app.db:
            self.app.db.execute('UPDATE mail SET facts=?', (json.dumps(facts),))
            self.app.set_meta('watch_analysis_resume', (NOW + timedelta(minutes=15)).isoformat())
        class Reader:
            def authenticate(self): pass
            def capture(self, *args): return []
        class Sender:
            def send(inner, row): self.fail('must not deliver Social')
        result = self.watch.tick(Reader(), lambda _: self.fail('cooldown'), Sender(), NOW)
        self.assertEqual(result['delivery'], {'sent': 0})
        self.assertEqual(self.watch.status()['outbox'], {'cancelled': 1})

    def test_promotions_query_and_returned_label_excluded_before_queue(self):
        promo = metadata('abc2')
        promo['labels'].append('CATEGORY_PROMOTIONS')
        self.pages['']['messages'].append(promo)
        self.assertEqual(self.capture(), 1)
        self.assertIn('-category:promotions', ' '.join(self.calls[0][0]))
        self.assertEqual(self.watch.status()['events'], {'queued': 1})
        self.assertIsNone(self.app.db.execute("SELECT 1 FROM mail WHERE id='abc2'").fetchone())

    def test_existing_promotions_never_construct_reader_or_recover_notice(self):
        self.capture()
        facts = normalize(metadata())
        facts['labels'].append('CATEGORY_PROMOTIONS')
        for state in ('queued', 'processing'):
            with self.app.db:
                self.app.db.execute('UPDATE mail SET facts=?', (json.dumps(facts),))
                self.app.db.execute('UPDATE watch_events SET state=?', (state,))
            result = self.watch.process(lambda _: self.fail('no body/model factory for Promotions'))
            self.assertEqual(result, {'processed': 0, 'needs_review': 0})
            self.assertEqual(self.watch.status()['events'], {'excluded': 1})
            self.assertEqual(self.watch.status()['outbox'], {})

    def test_existing_ready_promotion_cancelled_without_mutating_delivery_history(self):
        self.capture()
        facts = normalize(metadata())
        facts['labels'].append('CATEGORY_PROMOTIONS')
        with self.app.db:
            self.app.db.execute('UPDATE mail SET facts=?', (json.dumps(facts),))
        for state in ('ready', 'inflight', 'unknown', 'sent'):
            with self.app.db:
                self.app.db.execute('DELETE FROM outbox')
                self.app.db.execute("INSERT INTO outbox VALUES ('incoming:abc1','incoming','old',?,?,NULL,NULL)",
                                    (self.watch.box.route(), state))
            self.watch.exclude_categories()
            self.assertEqual(self.watch.status()['outbox'], {'cancelled' if state == 'ready' else state: 1})

    def test_analysis_cooldown_still_cancels_old_promotion_before_delivery(self):
        self.capture()
        self.watch._notice('abc1', 'Old notice')
        facts = normalize(metadata())
        facts['labels'].append('CATEGORY_PROMOTIONS')
        with self.app.db:
            self.app.db.execute('UPDATE mail SET facts=?', (json.dumps(facts),))
            self.app.set_meta('watch_analysis_resume', (NOW + timedelta(minutes=15)).isoformat())
        class Reader:
            def authenticate(self): pass
            def capture(self, *args): return []
        class Sender:
            def send(inner, row): self.fail('must not deliver Promotions')
        result = self.watch.tick(Reader(), lambda _: self.fail('cooldown'), Sender(), NOW)
        self.assertEqual(result['delivery'], {'sent': 0})
        self.assertEqual(self.watch.status()['outbox'], {'cancelled': 1})

    def test_notice_clean_sender_full_summary_no_duplicate_subject_or_email(self):
        self.pages['']['messages'][0]['from'] = 'Equipo de soporte <support@example.invalid>'
        self.capture()
        with self.app.db:
            self.watch._notice('abc1', '«Revisá el contrato antes del viernes.»\nPrioridad: alta')
        payload = json.loads(self.watch.box.rows()[0]['payload'])
        self.assertIn('👤 **Equipo de soporte**\n', payload['text'])
        self.assertIn('**Proyecto**', payload['text'])
        self.assertNotIn('support@example.invalid', payload['text'])
        self.assertNotIn('https://', payload['text'])
        self.assertEqual(payload['presentation']['blocks'][0]['buttons'][0]['action']['url'],
                         'https://mail.google.com/mail/?authuser=synthetic%40example.invalid#all/abc1')

    def test_durable_boundary_no_reset_on_restart(self):
        self.capture()
        self.app.close()
        self.app = Assistant(self.db, CFG)
        self.watch = Watch(self.app, clock=lambda: self.now)
        self.assertFalse(self.watch.arm(NOW + timedelta(days=2)))
        self.assertEqual(self.watch.status()['boundary'], (NOW - timedelta(seconds=1)).isoformat())
        self.capture()
        self.assertEqual(self.watch.status()['events'], {'queued': 1})

    def test_incomplete_page_no_ingest_cursor_or_analysis(self):
        self.pages['']['nextPageToken'] = 'private-page'
        self.pages['private-page'] = {'messages': None, 'nextPageToken': 'again'}
        before = self.watch.status()['cursor']
        with self.assertRaises(WatchError): self.capture()
        self.assertEqual(self.watch.status()['events'], {})
        self.assertEqual(self.watch.status()['cursor'], before)

    def test_terminal_null_valid_and_malformed_missing_rejected(self):
        self.pages['']['nextPageToken'] = 'terminal'
        self.pages['terminal'] = {'messages': None}
        self.assertEqual(self.capture(), 1)
        self.pages['terminal'] = {}
        with self.assertRaises(WatchError): self.capture()

    def test_token_loop_and_invalid_types_rejected(self):
        for token in (0, [], '\n', 'loop'):
            self.pages['']['nextPageToken'] = token
            self.pages['loop'] = {'messages': [], 'nextPageToken': 'loop'}
            with self.assertRaises(WatchError): self.capture()

    def test_no_unbounded_paging(self):
        for i in range(10):
            self.pages[str(i) if i else ''] = {'messages': [], 'nextPageToken': str(i + 1)}
        with self.assertRaises(WatchError): self.capture()
        self.assertEqual(len(self.calls), 10)
        self.assertEqual(self.watch.status()['events'], {})

    def test_invalid_internal_date_and_excluded_mail_rejected(self):
        for changes in ({'internalDateIso': 'bad'}, {'threadId': '-bad'}, {'body': 'no-body'},
                        {'labels': ['SPAM', 'INBOX']}, {'labels': ['DRAFT', 'INBOX']}):
            self.pages['']['messages'] = [{**metadata(), **changes}]
            with self.assertRaises((ValueError, WatchError)): self.capture()
        self.assertEqual(self.watch.status()['events'], {})

    def test_secret_headers_scrubbed_before_sqlite(self):
        self.pages['']['messages'][0]['subject'] = 'Código de acceso 123456'
        self.capture()
        self.assertNotIn(b'123456', self.db.read_bytes())

    def test_wrong_route_and_existing_historical_database_rejected(self):
        self.app.config = {**CFG, 'telegram_chat_id': '999'}
        with self.assertRaises(WatchError): self.watch._bound()
        for patch_cfg in ({'telegram_account_id': 'albert'}, {'telegram_chat_id': '-1'},
                          {'iol_summary_mode': 'invalid'},
                          {'job_enabled': False}):
            with self.assertRaises(WatchError): validate_config({**CFG, **patch_cfg})
        self.app.config = CFG
        with self.app.db:
            self.app.db.execute("DELETE FROM meta WHERE key='watch_contract'")
            self.app.db.execute("INSERT INTO mail VALUES ('aa','{}','{}',0)")
        with self.assertRaises(WatchError): self.watch.arm(NOW)

    def test_public_config_uses_explicit_private_recipient_not_author_identity(self):
        validate_config({**CFG, 'telegram_user_id': '456', 'telegram_chat_id': '456'})
        for user, chat in ((None, None), (123, 123), ('0', '0'), ('-1', '-1'),
                           ('123', '456'), ('', ''), ('1' * 21, '1' * 21)):
            with self.subTest(user=user, chat=chat), self.assertRaises(WatchError):
                validate_config({**CFG, 'telegram_user_id': user, 'telegram_chat_id': chat})

    def test_crash_after_processing_claim_is_review_not_inference_retry(self):
        self.capture()
        with self.app.db:
            self.app.db.execute("UPDATE watch_events SET state='processing'")
        self.watch.recover()
        self.watch.recover()
        self.assertEqual(self.watch.status()['events'], {'needs_review': 1})
        self.assertEqual(self.watch.status()['outbox'], {'ready': 1})
        self.assertEqual(self.watch.process(lambda _: self.fail('must not retry'))['processed'], 0)

    def test_gmail_failure_cooldown_and_auth_latch(self):
        class Reader:
            def authenticate(self): raise WatchError('keyring_unlock_required')
        with self.assertRaises(WatchError): self.watch.tick(Reader(), None, None, NOW)
        self.assertEqual(self.watch.status()['blocked'], 'keyring_unlock_required')
        self.assertEqual(self.watch.status()['events'], {})

    def test_password_only_in_credential_environment_not_argv(self):
        self.capture()
        args, kwargs = self.calls[0]
        self.assertNotIn(self.reader.password, str(args))
        self.assertNotIn('GOG_ACCESS_TOKEN', kwargs['env'])
        self.assertIn('GOG_KEYRING_PASSWORD', kwargs['env'])

    def test_credential_storage_explicit_private_no_overwrite(self):
        path = Path(self.tmp.name) / 'gmail-keyring-password'
        store(path, 'synthetic-password-long')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError): store(path, 'another-test-password')
        with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': self.tmp.name}):
            self.assertEqual(load_password(), 'synthetic-password-long')
            path.chmod(0o644)
            with self.assertRaises(WatchError): load_password()

    def test_setup_requires_explicit_storage_and_private_tty(self):
        root = Path(self.tmp.name)/'.config/bichi-mail'
        root.mkdir(parents=True, mode=0o700)
        config = root/'watch.json'
        config.write_text('{}')
        config.chmod(0o600)
        program = "import sys,watch_setup;watch_setup.preflight=lambda *args:None;sys.argv=['setup']+sys.argv[1:];raise SystemExit(watch_setup.main())"
        env = {**os.environ, 'HOME': self.tmp.name}
        for flags in ([], ['--accept-private-file-storage']):
            result = subprocess.run([sys.executable,'-c',program,*flags],env=env,
                                    input='not-a-password',text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn('not-a-password', result.stdout + result.stderr)
            self.assertFalse((root/'gmail-keyring-password').exists())

    def test_setup_private_pty_no_echo_or_password_log(self):
        root = Path(self.tmp.name)/'.config/bichi-mail'
        root.mkdir(parents=True, mode=0o700)
        config = root/'watch.json'
        config.write_text('{}')
        config.chmod(0o600)
        program = "import sys,watch_setup;watch_setup.preflight=lambda *args:None;sys.argv=['setup','--accept-private-file-storage'];raise SystemExit(watch_setup.main())"
        pid, fd = pty.fork()
        if pid == 0:
            os.environ['HOME'] = self.tmp.name
            os.execv(sys.executable, [sys.executable,'-c',program])
        output, sent = b'', 0
        password = b'synthetic-pty-password'
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                if select.select([fd],[],[],0.1)[0]:
                    try: chunk = os.read(fd,65536)
                    except OSError: break
                    if not chunk: break
                    output += chunk
                    if sent == 0 and b'almac' in output and output.endswith(b': '):
                        os.write(fd,password+b'\n'); sent=1
                    elif sent == 1 and b'Repetir contrase' in output:
                        os.write(fd,password+b'\n'); sent=2
            _, status = os.waitpid(pid, os.WNOHANG)
            if status == 0 and not (root/'gmail-keyring-password').exists():
                self.fail('PTY did not finish')
            self.assertEqual(sent, 2)
            self.assertNotIn(password, output)
            self.assertEqual((root/'gmail-keyring-password').read_bytes(),password)
        finally:
            os.close(fd)
            try: os.kill(pid,signal.SIGKILL)
            except ProcessLookupError: pass
            try: os.waitpid(pid,0)
            except ChildProcessError: pass


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.harness = test_mail_readonly.MailSessionTests()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.f = self.harness.fixture
        self.f.rendering = {'title': 'Revisión del proyecto',
                            'sentences': [{'text': 'Solicita revisar el proyecto.', 'fact_ids': ['f1']}],
                            'action': {'text': 'Revisar el proyecto.', 'fact_ids': ['f1']},
                            'decisions': self.f.rendering['decisions']}
        self.f.envelope['final'] = json.dumps(self.f.rendering)
        self.app = Assistant(self.f.home / 'watch.sqlite3', CFG)
        self.addCleanup(self.app.close)
        self.now = NOW
        self.watch = Watch(self.app, clock=lambda: self.now)
        self.watch.arm(NOW - timedelta(seconds=1))
        self.sent, self.items = [], []

    def factory(self, save):
        item = ContinuousItem(self.f.runtime, self.f.config, save,
                              runner=self.f.run_fake, clock=lambda: self.f.now,
                              decider=self.harness.decider)
        item.watch_password = self.f.password
        self.items.append(item)
        return item

    def tick(self, mid='abc1', sender=None):
        outer = self
        class Reader:
            def authenticate(self): pass
            def capture(self, *args): return [normalize(metadata(mid))]
        class Sender:
            def send(self, row):
                outer.sent.append(row)
                return str(len(outer.sent))
        return self.watch.tick(Reader(), self.factory, sender or Sender(), self.now)

    def test_gpt_jev_gpt_individual_delivery_dedupe_and_restart(self):
        result = self.tick()
        self.assertEqual(result['delivery'], {'sent': 1})
        self.assertEqual(self.watch.status()['events'], {'analyzed': 1})
        payload = json.loads(self.sent[0]['payload'])
        self.assertIn('Revisión del proyecto', payload['text'])
        self.assertIn('Prioridad media', payload['text'])
        self.assertIn('Solicita revisar el proyecto', payload['text'])
        self.assertEqual(json.loads(self.sent[0]['route']), {'account': 'default', 'chat': '123'})
        receipt = json.loads(self.app.db.execute('SELECT receipt FROM watch_events').fetchone()[0])
        self.assertEqual([s['stage'] for s in receipt['pipeline_stages']],
                         ['preparing_gpt', 'deciding_jev', 'summarizing_gpt'])
        self.assertEqual(receipt['model_invocations'], 2)
        self.assertEqual(receipt['decision']['invocations'], 1)
        self.assertEqual(self.tick()['delivery'], {'sent': 0})
        self.assertEqual(len(self.items), 1)
        self.assertEqual(self.watch.status()['outbox'], {'sent': 1})
        for secret in (self.f.password, self.f.private_marker, 'PRIVATE_RECIPIENT_NOT_PROPAGATED'):
            self.assertNotIn(secret.encode(), (self.f.home / 'watch.sqlite3').read_bytes())

    def test_jev_failure_no_final_gpt_or_default_summary_and_no_retry(self):
        self.harness.decider.error = 'timeout'
        result = self.tick()
        self.assertEqual(result['needs_review'], 1)
        self.assertEqual(self.items[0].receipt['model_invocations'], 1)
        self.assertIn('Nuevo correo para revisar', self.sent[0]['payload'])
        self.assertNotIn('No se pudo completar', self.sent[0]['payload'])
        self.assertNotIn('Prioridad:', self.sent[0]['payload'])
        self.tick()
        self.assertEqual(len(self.items), 1)
        self.assertTrue(self.watch.status()['analysis_resume'])

    def test_quota_queue_capture_restart_and_recovery_report(self):
        self.f.model_rc, self.f.stderr = 1, 'Rate limit reached (429) PRIVATE_QUOTA_DETAIL'
        result = self.tick()
        self.assertEqual(self.watch.status()['events'], {'queued': 1})
        self.assertEqual(self.sent, [])
        self.assertTrue(self.watch.status()['recovery_batch_active'])
        # Gmail capture continues without invoking models while quota is cooling.
        self.tick('abc2')
        self.assertEqual(self.watch.status()['events'], {'queued': 2})
        self.assertEqual(len(self.items), 1)
        self.watch = Watch(self.app, clock=lambda: self.now)
        original = self.f.run_fake
        def multi_message(args, **kw):
            second = 'get' in args and 'abc2' in args
            if second:
                args = ['abc1' if a == 'abc2' else a for a in args]
            result = original(args, **kw)
            if second:
                data = json.loads(result.stdout)
                data['message']['id'] = 'abc2'
                result.stdout = json.dumps(data)
            return result
        self.f.run_fake = multi_message
        self.f.model_rc, self.f.stderr = 0, ''
        self.now += timedelta(minutes=16)
        result = self.tick('abc2')
        self.assertEqual(self.watch.status()['events'], {'analyzed': 2})
        reports = [r for r in self.sent if r['kind'] == 'recovery']
        self.assertEqual(len(reports), 1)
        self.assertIn('2 correos pendientes', reports[0]['payload'])
        self.assertIn('Solicita revisar el proyecto', reports[0]['payload'])
        self.assertFalse(self.watch.status()['recovery_batch_active'])
        self.tick('abc2')
        self.assertEqual(len([r for r in self.sent if r['kind'] == 'recovery']), 1)
        self.assertNotIn(b'PRIVATE_QUOTA_DETAIL', (self.f.home / 'watch.sqlite3').read_bytes())

    def test_jev_transport_failure_queued_but_invalid_result_not_retried(self):
        self.harness.decider.error = 'gateway_unavailable'
        self.harness.decider.metadata['reason'] = 'gateway_unavailable'
        self.tick()
        self.assertEqual(self.watch.status()['events'], {'queued': 1})
        self.assertEqual(self.sent, [])

    def test_model_final_cannot_trigger_quota_retry(self):
        self.f.model_rc, self.f.stderr = 0, ''
        self.f.envelope['final'] = 'quota 429 usage limit'
        self.tick()
        self.assertEqual(self.watch.status()['events'], {'needs_review': 1})
        self.assertFalse(self.watch.status()['recovery_batch_active'])

    def test_recovery_report_unknown_is_not_resent(self):
        self.f.model_rc, self.f.stderr = 1, 'usage limit'
        self.tick()
        self.f.model_rc, self.f.stderr = 0, ''
        self.now += timedelta(minutes=16)
        outer = self
        class Sender:
            def send(self, row):
                if row['kind'] == 'recovery':
                    raise subprocess.TimeoutExpired('synthetic', 1)
                outer.sent.append(row)
                return '42'
        self.tick(sender=Sender())
        self.assertEqual(self.app.db.execute("SELECT state FROM outbox WHERE kind='recovery'").fetchone()[0],
                         'unknown')
        self.tick()
        self.assertFalse(any(r['kind'] == 'recovery' for r in self.sent))

    def test_delivery_timeout_unknown_no_automatic_resend(self):
        class Timeout:
            def send(self, row): raise subprocess.TimeoutExpired('synthetic', 1)
        self.assertEqual(self.tick(sender=Timeout())['delivery']['blocked'], 'unknown_delivery')
        self.assertEqual(self.watch.status()['outbox'], {'unknown': 1})
        self.assertEqual(self.tick()['delivery'], {'sent': 0})
        self.assertEqual(len(self.sent), 0)
        row = self.watch.box.rows()[0]
        self.watch.box.ack(row['token'], '99')
        self.assertEqual(self.watch.status()['outbox'], {'sent': 1})

    def test_unverified_pipeline_never_publishes_returned_summary(self):
        factory = self.factory
        def unverified(save):
            item = factory(save)
            item._functional_passed = lambda: False
            return item
        self.factory = unverified
        self.tick()
        payload = json.loads(self.sent[0]['payload'])
        self.assertIn('Nuevo correo para revisar', payload['text'])
        self.assertNotIn('Solicita revisar', payload['text'])
        self.assertNotIn('Revisión del proyecto', payload['text'])

    def test_body_authentication_bypasses_models_and_scrubs_metadata(self):
        self.f.message['headers']['from'] = 'Acme <login@acme.example>'
        self.f.message['body'] = 'Código de acceso 987654'
        self.tick()
        self.assertEqual(self.items[0].receipt['model_invocations'], 0)
        self.assertNotIn('987654', self.sent[0]['payload'])
        self.assertIn('Seguridad y acceso', self.sent[0]['payload'])
        self.assertIn('Código de acceso', self.sent[0]['payload'])
        self.assertIn('Acme', self.sent[0]['payload'])
        facts = json.loads(self.app.db.execute('SELECT facts FROM mail').fetchone()[0])
        self.assertEqual(facts['sender'], 'Acme <login@acme.example>')
        self.assertNotIn(b'987654', (self.f.home / 'watch.sqlite3').read_bytes())

    def test_header_auth_context_survives_queue_restart_without_body_or_models(self):
        facts = normalize({**metadata(), 'subject': 'Tu código de acceso 987654',
                           'from': 'Acme <login@acme.example>'})
        class Reader:
            def capture(self, *args): return [facts]
        self.watch.capture(Reader(), self.now)
        self.app.close()
        self.app = Assistant(self.f.home / 'watch.sqlite3', CFG)
        self.addCleanup(self.app.close)
        self.watch = Watch(self.app, clock=lambda: self.now)
        self.assertEqual(self.watch.process(self.factory), {'processed': 1, 'needs_review': 0})
        item = self.items[0]
        self.assertEqual(item.receipt['body_get_invocations'], 0)
        self.assertEqual(item.receipt['model_invocations'], 0)
        saved_receipt = json.loads(self.app.db.execute('SELECT receipt FROM watch_events').fetchone()[0])
        self.assertTrue(saved_receipt['authentication_protected'])
        payload = json.loads(self.watch.box.rows()[0]['payload'])
        for expected in ('Acme', 'Código de acceso', 'Prioridad baja', 'Categoría: Seguridad y acceso'):
            self.assertIn(expected, payload['text'])
        self.assertNotIn('987654', payload['text'])
        self.assertNotIn(b'987654', (self.f.home / 'watch.sqlite3').read_bytes())

    def test_cli_process_real_subprocesses_with_fake_executables_and_restart(self):
        # No live binaries/credentials: fake HOME, config, gog/openclaw/node,
        # synthetic credential and deterministic clock in a separate process.
        folder = self.f.home
        bin_dir = folder / 'bin'
        bin_dir.mkdir()
        fake = '''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
root = Path(os.environ['HOME'])
cfg = json.loads((root/'fixture.json').read_text())
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root/'calls.jsonl').open('a') as out:
    out.write(json.dumps({'program':name,'stage':('gpt' if name=='openclaw' else 'jev' if name=='node' else args[args.index('--enable-commands-exact')+1])})+'\\n')
if name == 'gog':
    assert os.environ['GOG_KEYRING_PASSWORD'] == 'synthetic-process-password'
    command = args[args.index('--enable-commands-exact')+1]
    value = cfg[{'auth.list':'auth','gmail.messages.search':'listing','gmail.get':'message',
                 'gmail.labels.list':'labels'}[command]]
elif name == 'node':
    assert 'GOG_KEYRING_PASSWORD' not in os.environ
    prepared = json.load(sys.stdin)
    assert list(prepared) == ['state','questions']
    value = cfg['jev']
else:
    assert 'GOG_KEYRING_PASSWORD' not in os.environ
    if 'agent' in args:
        prompt = sys.stdin.read()
        final = cfg['preparation'] if 'ETAPA_PREPARAR_GPT' in prompt else cfg['rendering']
        value = {**cfg['envelope'], 'final':json.dumps(final)}
    else:
        assert args[:2] == ['message','send']
        assert args[args.index('--account')+1] == 'default'
        assert args[args.index('--target')+1] == '123'
        value = {'dryRun':False,'channel':'telegram','action':'send','payload':{'deliveryStatus':'sent','result':{'messageId':101}}}
print(json.dumps(value))
'''
        for name, path in [('gog', self.f.gog), ('openclaw', bin_dir/'openclaw'), ('node', bin_dir/'node')]:
            path.write_text(fake)
            path.chmod(0o700)
        from test_jev_decisions import outcome
        fixture = {'auth': self.f.auth, 'listing': {'messages': [metadata()]},
                   'message': {'message': self.f.message}, 'preparation': self.f.preparation,
                   'rendering': {**self.f.rendering, 'label': {
                       'kind': 'existing', 'name': 'Trabajo', 'fact_ids': ['f1']}},
                   'labels': {'labels': [{'name': 'Trabajo', 'type': 'user'}]},
                   'envelope': self.f.envelope, 'jev': outcome()}
        (folder/'fixture.json').write_text(json.dumps(fixture))
        (folder/'gmail-keyring-password').write_text('synthetic-process-password')
        (folder/'gmail-keyring-password').chmod(0o600)
        config = folder/'watch.json'
        config.write_text(json.dumps({**CFG,'analysis_config': str(self.f.config)}))
        config.chmod(0o600)
        state = folder/'state'
        state.mkdir(mode=0o700)
        bootstrap = '''
import hashlib,sys
from pathlib import Path
from datetime import timedelta
import mail_watch,inbox_readonly
digest=hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest()
mail_watch.CONFIG_SHA256=inbox_readonly.CONFIG_SHA256=digest
mail_watch.GOG_SHA256=hashlib.sha256((Path.home()/'.local/bin/gog').read_bytes()).hexdigest()
moment=mail_watch.iso(sys.argv[2])
mail_watch.now_utc=lambda:moment
sys.argv=sys.argv[2:]
sys.argv[0]='mail_watch.py'
raise SystemExit(mail_watch.main())
'''
        env = {'HOME': str(folder), 'PATH': str(bin_dir) + ':/usr/bin:/bin',
               'CREDENTIALS_DIRECTORY': str(folder),
               'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
        def invoke(command, at, expected=0):
            result = subprocess.run([sys.executable,'-c',bootstrap,str(self.f.config),at.isoformat(),
                                     command,'--config',str(config),'--db',str(state/'watch.sqlite3'),
                                     '--legacy',str(self.f.runtime)], env=env,
                                    capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode, expected, result.stderr)
            return json.loads(result.stdout)
        invoke('arm', NOW-timedelta(seconds=1))
        first = invoke('job', NOW+timedelta(seconds=1))
        self.assertEqual(first['delivery'], {'sent': 1})
        self.assertEqual(invoke('job', NOW+timedelta(seconds=2))['delivery'], {'sent': 0})
        self.assertEqual(invoke('status', NOW+timedelta(seconds=3))['events'], {'analyzed': 1})
        calls = [json.loads(line) for line in (folder/'calls.jsonl').read_text().splitlines()]
        self.assertEqual([c['stage'] for c in calls],
                         ['auth.list','gmail.messages.search','gmail.labels.list','gmail.get','gpt','jev','gpt','gpt',
                          'auth.list','gmail.messages.search'])
        self.assertNotIn('synthetic-process-password', (folder/'calls.jsonl').read_text())
        # Provider outage on the next NEW mail: truthful review notice, nonzero
        # job result, no final GPT or retry of the failed ID on the next process.
        fixture['listing'] = {'messages': [metadata('abc2', NOW+timedelta(seconds=3))]}
        fixture['message']['message']['id'] = 'abc2'
        fixture['jev'] = {'status': 'unavailable'}
        (folder/'fixture.json').write_text(json.dumps(fixture))
        failed = invoke('job', NOW+timedelta(seconds=4), expected=1)
        self.assertEqual(failed['needs_review'], 1)
        self.assertEqual(failed['delivery'], {'sent': 1})
        self.assertEqual(invoke('job', NOW+timedelta(seconds=5))['processed'], 0)
        self.assertEqual(invoke('status', NOW+timedelta(seconds=6))['events'],
                         {'analyzed': 1, 'needs_review': 1})


if __name__ == '__main__':
    unittest.main()
