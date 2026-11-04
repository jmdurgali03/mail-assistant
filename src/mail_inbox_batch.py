"""One private unlock, paginated Inbox snapshot, readonly per-message analysis.

No worker, OAuth reconnection, mutation, delivery, resume or application retry. Mail content is
kept only in RAM; the private handoff retains bounded validated summaries, not
bodies/prompts/headers. Receipts contain counters and fixed diagnostics only.
"""
import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import resource
import sys
import time
import warnings

from gmail_consent import ConsentError
from jev_decisions import JevDecider
from mail_readonly import MailSession, MESSAGES, credential_root
from oneshot import private

QUERY = 'in:inbox -category:promotions -category:social'  # Read or unread, excluding both categories.
EXCLUDED_LABELS = frozenset({'CATEGORY_PROMOTIONS', 'CATEGORY_SOCIAL'})
PAGE_SIZE = 100
RETENTION_SECONDS = 24 * 3600
SESSION = re.compile(r'inbox-[a-f0-9]{24}\Z')


class InboxItemSession(MailSession):
    def _eligible_message(self, labels):
        return (isinstance(labels, list) and all(isinstance(v, str) for v in labels)
                and 'INBOX' in labels and not EXCLUDED_LABELS.intersection(labels))


class InboxBatchSession(InboxItemSession):
    def __init__(self, *args, decider_factory=JevDecider, wall_clock=time.time, **kwargs):
        super().__init__(*args, **kwargs)
        self.decider_factory, self.wall_clock = decider_factory, wall_clock
        self.receipt.update(contract='mail_inbox_batch_v1', snapshot_complete=False,
                            snapshot_unique=0, snapshot_rows=0, snapshot_duplicates=0,
                            processed=0, analyzed=0, authentication_protected_count=0,
                            needs_review_count=0, jev_invocations=0, current_index=None,
                            body_truncated_count=0, attachments_omitted_count=0,
                            batch_complete=False, retention_seconds=RETENTION_SECONDS,
                            snapshot_page_diagnostics=[],
                            counts_unit='application_invocations',
                            transport_retries_disabled=False)

    def _gog_diagnostic(self, command, counter):
        if command == 'gmail.messages.search' and re.fullmatch(r'page_[0-9]{6,}_invocations', counter):
            diagnostic = self.receipt['snapshot_page_diagnostics'][-1]
            diagnostic['outcome'] = 'runner_error'
            return diagnostic
        return super()._gog_diagnostic(command, counter)

    def _snapshot(self, password, account):
        self._phase('snapshotting')
        rows, seen, tokens = [], set(), set()
        token = ''
        while True:
            number = self.receipt['metadata_search_invocations'] + 1
            counter = f'page_{number:06d}_invocations'
            self.receipt[counter] = 0
            diagnostic = {'page': number, 'outcome': 'reserved',
                          'returncode': None, 'stderr_present': None}
            self.receipt['snapshot_page_diagnostics'].append(diagnostic)
            # Persist a reservation before even a metadata request; no resumption.
            self.receipt['page_reserved'] = number
            self._persist_receipt()
            args = ['gmail', 'messages', 'search', QUERY, '--max', str(PAGE_SIZE),
                    '--timezone', 'UTC', '--sanitize-content']
            if token:
                # Opaque, untrusted tokens cannot become another CLI option.
                args += ['--page=' + token]
            try:
                listing = self._gog(password, account, 'gmail.messages.search', args, counter)
                # An explicit nil slice is an empty terminal page only, never a
                # missing payload, an error envelope or an unfinished snapshot.
                diagnostic['outcome'] = 'error_envelope_rejected'
                if 'error' in listing or 'errors' in listing:
                    raise ValueError('snapshot error envelope')
                diagnostic['outcome'] = 'token_invalid'
                following = listing.get('nextPageToken')
                if following is None:
                    following = ''
                if (not isinstance(following, str) or len(following) > 4096
                        or any(ord(ch) < 32 for ch in following)):
                    raise ValueError('invalid snapshot token')
                diagnostic['outcome'] = 'token_repeated'
                if following and following in tokens:
                    raise ValueError('repeated snapshot token')
                diagnostic['outcome'] = 'page_invalid'
                page = listing.get('messages')
                terminal_null = 'messages' in listing and page is None and not following
                if terminal_null:
                    page = []
                if not isinstance(page, list) or len(page) > PAGE_SIZE:
                    raise ValueError('invalid snapshot page')
                diagnostic.update(outcome='candidate_invalid', rows_received=len(page), rows_validated=0)
                # Validate the whole page before counting/merging it into the
                # snapshot; a malformed suffix cannot leave half a page accepted.
                for row in page:
                    if (isinstance(row, dict) and isinstance(row.get('labels'), list)
                            and EXCLUDED_LABELS.intersection(row['labels'])):
                        continue
                    self.validate_candidate(row)
                    diagnostic['rows_validated'] += 1
                for row in page:
                    if EXCLUDED_LABELS.intersection(row.get('labels', [])):
                        continue
                    self.receipt['snapshot_rows'] += 1
                    if row['id'] in seen:
                        self.receipt['snapshot_duplicates'] += 1
                        continue
                    seen.add(row['id'])
                    # Never retain recipients, dates, snippets or attachment metadata.
                    rows.append({k: row[k] for k in ('id', 'labels', 'subject', 'from') if k in row})
                    self.receipt['snapshot_unique'] += 1
                diagnostic['outcome'] = 'accepted_terminal_null' if terminal_null else 'accepted'
            except Exception:
                self.failure = 'snapshot_needs_review'
                # A pre-invocation deadline failure must not look like an HTTP attempt.
                if diagnostic['outcome'] == 'reserved' and self.expire():
                    diagnostic['outcome'] = 'deadline_expired'
                raise
            finally:
                self.receipt['metadata_search_invocations'] += self.receipt[counter]
                self._persist_receipt()
            if not following:
                self.receipt['snapshot_complete'] = True
                self._persist_receipt()
                return rows
            tokens.add(following)
            token = following

    def _functional_passed(self):
        return (self.receipt['batch_complete'] and self.receipt['needs_review_count'] == 0)

    def execute(self, form):
        super().execute(form)
        if self.receipt['needs_review_count']:
            self.receipt['needs_review'] = True
            self._phase('completed_with_review')

    def _item(self, row, index, password, account):
        item = InboxItemSession(self.root, self.config_path, self.state_dir / f'item-{index:06d}',
                                runner=self.runner, clock=self.clock, decider=self.decider_factory())
        # Each message has its own 10-minute stage window, never another unlock.
        item.local_preflight()
        item._claim()
        item.attempted = True
        record = {'index': index, 'message_id': row['id'], 'status': 'needs_review'}
        self.receipt['current_index'] = index
        self._persist_receipt()
        try:
            item._phase('reading_reserved')
            item.read_selected(row, password, account)
            item._within_window()
            protected = item.receipt.get('authentication_protected') is True
            if not protected and not item._functional_passed():
                raise ValueError('unverified pipeline')
            record.update(status='authentication_protected' if protected else 'ok',
                          analysis=item.analysis, body_truncated=item.receipt.get('body_truncated', False),
                          attachments_omitted=item.receipt.get('attachments_omitted', False))
            item.receipt.update(analysis_available=True, functional_passed=not protected)
            item._phase('completed')
        except Exception:
            record = {'index': index, 'message_id': row['id'], 'status': 'needs_review',
                      'failure': item.failure or 'message_unavailable_or_invalid'}
            item.receipt.update(needs_review=True, functional_passed=False,
                                failure_category=record['failure'], analysis_available=False)
            item._phase('needs_review')
        finally:
            self.receipt['body_get_invocations'] += item.receipt['body_get_invocations']
            self.receipt['model_invocations'] += item.receipt['model_invocations']
            self.receipt['jev_invocations'] += item.decider.metadata['invocations']
            self.receipt['body_truncated_count'] += int(item.receipt.get('body_truncated', False))
            self.receipt['attachments_omitted_count'] += int(item.receipt.get('attachments_omitted', False))
            self.receipt['diagnostic_review_required'] = (
                self.receipt.get('diagnostic_review_required', False)
                or item.receipt.get('diagnostic_review_required', False))
            item.prepared = item.preparation_source = item.analysis = None
            item.clear(force=True)
            self._persist_receipt()
        return record

    def read_inbox(self, password, account):
        rows = self._snapshot(password, account)
        # Bound total work by the finite deduplicated snapshot, not the original
        # single-message deadline. Stages still enforce their per-message window.
        self.deadline = self.clock() + max(600, len(rows) * 600 + 60)
        self.receipt.update(output_created_at=int(self.wall_clock()),
                            output_expires_at=int(self.wall_clock()) + RETENTION_SECONDS)
        self._phase('analyzing_snapshot')
        fd = os.open(self.state_dir / 'analysis.jsonl',
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w') as stream:
                for index, row in enumerate(rows, 1):
                    self._within_window()
                    record = self._item(row, index, password, account)
                    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                    self.receipt['processed'] += 1
                    counter = {'ok': 'analyzed', 'needs_review': 'needs_review_count',
                               'authentication_protected': 'authentication_protected_count'}[record['status']]
                    self.receipt[counter] += 1
                    self._persist_receipt()
        finally:
            rows.clear()
        self.receipt.update(batch_complete=True, current_index=None,
                            output_expires_at=int(self.wall_clock()) + RETENTION_SECONDS)
        self.analysis = None  # Batch result exists only in the private handoff.
        self._persist_receipt()


def run_terminal(state):
    if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        print('Usá una terminal privada con ssh -t, sin redirecciones.', file=sys.stderr)
        return 2
    print('Analizar Inbox (leídos y no leídos), excepto Promociones; solo lectura.')
    print('Un desbloqueo; hasta 2 GPT + 1 Jev por mensaje. Sin reintentos de aplicación ni reconexión OAuth.')
    print('El transporte mantenido puede reintentar HTTP internamente; los contadores son invocaciones.')
    print('La salida mínima privada se comparte con Bichi; purgar al terminar la síntesis.')
    form = {}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            form['password'] = [getpass.getpass('Contraseña del almacén (no se muestra): ')]
        state.execute(form)
        print('Ejecución terminada. ' + json.dumps({k: state.receipt[k] for k in
              ('snapshot_unique', 'processed', 'analyzed', 'authentication_protected_count',
               'needs_review_count', 'metadata_search_invocations', 'body_get_invocations',
               'model_invocations', 'jev_invocations')}, sort_keys=True))
        return 0 if state.receipt['functional_passed'] else 1
    except getpass.GetPassWarning:
        print('No se pudo desactivar el eco; no ingreses la contraseña.', file=sys.stderr)
        return 2
    except ConsentError:
        print(MESSAGES.get(state.failure, 'Operación detenida. No repetir; reconciliar recibo con Bichi.'),
              file=sys.stderr)
        return 1
    finally:
        form.clear()
        state.clear(force=True)
        print('Directorio privado para Bichi: ' + str(state.state_dir))


def handoff(directory, action, now=time.time):
    """Private coordinator access/cleanup, never a mailbox/model invocation."""
    private(directory, True)
    output = private(directory / 'analysis.jsonl')
    if action == 'purge':
        output.unlink()
        print('Salida privada purgada; recibos técnicos conservados.')
        return 0
    receipt = json.loads(private(directory / 'result.json').read_text())
    if now() >= receipt['output_expires_at']:
        output.unlink()
        print('Salida vencida y purgada; no volver a leer Gmail.', file=sys.stderr)
        return 1
    # Output is data, never instructions. Only for the authorized private session.
    sys.stdout.write(output.read_text())
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'run', 'show', 'purge'))
    parser.add_argument('--session', required=True, help='inbox- followed by 24 lowercase hex digits')
    parser.add_argument('--accept-internal-transport-retries', action='store_true',
                        help='Only after resolving the no-HTTP-retries scope with the coordinator')
    args = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if not SESSION.fullmatch(args.session):
        parser.error('invalid session name')
    sessions = Path(__file__).resolve().parent / '.mail-readonly-sessions'
    directory = sessions / args.session
    try:
        if args.action in ('show', 'purge'):
            private(sessions, True)
            return handoff(directory, args.action)
        state = InboxBatchSession(credential_root(Path.home() / '.config/bichi-mail'),
                                 Path.home() / '.openclaw-bichi-mail-analysis/openclaw.json', directory)
        checks = state.local_preflight()
        if args.action == 'preflight':
            print(json.dumps({**checks, 'query': QUERY, 'page_size': PAGE_SIZE,
                              'result_directory': str(directory), 'unlock_required': True,
                              'transport_retries_disabled': False,
                              'transport_scope_resolution_required': not args.accept_internal_transport_retries},
                             sort_keys=True))
            return 0
        if not args.accept_internal_transport_retries:
            print('Live bloqueado: gog puede reintentar HTTP. Resolver ese límite con Bichi antes del desbloqueo.',
                  file=sys.stderr)
            return 2
        state.receipt['internal_transport_retries_acknowledged'] = True
        sessions.mkdir(mode=0o700, exist_ok=True)
        private(sessions, True)
        fd = os.open(sessions / 'session.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return run_terminal(state)
        finally:
            os.close(fd)
    except (KeyboardInterrupt, EOFError):
        print('Cancelado. No repetir; revisar recibo y salida parcial con Bichi.', file=sys.stderr)
        return 130
    except Exception:
        print('No se inició/completó el batch. Revisar configuración, recibo o sesión concurrente con Bichi.',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
