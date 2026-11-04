"""Durable local review/reminder actions through OpenClaw's existing Telegram ingress."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys

from assistant import Assistant, SALTA
from notifications import decode
from pipeline import Outbox


def setup(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS notification_tokens (
            message_id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL,
            reviewed INTEGER NOT NULL DEFAULT 0, due TEXT, version INTEGER NOT NULL DEFAULT 0,
            reminder_state TEXT);
        CREATE TABLE IF NOT EXISTS notification_callbacks (
            callback_hash TEXT PRIMARY KEY, result TEXT NOT NULL);
    ''')


def token_for(db, mid):
    db.execute('INSERT OR IGNORE INTO notification_tokens(message_id,token) VALUES (?,?)',
               (mid, secrets.token_hex(12)))
    return db.execute('SELECT token FROM notification_tokens WHERE message_id=?', (mid,)).fetchone()[0]


def enqueue_due(app, now):
    box = Outbox(app)
    with app.db:
        for row in app.db.execute("SELECT * FROM notification_tokens WHERE reviewed=0 AND "
                                  "reminder_state='scheduled' AND due<=?", (now.isoformat(),)).fetchall():
            original = app.db.execute('SELECT payload,route FROM outbox WHERE key=? AND state=\'sent\'',
                                       ('incoming:' + row['message_id'],)).fetchone()
            if original is None or original['route'] != box.route():
                continue
            payload = decode(original['payload'])
            if payload is None:
                continue
            payload['text'] = '⏰ **Recordatorio**\n\n' + payload['text']
            key = f"reminder:{row['message_id']}:{row['version']}"
            box._put(key, 'reminder', json.dumps(payload, ensure_ascii=False), original['route'])
            app.db.execute("UPDATE notification_tokens SET reminder_state='enqueued' WHERE message_id=?",
                           (row['message_id'],))


def apply(config_path, database, event, now=None):
    now = now or datetime.now(timezone.utc)
    cfg = json.loads(Path(config_path).read_text())
    # Runtime supplies this narrow envelope. Never accept identity from callback text.
    if (cfg.get('notification_actions_enabled') is not True
            or not isinstance(event, dict)
            or set(event) != {'channel', 'account', 'chat', 'user', 'authorized', 'is_group',
                              'callback_id', 'message_id', 'data'}
            or event['channel'] != 'telegram' or event['authorized'] is not True
            or event['is_group'] is not False
            or event['account'] != cfg['telegram_account_id'] or 'albert' in event['account'].lower()
            or event['chat'] != cfg['telegram_chat_id'] or event['user'] != cfg['telegram_user_id']
            or event['chat'] != event['user']
            or not isinstance(event['callback_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', event['callback_id'])
            or not isinstance(event['message_id'], str) or not event['message_id'].isdigit()
            or not isinstance(event['data'], str)):
        raise ValueError('action identity rejected')
    match = re.fullmatch(r'bm2:(done|later|1h|tom|cancel):([a-f0-9]{24})', event['data'])
    if not match or Path(database).is_symlink() or not Path(database).is_file():
        raise ValueError('action rejected')
    action, token = match.groups()
    app = Assistant(database, cfg)
    try:
        setup(app.db)
        box = Outbox(app)
        digest = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        with app.db:
            app.db.execute('BEGIN IMMEDIATE')
            previous = app.db.execute('SELECT result FROM notification_callbacks WHERE callback_hash=?',
                                      (digest,)).fetchone()
            if previous:
                return json.loads(previous[0])
            row = app.db.execute('SELECT * FROM notification_tokens WHERE token=?', (token,)).fetchone()
            if row is None:
                raise ValueError('unknown notification')
            mid = row['message_id']
            # Bind each click to a confirmed delivery in this exact private account/chat.
            sent = app.db.execute("SELECT payload FROM outbox WHERE route=? AND state='sent' AND receipt=? "
                                  "AND (key=? OR (kind='reminder' AND key LIKE ?))",
                                  (box.route(), event['message_id'], 'incoming:' + mid,
                                   'reminder:' + mid + ':%')).fetchone()
            if sent is None or decode(sent[0]) is None:
                raise ValueError('notification delivery mismatch')
            url = decode(sent[0])['presentation']['blocks'][0]['buttons'][0]['action']['url']
            status = 'reviewed' if row['reviewed'] else action
            result = {'status': status, 'url': url, 'token': token, 'external_writes': 0}
            if not row['reviewed']:
                if action == 'done':
                    app.db.execute('UPDATE mail SET open=0 WHERE id=?', (mid,))
                    app.db.execute("UPDATE notification_tokens SET reviewed=1,due=NULL,reminder_state='cancelled' "
                                   'WHERE message_id=?', (mid,))
                    app.db.execute("UPDATE outbox SET state='cancelled' WHERE kind='reminder' AND key LIKE ? "
                                   "AND state='ready'", ('reminder:' + mid + ':%',))
                    result['status'] = 'reviewed'
                elif action in ('1h', 'tom'):
                    if action == '1h':
                        due = now + timedelta(hours=1)
                    else:
                        local = now.astimezone(SALTA) + timedelta(days=1)
                        due = local.replace(hour=8, minute=30, second=0, microsecond=0).astimezone(timezone.utc)
                    app.db.execute("UPDATE outbox SET state='cancelled' WHERE kind='reminder' AND key LIKE ? "
                                   "AND state='ready'", ('reminder:' + mid + ':%',))
                    app.db.execute("UPDATE notification_tokens SET due=?,version=version+1,reminder_state='scheduled' "
                                   'WHERE message_id=?', (due.isoformat(), mid))
                    result.update(status='scheduled', due=due.astimezone(SALTA).strftime('%d/%m a las %H:%M'))
            app.db.execute('INSERT INTO notification_callbacks VALUES (?,?)', (digest, json.dumps(result)))
        return result
    finally:
        app.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--db', required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(4097)
        if len(raw) > 4096:
            raise ValueError('oversized event')
        print(json.dumps(apply(args.config, args.db, json.loads(raw))))
        return 0
    except Exception:
        print('{"error":"notification_action_rejected"}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
