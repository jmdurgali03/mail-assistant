"""Deterministic, persistent IOL operation summaries; no body reads or models.

Finanzas is a notification category, not a Gmail label mutation. Counts refer
to operations reported by email, not verified market executions.
"""
from datetime import timedelta, timezone
from email.utils import parseaddr
import json
import re
from urllib.parse import quote, urlencode

from assistant import SALTA, iso
from notifications import FORMAT

SUBJECT = re.compile(r'Estado de la Transacci[oó]n\s+N\s*[°ºo.]?\s*([0-9]{1,20})', re.I)


def operation(facts):
    if facts.get('authentication') or 'CATEGORY_PROMOTIONS' in facts.get('labels', []):
        return None
    if parseaddr(facts.get('sender', ''))[1].lower() != 'no-reply@invertironline.com':
        return None
    match = SUBJECT.fullmatch(facts.get('subject', '').strip())
    return match[1] if match else None


class IolSummaries:
    def __init__(self, app, box):
        self.app, self.db, self.box = app, app.db, box
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS iol_summary_groups (
                id TEXT PRIMARY KEY, due TEXT NOT NULL, started TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS iol_summary_operations (
                operation_id TEXT PRIMARY KEY, group_id TEXT NOT NULL);
        ''')

    def collect(self, now):
        """Only unclaimed new events. Sent/ambiguous historical notices are untouched."""
        mode = self.app.config.get('iol_summary_mode', 'daily')
        if mode not in ('daily', 'quiet'):
            raise ValueError('invalid IOL summary mode')
        local = now.astimezone(SALTA)
        due = local.replace(hour=18, minute=0, second=0, microsecond=0)
        if due <= local:
            due += timedelta(days=1)
        group = 'daily:' + due.date().isoformat()
        with self.db:
            for row in self.db.execute("SELECT m.id,m.facts FROM mail m JOIN watch_events w "
                                       "ON w.message_id=m.id WHERE w.state='queued'").fetchall():
                facts = json.loads(row['facts'])
                op = operation(facts)
                if op is None:
                    continue
                # Never supersede any already-claimed individual delivery.
                previous = self.db.execute('SELECT state FROM outbox WHERE key=?',
                                           ('incoming:' + row['id'],)).fetchone()
                if previous and previous[0] not in ('ready', 'cancelled'):
                    continue
                known = self.db.execute('SELECT 1 FROM iol_summary_operations WHERE operation_id=?',
                                        (op,)).fetchone()
                if not known:
                    if mode == 'quiet':
                        pending = self.db.execute("SELECT g.id FROM iol_summary_groups g LEFT JOIN outbox o "
                                                  "ON o.key='iol-summary:'||g.id WHERE g.id LIKE 'quiet:%' "
                                                  "AND o.key IS NULL ORDER BY g.started DESC LIMIT 1").fetchone()
                        group = pending[0] if pending else 'quiet:' + now.isoformat()
                        due = now + timedelta(minutes=15)
                    start = min(now, iso(facts.get('received_at', now.isoformat())))
                    self.db.execute('INSERT INTO iol_summary_groups VALUES (?,?,?) ON CONFLICT(id) '
                                    'DO UPDATE SET due=excluded.due,started=MIN(started,excluded.started)',
                                    (group, due.astimezone(timezone.utc).isoformat(),
                                     start.astimezone(timezone.utc).isoformat()))
                    self.db.execute('INSERT INTO iol_summary_operations VALUES (?,?)', (op, group))
                facts['notification_category'] = 'Finanzas'
                self.db.execute('UPDATE mail SET facts=? WHERE id=?', (json.dumps(facts), row['id']))
                self.db.execute("UPDATE watch_events SET state='grouped',category='iol_finance' "
                                'WHERE message_id=?', (row['id'],))
                self.db.execute("UPDATE notices SET state='grouped' WHERE message_id=?", (row['id'],))
                self.db.execute("UPDATE outbox SET state='cancelled' WHERE key=? AND state='ready'",
                                ('incoming:' + row['id'],))

    def prepare(self, now):
        with self.db:
            for group in self.db.execute("SELECT g.* FROM iol_summary_groups g LEFT JOIN outbox o "
                                         "ON o.key='iol-summary:'||g.id WHERE g.due<=? "
                                         "AND (o.key IS NULL OR o.state IN ('ready','cancelled'))",
                                         (now.astimezone(timezone.utc).isoformat(),)).fetchall():
                count = self.db.execute('SELECT count(*) FROM iol_summary_operations WHERE group_id=?',
                                        (group['id'],)).fetchone()[0]
                if not count:
                    continue
                # Search by observed interval; no transaction IDs, amounts or credentials in the card.
                start, end = iso(group['started']), iso(group['due'])
                query = ('from:no-reply@invertironline.com subject:"Estado de la Transacción" '
                         f'after:{int(start.timestamp()) - 600} before:{int(end.timestamp()) + 1}')
                url = ('https://mail.google.com/mail/?' + urlencode({'authuser': self.app.config['account']})
                       + '#search/' + quote(query, safe=''))
                label = end.astimezone(SALTA).strftime('%d/%m')
                noun = 'operación informada' if count == 1 else 'operaciones informadas'
                payload = {'format': FORMAT,
                           'text': f'👤 **InvertirOnline**\n**Resumen de operaciones · {label}**\n\n'
                                   '🟢 Prioridad baja · Categoría: Finanzas\n🏷️ Etiqueta sin evaluar\n\n'
                                   f'{count} {noun} por IOL.',
                           'presentation': {'blocks': [{'type': 'buttons', 'buttons': [
                               {'label': 'Abrir', 'action': {'type': 'url', 'url': url}}]}]}}
                self.box._put('iol-summary:' + group['id'], 'iol-summary',
                              json.dumps(payload, ensure_ascii=False), self.box.route())
