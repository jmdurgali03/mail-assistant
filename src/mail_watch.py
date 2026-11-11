"""New-Inbox-only worker. Explicit activation boundary; no historical batch reuse.

Reuses the readonly GPT/Jev/GPT contracts and transactional Telegram outbox.
No passwords in config, argv, database or logs. systemd supplies LoadCredential.
"""
import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
import fcntl
import hashlib
import json
import os
from pathlib import Path
import resource
import re
import stat
import sqlite3
import subprocess
import sys
import uuid

from assistant import (Assistant, AUTH_SUBJECT, ID, authentication_content, iso, text)
from gmail_unlock import Verification
from inbox_readonly import GOG_SHA256, CONFIG_SHA256, json_object
from mail_inbox_batch import InboxItemSession
from mail_readonly import credential_root, auth_error_category
from oneshot import analysis_config, clean_env, private
from pipeline import OpenClawAnalyzer, OpenClawSender, Outbox, deliver
from notification_actions import setup as setup_actions, token_for, enqueue_due
from notifications import card
from iol_notifications import IolSummaries
from protected_notice import context as protected_context, analysis as protected_analysis

CONTRACT = 'new-inbox-gpt-jev-gpt-v1'
QUERY = 'in:inbox -in:spam -in:trash -in:drafts -category:promotions -category:social'
EXCLUDED_LABELS = {'CATEGORY_PROMOTIONS': 'promotions', 'CATEGORY_SOCIAL': 'social'}
SECRET_NAME = 'gmail-keyring-password'
REVIEW_SUMMARY = 'Nuevo correo para revisar en Gmail.'


class WatchError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def now_utc():
    return datetime.now(timezone.utc)


def validate_config(cfg):
    # Dedicated state/contract. Never silently repurpose the historical worker DB.
    if (cfg.get('watch_contract') != CONTRACT or cfg.get('analysis_batch_limit') != 3
            or cfg.get('telegram_account_id') != 'default'
            or not isinstance(cfg.get('telegram_user_id'), str)
            or not re.fullmatch(r'[1-9][0-9]{0,19}', cfg['telegram_user_id'])
            or cfg.get('telegram_chat_id') != cfg['telegram_user_id']
            or cfg.get('analysis_model') != 'openai/gpt-6.1-sol'
            or cfg.get('analysis_profile') != 'bichi-mail-analysis'
            or cfg.get('incoming_notifications') != 'individual_all'
            or cfg.get('iol_summary_mode', 'daily') not in ('daily', 'quiet')
            or cfg.get('initial_sync_mode') != 'baseline'
            or any(cfg.get(k) is not True for k in
                   ('live_reads_enabled', 'analysis_enabled', 'delivery_enabled', 'job_enabled'))
            or cfg.get('correction_bridge_enabled') is not False):
        raise WatchError('watch_config_rejected')


def preflight(cfg, legacy):
    """Local only. No auth command, refresh, message read, inference or delivery."""
    validate_config(cfg)
    root = credential_root(legacy)
    account = private(root / 'gmail-account.pending').read_text().strip()
    if account != cfg.get('account'):
        raise WatchError('credential_account_changed')
    if hashlib.sha256(private(Path(cfg['analysis_config'])).read_bytes()).hexdigest() != CONFIG_SHA256:
        raise WatchError('analysis_config_changed')
    if hashlib.sha256((Path.home() / '.local/bin/gog').read_bytes()).hexdigest() != GOG_SHA256:
        raise WatchError('gog_binary_changed')
    OpenClawAnalyzer(analysis_config(cfg))
    return root


def provider_diagnostics():
    """Metadata-only local inspection. No token values, probes or paid requests."""
    result = {'openai_profile_present': False, 'openai_local_error_count': None,
              'openai_cooldown_until_ms': None, 'openai_disabled_until_ms': None,
              'jev_configured': False, 'quota_live_checked': False}
    profile = 'openai:bichi-mail-analyzer'
    path = Path.home() / '.openclaw-bichi-mail-analysis/agents/bichi-mail-analyzer/agent/openclaw-agent.sqlite'
    if path.exists():
        conn = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)
        try:
            row = conn.execute('''SELECT json_extract(store_json, ?), json_extract(store_json, ?)
                                  FROM auth_profile_store''',
                               ('$.' + 'profiles."' + profile + '".provider',
                                '$.' + 'profiles."' + profile + '".type')).fetchone()
            result['openai_profile_present'] = row == ('openai', 'oauth')
            for output, field in (('openai_local_error_count', 'errorCount'),
                                  ('openai_cooldown_until_ms', 'cooldownUntil'),
                                  ('openai_disabled_until_ms', 'disabledUntil')):
                row = conn.execute('SELECT json_extract(state_json, ?) FROM auth_profile_state',
                                   ('$.usageStats."' + profile + '".' + field,)).fetchone()
                if row and type(row[0]) is int:
                    result[output] = row[0]
        finally:
            conn.close()
    config = Path.home() / '.openclaw/openclaw.json'
    if config.exists():
        value = json_object(config.read_text())
        plugin = value.get('plugins', {}).get('entries', {}).get('typesafe', {})
        selection = (value.get('agents', {}).get('entries', {}).get('main', {}).get('decisionModel')
                     or value.get('agents', {}).get('defaults', {}).get('decisionModel'))
        result['jev_configured'] = (selection == 'typesafe/jev-1.13.0'
            and plugin.get('enabled') is True and not plugin.get('config', {}).get('baseUrl')
            and plugin.get('config', {}).get('apiKey') ==
                {'source': 'store', 'provider': 'default', 'id': 'TYPESAFE_API_KEY'})
    return result


def load_password():
    # Value is read only by the actual job, never by preflight/status/install.
    directory = Path(os.environ.get('CREDENTIALS_DIRECTORY', '/nonexistent'))
    path = directory / SECRET_NAME
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_size > 4096):
        raise WatchError('credential_unavailable')
    value = path.read_text().rstrip('\n')
    if not 16 <= len(value) <= 1024 or '\n' in value or '\r' in value:
        raise WatchError('credential_unavailable')
    return value


class ReadTransport:
    def __init__(self, root, account, password, runner=subprocess.run):
        self.root, self.account, self.password, self.runner = root, account, password, runner

    def call(self, command, args):
        env = clean_env()
        env.update(GOG_HOME=str(self.root / 'gog'), GOG_KEYRING_BACKEND='file',
                   GOG_KEYRING_PASSWORD=self.password)
        argv = [str(Path.home() / '.local/bin/gog'), '--home', str(self.root / 'gog'),
                '--client', 'default', '--account', self.account, '--readonly', '--gmail-no-send',
                '--no-input', '--json', '--enable-commands-exact', command, *args]
        try:
            result = self.runner(argv, env=env, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=45)
            if result.returncode != 0:
                category = auth_error_category(result.stderr)
                raise WatchError(category if category != 'unclassified' else 'read_transport_failed')
            if len(result.stdout.encode()) > 2_000_000:
                raise WatchError('oversized_page')
            value = json_object(result.stdout)
            if 'error' in value or 'errors' in value:
                raise WatchError('read_error_envelope')
            return value
        finally:
            env.pop('GOG_KEYRING_PASSWORD', None)

    def authenticate(self):
        result = self.call('auth.list', ['auth', 'list'])
        rows = result.get('accounts')
        if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) and rows[0].get('error'):
            raise WatchError(auth_error_category(rows[0]['error']))
        Verification(self.root).validate(result, self.account, False)

    def label_catalog(self):
        from notification_labels import catalog
        return catalog(self.call('gmail.labels.list', ['gmail', 'labels', 'list']))

    def capture(self, since, until, boundary):
        # Server time filter plus Gmail internalDate, never the Date header.
        # Ten-minute overlap tolerates indexing delay; IDs dedupe it durably.
        lower = max(boundary, since - timedelta(minutes=10))
        query = QUERY + f' after:{int(lower.timestamp()) - 1} before:{int(until.timestamp()) + 1}'
        token, tokens, rows, seen = '', set(), [], set()
        for _ in range(10):
            args = ['gmail', 'messages', 'search', query, '--max', '100',
                    '--timezone', 'UTC', '--sanitize-content']
            if token:
                args += ['--page=' + token]
            data = self.call('gmail.messages.search', args)
            following = data.get('nextPageToken')
            if following is None:
                following = ''
            if (not isinstance(following, str) or len(following) > 4096
                    or any(ord(ch) < 32 for ch in following) or following in tokens):
                raise WatchError('page_token_invalid')
            page = data.get('messages')
            if 'messages' in data and page is None and not following:
                page = []
            if not isinstance(page, list) or len(page) > 100:
                raise WatchError('page_invalid')
            for row in page:
                # Validate ALL pages before persisting any candidate or cursor.
                facts = normalize(row)
                received = iso(row['internalDateIso'])
                if EXCLUDED_LABELS.keys() & set(facts['labels']):
                    continue
                if boundary <= received <= until and received >= lower and facts['id'] not in seen:
                    seen.add(facts['id'])
                    rows.append(facts)
            if not following:
                return rows
            tokens.add(following)
            token = following
        raise WatchError('capture_page_limit')


def normalize(row):
    if not isinstance(row, dict) or 'body' in row:
        raise WatchError('candidate_invalid')
    mid, tid, labels = row.get('id'), row.get('threadId'), row.get('labels')
    if (not isinstance(mid, str) or not ID.fullmatch(mid)
            or not isinstance(tid, str) or not ID.fullmatch(tid)
            or not isinstance(labels, list) or any(not isinstance(v, str) for v in labels)
            or 'INBOX' not in labels or {'SPAM', 'TRASH', 'DRAFT'}.intersection(labels)):
        raise WatchError('candidate_invalid')
    raw_subject, raw_sender = row.get('subject', ''), row.get('from', '')
    if (not isinstance(raw_subject, str) or not isinstance(raw_sender, str)
            or len(raw_subject) + len(raw_sender) > 32000):
        raise WatchError('candidate_invalid')
    sensitive = authentication_content(raw_subject + ' ' + raw_sender)
    subject, sender = text(raw_subject[:512], 512), text(raw_sender[:500], 500)
    facts = {'id': mid, 'thread_id': tid, 'labels': labels, 'authentication': sensitive,
            'received_at': iso(row['internalDateIso']).isoformat(),
            'subject': AUTH_SUBJECT if sensitive else subject,
            'sender': sender}
    if sensitive:
        notice = protected_context(raw_subject, raw_sender)
        facts.update(sender=notice['sender'], authentication_notice=notice)
    return facts


class ContinuousItem(InboxItemSession):
    concise_notifications = True

    def _model_run(self, args, **kwargs):
        # Inspect only transport diagnostics/error fields, never model final/mail text.
        runner = self.runner
        def observed(*a, **kw):
            result = runner(*a, **kw)
            diagnostic = result.stderr if result.returncode else ''
            try:
                envelope = json.loads(result.stdout)
                if isinstance(envelope, dict) and envelope.get('error'):
                    diagnostic += json.dumps(envelope['error'])
            except (ValueError, TypeError):
                pass
            if re.search(r'usage.limit|rate.limit|quota|too many requests|cooldown|'
                         r'capacity|\b429\b|token.limit', diagnostic, re.I):
                self.receipt['retryable_provider_failure'] = True
                self._persist_receipt()
            return result
        self.runner = observed
        try:
            return super()._model_run(args, **kwargs)
        finally:
            self.runner = runner

    def _gog(self, *args, **kwargs):
        result = super()._gog(*args, **kwargs)
        message = result.get('message', {}) if isinstance(result, dict) else {}
        if isinstance(message, dict):
            h, body = message.get('headers'), message.get('body')
            if (isinstance(h, dict) and isinstance(body, str)
                    and all(isinstance(h.get(k, ''), str) for k in ('subject', 'from'))
                    and authentication_content(h.get('subject', '') + ' ' + h.get('from', '') + ' ' + body)):
                self.authentication_notice = protected_context(h.get('subject', ''), h.get('from', ''), body)
        headers = result.get('message', {}).get('headers', {}) if isinstance(result, dict) else {}
        if isinstance(headers, dict):
            for name, value in headers.items():
                if (name.lower() == 'message-id' and isinstance(value, str)
                        and re.fullmatch(r'<?[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+>?', value)
                        and len(value) <= 254):
                    self.rfc_message_id = value.strip('<>')
        return result

    def read_selected(self, row, password, account):
        if row.get('authentication_notice'):
            self.validate_candidate(row)
            self.authentication_notice = row['authentication_notice']
            self.receipt['authentication_protected'] = True
        else:
            result = super().read_selected(row, password, account)
            if not self.receipt.get('authentication_protected'):
                return result
        notice = getattr(self, 'authentication_notice', None) or protected_context(
            row.get('subject', ''), row.get('from', ''))
        self.authentication_notice = notice
        self.analysis = protected_analysis(notice)
        self._persist_receipt()
        return dict(self.analysis)

    def __init__(self, root, config, save, **kwargs):
        self.save = save
        super().__init__(root, config, '/unused-watch-state', **kwargs)

    def _persist_receipt(self):
        # Only inherited counters/statuses, never content, prompts or password.
        self.save(self.receipt)


class Watch:
    def __init__(self, app, clock=None):
        validate_config(app.config)
        self.app, self.db, self.box = app, app.db, Outbox(app)
        self.clock = clock or now_utc
        self.notification_labels = None
        setup_actions(self.db)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS watch_events (
                message_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                category TEXT, receipt TEXT);
        ''')
        self.db.execute('CREATE TABLE IF NOT EXISTS watch_recovery_members '
                        '(batch TEXT, message_id TEXT, PRIMARY KEY(batch,message_id))')
        self.iol = IolSummaries(app, self.box)

    def arm(self, now):
        """Explicit local activation, atomic boundary persisted before first read."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.app.meta('watch_contract'):
                self._bound()
                return False  # Restarts NEVER reset the boundary.
            if any(self.db.execute('SELECT 1 FROM ' + table).fetchone() for table in
                   ('mail', 'outbox', 'assessments', 'notices', 'watch_events')):
                raise WatchError('nonempty_watch_database')
            self.app.set_meta('watch_contract', CONTRACT)
            self.app.set_meta('watch_route', self.box.route())
            self.app.set_meta('watch_boundary', now.isoformat())
            self.app.set_meta('watch_cursor', now.isoformat())
            self.app.set_meta('topics', [])
        return True

    def _bound(self):
        if (self.app.meta('watch_contract') != CONTRACT
                or self.app.meta('watch_route') != self.box.route()
                or not self.app.meta('watch_boundary')):
            raise WatchError('watch_not_armed_or_route_changed')

    def capture(self, reader, now):
        self._bound()
        cursor, boundary = iso(self.app.meta('watch_cursor')), iso(self.app.meta('watch_boundary'))
        if now < cursor:
            raise WatchError('clock_rollback')
        rows = reader.capture(cursor, now, boundary)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for facts in rows:
                if EXCLUDED_LABELS.keys() & set(facts['labels']):
                    continue
                mid = facts['id']
                if self.db.execute('SELECT 1 FROM watch_events WHERE message_id=?', (mid,)).fetchone():
                    continue
                self.db.execute("INSERT INTO watch_events VALUES (?, 'queued', NULL, NULL)", (mid,))
                self.db.execute("INSERT INTO mail VALUES (?,?,'{}',1)", (mid, json.dumps(facts)))
                self.db.execute("INSERT INTO notices VALUES (?, 'awaiting_analysis')", (mid,))
            self.app.set_meta('watch_cursor', now.isoformat())
            self.app.set_meta('observed_at', now.isoformat())
            self.app.set_meta('watch_read_resume', None)
            self.app.set_meta('watch_last_read_failure', None)
        return sum(not (EXCLUDED_LABELS.keys() & set(facts['labels'])) for facts in rows)

    def save_receipt(self, mid, receipt):
        with self.db:
            self.db.execute('UPDATE watch_events SET receipt=? WHERE message_id=?',
                            (json.dumps(receipt), mid))

    def _notice(self, mid, summary, result=None, rfc_message_id=None):
        facts = json.loads(self.db.execute('SELECT facts FROM mail WHERE id=?', (mid,)).fetchone()[0])
        if EXCLUDED_LABELS.keys() & set(facts['labels']):
            return
        name, address = parseaddr(facts['sender']) if ('<' in facts['sender'] or '@' in facts['sender']) else ('', '')
        if not facts.get('authentication'):
            facts['sender'] = text(name or address or facts['sender'] or 'Correo', 100)
        result = result or {'summary': summary, 'priority': None, 'review_required': True}
        payload = card(facts, result, self.app.config['account'], token_for(self.db, mid), rfc_message_id)
        self.box._put('incoming:' + mid, 'incoming', json.dumps(payload, ensure_ascii=False), self.box.route())

    def exclude_categories(self):
        """Apply the policy to pre-existing queue/outbox; never replay or delete.

        Claimed/ambiguous/sent deliveries remain immutable for reconciliation.
        Only known metadata is inspected: no body reads or model calls.
        """
        with self.db:
            for row in self.db.execute('SELECT id,facts FROM mail').fetchall():
                labels = json.loads(row['facts']).get('labels', [])
                category = next((value for label, value in EXCLUDED_LABELS.items() if label in labels), None)
                if category is None:
                    continue
                self.db.execute("UPDATE watch_events SET state='excluded',category=? "
                                "WHERE message_id=? AND state IN ('queued','processing')", (category, row['id']))
                self.db.execute("UPDATE outbox SET state='cancelled' "
                                "WHERE key=? AND state='ready'", ('incoming:' + row['id'],))

    def recover(self):
        self.exclude_categories()
        # Claimed inference may have cost money before a crash: never replay it.
        with self.db:
            for row in self.db.execute("SELECT message_id FROM watch_events WHERE state='processing'").fetchall():
                self.db.execute("UPDATE watch_events SET state='needs_review', category='interrupted' "
                                'WHERE message_id=?', (row[0],))
                self._notice(row[0], REVIEW_SUMMARY)

    def track_recovery(self):
        batch = self.app.meta('watch_recovery_batch')
        if batch:
            self.db.execute("INSERT OR IGNORE INTO watch_recovery_members "
                            "SELECT ?,message_id FROM watch_events WHERE state='queued'", (batch,))

    def prepare_recovery_report(self):
        batch = self.app.meta('watch_recovery_batch')
        if not batch:
            return
        rows = self.db.execute("SELECT e.message_id,e.state,o.payload FROM watch_recovery_members m "
                               "JOIN watch_events e ON e.message_id=m.message_id "
                               "LEFT JOIN outbox o ON o.key='incoming:'||e.message_id "
                               "WHERE m.batch=? ORDER BY e.rowid", (batch,)).fetchall()
        if not rows or any(r['state'] in ('queued', 'processing') for r in rows):
            return
        analyzed = sum(r['state'] == 'analyzed' for r in rows)
        review = sum(r['state'] == 'needs_review' for r in rows)
        lines = [f'📬 Reporte de recuperación: {len(rows)} correos pendientes. '
                 f'{analyzed} analizados/protegidos, {review} para revisión. '
                 'Los grupos financieros y exclusiones conservan su tratamiento habitual.']
        for row in rows:
            if row['payload']:
                from notifications import decode
                notice = decode(row['payload'])
                if notice:
                    lines.append(notice['text'])
        chunks, current = [], ''
        for line in lines:
            if len(current) + len(line) + 2 > 3000:
                chunks.append(current)
                current = ''
            current += ('\n\n' if current else '') + line
        if current:
            chunks.append(current)
        with self.db:
            for index, chunk in enumerate(chunks):
                self.box._put(f'recovery:{batch}:{index:06d}', 'recovery', chunk, self.box.route())
            self.app.set_meta('watch_recovery_batch', None)

    def process(self, factory):
        self.recover()
        count, failures, reviews = 0, 0, 0
        for event in self.db.execute("SELECT message_id FROM watch_events WHERE state='queued' "
                                     'ORDER BY rowid LIMIT 3').fetchall():
            mid = event[0]
            # Durable claim before invocation; only explicit provider failures retry.
            with self.db:
                self.db.execute("UPDATE watch_events SET state='processing' WHERE message_id=?", (mid,))
            item = factory(lambda receipt: self.save_receipt(mid, receipt))
            item.notification_labels = self.notification_labels
            facts = json.loads(self.db.execute('SELECT facts FROM mail WHERE id=?', (mid,)).fetchone()[0])
            row = {'id': mid, 'labels': facts['labels'], 'subject': facts['subject'], 'from': facts['sender']}
            if facts.get('authentication_notice'):
                row['authentication_notice'] = facts['authentication_notice']
            result = None
            try:
                result = item.read_selected(row, item.watch_password, self.app.config['account'])
                if not item.receipt.get('authentication_protected') and not item._functional_passed():
                    raise WatchError('pipeline_not_verified')
                summary, state, category = result['summary'], 'analyzed', None
            except (RuntimeError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
                result = None
                summary, state = REVIEW_SUMMARY, 'needs_review'
                category = item.failure or 'message_unavailable_or_invalid'
                failures += 1
                decision = item.receipt.get('decision', {})
                if (item.receipt.get('retryable_provider_failure')
                        or (category == 'decision_needs_review'
                            and decision.get('reason') in ('timeout', 'gateway_unavailable'))):
                    state, category = 'queued', 'provider_unavailable'
            finally:
                # Persist only fixed descriptions and safe identity, never raw secrets.
                if item.receipt.get('authentication_protected'):
                    notice = getattr(item, 'authentication_notice', None) or protected_context(
                        facts['subject'], facts['sender'])
                    facts.update(subject=notice['title'], sender=notice['sender'], authentication=True,
                                 authentication_notice=notice)
                    with self.db:
                        self.db.execute('UPDATE mail SET facts=? WHERE id=?', (json.dumps(facts), mid))
                item.prepared = item.preparation_source = item.analysis = None
                item.watch_password = None
            with self.db:
                self.db.execute('UPDATE watch_events SET state=?,category=? WHERE message_id=?',
                                (state, category, mid))
                if state == 'queued':
                    if not self.app.meta('watch_recovery_batch'):
                        self.app.set_meta('watch_recovery_batch', uuid.uuid4().hex)
                    self.track_recovery()
                else:
                    self._notice(mid, summary, result, getattr(item, 'rfc_message_id', None))
            count += 1
            reviews += state == 'needs_review'
            if failures:
                # Circuit-breaker; quota queue retries after cooldown, never delivery replay.
                with self.db:
                    self.app.set_meta('watch_analysis_resume', (self.clock() + timedelta(minutes=15)).isoformat())
                break
        return {'processed': count, 'needs_review': reviews}

    def tick(self, reader, factory, sender, now):
        self._bound()
        enqueue_due(self.app, now)
        reminders = deliver(self.box, sender, self.clock(), clock=self.clock, kind='reminder')
        blocked = self.app.meta('watch_blocked')
        if blocked:
            raise WatchError(blocked)
        resume = self.app.meta('watch_read_resume')
        if resume and now < iso(resume):
            return {'status': 'read_cooldown', 'delivery': reminders}
        try:
            reader.authenticate()
            captured = self.capture(reader, now)
        except Exception as exc:
            category = exc.category if isinstance(exc, WatchError) else 'capture_or_auth_failed'
            with self.db:
                self.app.set_meta('watch_last_read_failure', category)
                # Ambiguous capture never advances cursor or triggers analysis.
                if category.startswith(('keyring_', 'oauth_', 'google_', 'credential_')):
                    self.app.set_meta('watch_blocked', category)
                else:
                    self.app.set_meta('watch_read_resume', (now + timedelta(minutes=15)).isoformat())
            raise WatchError(category) from None
        with self.db:
            self.track_recovery()
        resume = self.app.meta('watch_analysis_resume')
        # Deterministic financial counts remain available during model cooldown.
        self.iol.collect(now)
        result = {'processed': 0, 'needs_review': 0}
        if not resume or now >= iso(resume):
            # One readonly catalog per analysis cycle, not per message or empty poll.
            # Failure never reuses a stale catalog or invents an existing label.
            self.notification_labels = None
            if self.db.execute("SELECT 1 FROM watch_events WHERE state='queued' LIMIT 1").fetchone():
                try:
                    self.notification_labels = reader.label_catalog()
                except (AttributeError, WatchError, ValueError, OSError, TypeError, KeyError):
                    pass  # Summary still works; card explicitly says label not evaluated.
            result = self.process(factory)
        self.exclude_categories()  # Also protects ready outbox during analysis cooldown.
        self.iol.prepare(now)
        self.prepare_recovery_report()
        delivery = deliver(self.box, sender, self.clock(), clock=self.clock)
        delivery['sent'] += reminders['sent']
        if reminders.get('blocked'):
            delivery['blocked'] = reminders['blocked']
        result.update(captured=captured, delivery=delivery)
        return result

    def status(self):
        return {'contract': self.app.meta('watch_contract'),
                'boundary': self.app.meta('watch_boundary'), 'cursor': self.app.meta('watch_cursor'),
                'blocked': self.app.meta('watch_blocked'),
                'last_read_failure': self.app.meta('watch_last_read_failure'),
                'read_resume': self.app.meta('watch_read_resume'),
                'analysis_resume': self.app.meta('watch_analysis_resume'),
                'recovery_batch_active': bool(self.app.meta('watch_recovery_batch')),
                'events': dict(self.db.execute('SELECT state,count(*) FROM watch_events GROUP BY state')),
                'outbox': dict(self.db.execute('SELECT state,count(*) FROM outbox GROUP BY state'))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['preflight', 'diagnostics', 'arm', 'status', 'job', 'clear-block'])
    parser.add_argument('--config', required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--legacy', default=str(Path.home() / '.config/bichi-mail'))
    args = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    app, lock, password = None, None, None
    try:
        cfg = json_object(private(Path(args.config)).read_text())
        root = preflight(cfg, args.legacy)
        if args.command == 'diagnostics':
            print(json.dumps(provider_diagnostics(), sort_keys=True))
            return 0
        if args.command == 'preflight':
            print(json.dumps({'local_preflight': 'ok', 'network_checked': False,
                              'credential_source_present': (Path(args.legacy) / SECRET_NAME).exists()}))
            return 0
        db = Path(args.db)
        if db.name != 'watch.sqlite3':
            raise WatchError('dedicated_watch_database_required')
        db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private(db.parent, True)
        lock = os.open(str(db) + '.pipeline.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app = Assistant(db, cfg)
        watch = Watch(app)
        if args.command == 'arm':
            result = {'new_boundary': watch.arm(now_utc()), **watch.status()}
        elif args.command == 'clear-block':
            # Operator-only recovery after fixing credentials/provider. No reset,
            # inference retry, delivery retry or external operation.
            with app.db:
                for key in ('watch_blocked', 'watch_read_resume', 'watch_analysis_resume'):
                    app.set_meta(key, None)
            result = watch.status()
        elif args.command == 'status':
            result = watch.status()
        else:
            password = load_password()
            reader = ReadTransport(root, cfg['account'], password)
            def factory(save):
                item = ContinuousItem(root, cfg['analysis_config'], save)
                item.watch_password = password
                return item
            result = watch.tick(reader, factory, OpenClawSender(cfg), now_utc())
        print(json.dumps(result, sort_keys=True))
        if result.get('needs_review') or (isinstance(result.get('delivery'), dict)
                                        and result['delivery'].get('blocked')):
            print('{"status":"needs_review","category":"analysis_or_delivery_unconfirmed"}',
                  file=sys.stderr)
            return 1
        return 0
    except Exception as exc:
        # Fixed categories only; no subprocess/SQL/JSON/getpass exception text.
        print(json.dumps({'status': 'blocked', 'category': exc.category if isinstance(exc, WatchError)
                          else 'local_or_runtime_validation_failed'}), file=sys.stderr)
        return 1
    finally:
        password = None
        if app:
            app.close()
        if lock is not None:
            os.close(lock)


if __name__ == '__main__':
    raise SystemExit(main())
