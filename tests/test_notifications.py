import copy
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import unittest
from urllib.parse import unquote

from assistant import Assistant
from mail_stages import validate_rendering
from notification_actions import apply, enqueue_due
from notifications import card, gmail_url
from pipeline import OpenClawSender, deliver
import test_mail_watch
from test_mail_watch import CFG, NOW


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.harness = test_mail_watch.WatchTests()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.app, self.watch = self.harness.app, self.harness.watch
        self.harness.capture()
        self.cfg = {**CFG, 'notification_actions_enabled': True}
        self.config = Path(self.harness.tmp.name) / 'config.json'
        self.config.write_text(json.dumps(self.cfg))
        self.config.chmod(0o600)
        self.result = {'summary': 'Solicita revisar el programa.', 'priority': 'media',
                       'notification': {'title': 'Revisión del programa', 'action': 'Enviar comentarios.'}}
        with self.app.db:
            self.watch._notice('abc1', self.result['summary'], self.result, 'test-id@example.invalid')
        class Sender:
            def send(self, row): return '501'
        deliver(self.watch.box, Sender(), NOW)
        self.token = self.app.db.execute('SELECT token FROM notification_tokens').fetchone()[0]
        self.event = {'channel': 'telegram', 'account': 'default', 'chat': CFG['telegram_chat_id'],
                      'user': CFG['telegram_user_id'], 'authorized': True, 'is_group': False,
                      'callback_id': 'callback-1', 'message_id': '501', 'data': f'bm2:done:{self.token}'}

    def action(self, verb='done', event=None, now=NOW):
        value = {**self.event, 'data': f'bm2:{verb}:{self.token}', **(event or {})}
        return apply(self.config, self.harness.db, value, now)

    def test_review_is_local_idempotent_and_preserves_gmail_facts(self):
        before = self.app.db.execute('SELECT facts FROM mail').fetchone()[0]
        self.assertEqual(self.action()['status'], 'reviewed')
        self.assertEqual(self.action()['status'], 'reviewed')
        self.assertEqual(self.app.db.execute('SELECT open FROM mail').fetchone()[0], 0)
        self.assertEqual(self.app.db.execute('SELECT facts FROM mail').fetchone()[0], before)
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM notification_callbacks').fetchone()[0], 1)

    def test_wrong_user_group_account_message_token_or_auth_cannot_change_state(self):
        for change in ({'user': '999'}, {'chat': '999'}, {'is_group': True}, {'authorized': False},
                       {'account': 'albert'}, {'message_id': '999'},
                       {'data': 'bm2:done:' + '0' * 24}, {'data': '/mail_correct abc1 {}'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.action(event=change)
            self.assertEqual(self.app.db.execute('SELECT open FROM mail').fetchone()[0], 1)

    def test_disabled_gate_is_rechecked_per_click(self):
        self.config.write_text(json.dumps({**self.cfg, 'notification_actions_enabled': False}))
        with self.assertRaises(ValueError): self.action()

    def test_choose_later_does_not_schedule_until_duration_selected(self):
        self.assertEqual(self.action('later')['status'], 'later')
        self.assertIsNone(self.app.db.execute('SELECT due FROM notification_tokens').fetchone()[0])
        self.assertEqual(self.action('cancel')['status'], 'cancel')

    def test_hour_reminder_due_once_without_models_or_gmail_refresh(self):
        result = self.action('1h')
        self.assertEqual(result['status'], 'scheduled')
        self.action('1h')  # Same callback is a replay, not a second schedule.
        self.assertEqual(self.app.db.execute('SELECT version FROM notification_tokens').fetchone()[0], 1)
        enqueue_due(self.app, NOW + timedelta(minutes=59))
        self.assertEqual(len(self.watch.box.rows()), 1)
        enqueue_due(self.app, NOW + timedelta(hours=1))
        enqueue_due(self.app, NOW + timedelta(hours=2))
        self.assertEqual(len(self.watch.box.rows()), 2)
        sent = []
        class Sender:
            def send(self, row): sent.append(row); return '502'
        self.assertEqual(deliver(self.watch.box, Sender(), NOW + timedelta(hours=2), kind='reminder'), {'sent': 1})
        self.assertIn('Recordatorio', json.loads(sent[0]['payload'])['text'])
        self.assertEqual(deliver(self.watch.box, Sender(), NOW + timedelta(hours=3), kind='reminder'), {'sent': 0})
        self.assertEqual(self.action(event={'message_id': '502', 'callback_id': 'reminder-click'})['status'], 'reviewed')

    def test_tomorrow_means_salta_0830(self):
        self.action('tom')
        self.assertEqual(self.app.db.execute('SELECT due FROM notification_tokens').fetchone()[0],
                         '2026-10-26T11:30:00+00:00')

    def test_review_cancels_scheduled_and_ready_reminders(self):
        self.action('1h')
        enqueue_due(self.app, NOW + timedelta(hours=1))
        self.action(event={'callback_id': 'review-click'})
        self.assertEqual(dict(self.app.db.execute('SELECT state,count(*) FROM outbox GROUP BY state')),
                         {'cancelled': 1, 'sent': 1})
        enqueue_due(self.app, NOW + timedelta(days=2))
        self.assertEqual(len(self.watch.box.rows()), 2)

    def test_sender_keeps_urls_out_of_message_and_passes_native_presentation(self):
        row = self.watch.box.rows()[0]
        def run(command, **kwargs):
            message = command[command.index('--message') + 1]
            self.assertNotIn('https://', message)
            p = json.loads(command[command.index('--presentation') + 1])
            self.assertEqual([b['label'] for b in p['blocks'][0]['buttons']], ['Abrir', 'Revisado', 'Después'])
            return subprocess.CompletedProcess(command, 0, json.dumps({
                'action': 'send', 'channel': 'telegram', 'dryRun': False,
                'payload': {'ok': True, 'messageId': '501', 'chatId': CFG['telegram_chat_id']}}))
        self.assertEqual(OpenClawSender({'delivery_enabled': True}, run).send(row), '501')

    def test_flat_sent_receipt_and_wrong_chat_rejection(self):
        row = self.watch.box.rows()[0]
        for chat in (CFG['telegram_chat_id'], '999'):
            def run(command, **kwargs):
                return subprocess.CompletedProcess(command, 0, json.dumps({
                    'action': 'send', 'channel': 'telegram', 'dryRun': False,
                    'payload': {'deliveryStatus': 'sent', 'messageId': '501', 'chatId': chat}}))
            sender = OpenClawSender({'delivery_enabled': True}, run)
            if chat == '999':
                with self.assertRaises(RuntimeError): sender.send(row)
            else: self.assertEqual(sender.send(row), '501')

    def test_gmail_link_selects_account_and_rfc_id_instead_of_email_path(self):
        url = gmail_url('person+mail@example.invalid', 'abc1', '<unique@example.invalid>')
        self.assertIn('?authuser=person%2Bmail%40example.invalid', url)
        self.assertIn('#search/rfc822msgid:unique@example.invalid', unquote(url))
        self.assertNotIn('/u/', url)

    def test_informative_has_no_action_and_escapes_email_markup(self):
        value = card({'subject': '[link](https://bad.invalid)', 'sender': '<b>Fake</b>', 'thread_id': 'a1'},
                     {'summary': 'Informa una transferencia.', 'priority': None}, CFG['account'], self.token)
        self.assertIn('🟢 Prioridad baja', value['text'])
        self.assertNotIn('🎯', value['text'])
        self.assertNotIn('<b>', value['text'])
        self.assertNotIn('[link](', value['text'])


class ConciseContracts(unittest.TestCase):
    def setUp(self):
        self.decisions = {'priority': None, 'topic': None, 'promotional': False, 'financial': True}
        self.prepared = {'facts': [{'id': 'f1', 'source': 'body', 'quote': 'Enviamos la transferencia.'},
                                   {'id': 'f2', 'source': 'sender', 'quote': 'Empresa'}],
                         'financial_data': None, 'body_truncated': False}
        self.value = {'title': 'Transferencia informada',
                      'sentences': [{'text': 'La empresa informa el envío de una transferencia.', 'fact_ids': ['f1']}],
                      'action': None, 'decisions': self.decisions}

    def validate(self): return validate_rendering(self.value, self.prepared, self.decisions, 'Transferencia')

    def test_concise_informative_financial_does_not_create_a_task(self):
        result = self.validate()
        self.assertIsNone(result['priority'])
        self.assertIsNone(result['notification']['action'])
        self.assertTrue(result['financial'])

    def test_unknown_or_sender_evidence_priority_override_and_urls_rejected(self):
        for change in ('unknown', 'sender', 'priority', 'url', 'too_long', 'too_many'):
            original = copy.deepcopy(self.value)
            if change == 'unknown': self.value['sentences'][0]['fact_ids'] = ['f99']
            if change == 'sender': self.value['sentences'][0]['fact_ids'] = ['f2']
            if change == 'priority': self.value['decisions'] = {**self.decisions, 'priority': 'alta'}
            if change == 'url': self.value['sentences'][0]['text'] = 'Abrir https://bad.invalid'
            if change == 'too_long': self.value['sentences'][0]['text'] = 'a' * 301
            if change == 'too_many': self.value['sentences'] *= 4
            with self.subTest(change=change), self.assertRaises(ValueError): self.validate()
            self.value = original


if __name__ == '__main__': unittest.main()
