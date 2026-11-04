from datetime import timedelta
import json
import unittest
from urllib.parse import unquote

from assistant import Assistant
from mail_watch import Watch, normalize
import test_mail_watch
from test_mail_watch import NOW, CFG, metadata


class IolSummaryTests(unittest.TestCase):
    def setUp(self):
        self.h = test_mail_watch.WatchTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.app, self.watch = self.h.app, self.h.watch
        self.now = (NOW + timedelta(days=1)).replace(hour=18)  # 15:00 Salta.
        self.sent = []

    def row(self, mid='abc1', op='700001', **changes):
        return normalize({**metadata(mid, self.now),
                          'subject': 'Estado de la Transacción N°' + op,
                          'from': 'no-reply@invertironline.com', **changes})

    def tick(self, rows=(), now=None, sender=None):
        self.h.now = now or self.now
        outer = self
        class Reader:
            def authenticate(self): pass
            def capture(self, *args): return list(rows)
        class Sender:
            def send(self, row):
                outer.sent.append(row)
                return str(500 + len(outer.sent))
        return self.watch.tick(Reader(), lambda _: self.fail('IOL must not read bodies or call models'),
                               sender or Sender(), self.h.now)

    def test_daily_one_notice_counts_unique_operations_not_messages(self):
        self.assertEqual(self.tick([self.row(), self.row('abc2', '700002'),
                                    self.row('abc3')])['delivery']['sent'], 0)
        result = self.tick(now=self.now.replace(hour=21))
        self.assertEqual(result['delivery']['sent'], 1)
        payload = json.loads(self.sent[0]['payload'])
        self.assertTrue(payload['text'].startswith('👤 **InvertirOnline**\n**Resumen de operaciones'))
        self.assertIn('🟢 Prioridad baja · Categoría: Finanzas', payload['text'])
        self.assertIn('Etiqueta sin evaluar', payload['text'])
        self.assertEqual(payload['presentation']['blocks'][0]['buttons'][0]['label'], 'Abrir')
        self.assertIn('2 operaciones informadas', payload['text'])
        self.assertIn('Finanzas', payload['text'])
        self.assertNotIn('700001', payload['text'])
        self.assertNotIn('Sin prioridad', payload['text'])
        self.assertNotIn('ejecutadas', payload['text'])
        url = payload['presentation']['blocks'][0]['buttons'][0]['action']['url']
        self.assertIn('from:no-reply@invertironline.com', unquote(url))
        self.assertEqual(self.watch.status()['events'], {'grouped': 3})
        self.assertEqual(self.tick(now=self.now.replace(hour=21, minute=1))['delivery']['sent'], 0)

    def test_status_updates_next_day_do_not_count_same_operation_twice(self):
        self.tick([self.row()])
        self.tick(now=self.now.replace(hour=21))
        self.tick([self.row('abc2')], now=self.now + timedelta(days=1))
        self.tick(now=self.now.replace(hour=21) + timedelta(days=1))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM iol_summary_operations').fetchone()[0], 1)

    def test_after_cutoff_goes_into_next_summary_without_losing_operation(self):
        self.tick([self.row()])
        self.tick([self.row('abc2', '42')], now=self.now.replace(hour=21))
        self.assertIn('1 operación informada', json.loads(self.sent[0]['payload'])['text'])
        self.tick(now=self.now.replace(hour=21) + timedelta(days=1))
        self.assertEqual(len(self.sent), 2)
        self.assertIn('1 operación informada', json.loads(self.sent[1]['payload'])['text'])

    def test_collection_survives_model_cooldown(self):
        with self.app.db:
            self.app.set_meta('watch_analysis_resume', (self.now + timedelta(days=1)).isoformat())
        self.tick([self.row()])
        self.tick(now=self.now.replace(hour=21))
        self.assertEqual(len(self.sent), 1)

    def test_restart_and_index_overlap_do_not_repeat_delivery(self):
        self.tick([self.row()])
        self.app.close()
        self.h.app = self.app = Assistant(self.h.db, CFG)
        self.h.watch = self.watch = Watch(self.app, clock=lambda: self.h.now)
        self.tick([self.row()], now=self.now.replace(hour=21))
        self.tick([self.row()], now=self.now.replace(hour=21, minute=1))
        self.assertEqual(len(self.sent), 1)

    def test_ambiguous_send_is_not_retried_or_overwritten(self):
        class Timeout:
            def send(self, row): raise TimeoutError('synthetic timeout')
        self.tick([self.row()])
        self.assertEqual(self.tick(now=self.now.replace(hour=21), sender=Timeout())['delivery']['blocked'],
                         'unknown_delivery')
        before = self.watch.box.rows()[0]
        self.tick(now=self.now.replace(hour=21, minute=1))
        self.assertEqual(self.watch.box.rows()[0], before)
        self.assertFalse(self.sent)

    def test_other_senders_subjects_promotions_and_authentication_are_not_grouped(self):
        from iol_notifications import operation
        for changes in ({'from': 'no-reply@invertironline.com.evil.invalid'},
                        {'from': 'other@invertironline.com'}, {'subject': 'IOL novedades'},
                        {'subject': 'Código de acceso 123456'},
                        {'subject': 'Estado de la Transacción N°42 contraseña'},
                        {'labels': ['INBOX', 'CATEGORY_PROMOTIONS']}):
            with self.subTest(changes=changes):
                self.assertIsNone(operation(self.row(**changes)))

    def test_existing_sent_or_ambiguous_deliveries_are_immutable(self):
        self.tick([self.row()])
        for state in ('sent', 'inflight', 'unknown'):
            with self.app.db:
                self.app.db.execute("UPDATE watch_events SET state='queued'")
                self.app.db.execute("DELETE FROM outbox")
                self.app.db.execute("INSERT INTO outbox VALUES ('incoming:abc1','incoming','original',?,?,NULL,'501')",
                                    (self.watch.box.route(), state))
            before = self.watch.box.rows()[0]
            self.watch.iol.collect(self.now)
            self.assertEqual(self.watch.box.rows()[0], before)

    def test_undelivered_queued_individual_is_superseded_without_resend(self):
        self.h.pages['']['messages'] = [{**metadata(), 'subject': 'Estado de la Transacción N°42',
                                         'from': 'no-reply@invertironline.com'}]
        self.h.capture()
        with self.app.db:
            self.watch._notice('abc1', 'Old pending notice')
        self.tick(now=NOW)
        self.assertEqual(self.watch.box.rows()[0]['state'], 'cancelled')
        self.assertEqual(self.watch.status()['events'], {'grouped': 1})

    def test_quiet_mode_waits_for_quiet_and_repeated_status_does_not_delay(self):
        self.app.config = {**CFG, 'iol_summary_mode': 'quiet'}
        self.tick([self.row()])
        self.tick([self.row('abc2', '42')], now=self.now + timedelta(minutes=10))
        self.tick([self.row('abc3', '42')], now=self.now + timedelta(minutes=20))
        self.assertFalse(self.sent)
        self.tick(now=self.now + timedelta(minutes=25))
        self.assertEqual(len(self.sent), 1)
        self.assertIn('2 operaciones informadas', json.loads(self.sent[0]['payload'])['text'])

    def test_delayed_delivery_keeps_count_and_original_summary_date(self):
        self.tick([self.row()])
        self.tick(now=self.now + timedelta(days=2))
        self.assertEqual(len(self.sent), 1)
        self.assertIn('26/10', json.loads(self.sent[0]['payload'])['text'])

    def test_delayed_indexing_keeps_original_mail_time_in_gmail_search(self):
        earlier = self.now - timedelta(hours=2)
        self.tick([self.row(internalDateIso=earlier.isoformat())])
        self.tick(now=self.now.replace(hour=21))
        payload = json.loads(self.sent[0]['payload'])
        url = payload['presentation']['blocks'][0]['buttons'][0]['action']['url']
        self.assertIn('after:' + str(int(earlier.timestamp()) - 600), unquote(url))


if __name__ == '__main__':
    unittest.main()
