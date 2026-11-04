"""Prepared, opt-in one-message test. No external operations on import/preflight.

serve is a future coordinated action, not authorization to run it.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import stat
import subprocess
import threading
import time

from assistant import ID, authentication_content, text
from gmail_consent import ConsentError, ThreadingHTTPServer, handler_for
from gmail_unlock import Verification
from pipeline import OpenClawAnalyzer, OpenClawSender

SUBJECT = 'Bichi Mail E2E'
BODY = 'Prueba controlada de Bichi Mail. Mensaje sin datos personales.'
FIXTURE_QUERY = (f'in:inbox -in:sent -in:drafts -in:spam -in:trash -has:attachment '
                 f'newer_than:2d subject:"{SUBJECT}" "{BODY}"')


def private(path, directory=False):
    path = Path(path)
    info = path.lstat()
    if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError('private path required')
    return path


def clean_env():
    return {k: os.environ[k] for k in ('HOME', 'PATH', 'LANG', 'LC_ALL') if k in os.environ}


def load_plan(path):
    plan = json.loads(private(path).read_text())
    expected = {'account', 'message_id', 'telegram_account_id', 'telegram_chat_id',
                'telegram_user_id', 'analysis_config', 'state_dir', 'expires_at',
                'recipient_confirmation'}
    if not isinstance(plan, dict) or set(plan) != expected:
        raise ValueError('closed plan required')
    if (not isinstance(plan['account'], str) or not re.fullmatch(r'[\w.+-]+@[\w.-]+', plan['account'])
            or (plan['message_id'] is not None
                and (not isinstance(plan['message_id'], str) or not ID.fullmatch(plan['message_id'])))
            or not isinstance(plan['telegram_account_id'], str)
            or not re.fullmatch(r'[\w-]{1,80}', plan['telegram_account_id'])
            or plan['telegram_account_id'].lower() == 'albert'
            or not isinstance(plan['telegram_user_id'], str)
            or not re.fullmatch(r'[1-9][0-9]{0,19}', plan['telegram_user_id'])
            or plan['telegram_chat_id'] != plan['telegram_user_id']
            or type(plan['expires_at']) is not int
            or not time.time() < plan['expires_at'] <= time.time() + 1800):
        raise ValueError('invalid or expired plan')
    confirmation = plan['recipient_confirmation']
    if (not isinstance(confirmation, dict)
            or set(confirmation) != {'by', 'channel', 'chat_type', 'at_unix'}
            or confirmation['by'] != 'operator' or confirmation['channel'] != 'telegram'
            or confirmation['chat_type'] != 'private'
            or type(confirmation['at_unix']) is not int
            or not 0 <= time.time() - confirmation['at_unix'] <= 1800):
        raise ValueError('recent private recipient confirmation required')
    state_dir = private(plan['state_dir'], True)
    # No runtime-private/production DB can be selected as the one-shot ledger.
    if state_dir.resolve() != Path(__file__).resolve().parent / '.oneshot-state':
        raise ValueError('project-isolated ledger required')
    private(plan['analysis_config'])
    return plan


def analysis_config(plan):
    return {'analysis_enabled': True, 'analysis_profile': 'bichi-mail-analysis',
            'analysis_model': 'openai/gpt-6.1-sol', 'analysis_config': plan['analysis_config']}


def safe_analysis(value):
    if (value.get('priority') not in (None, 'alta', 'media') or value.get('topic') is not None
            or value.get('promotional') is not False or value.get('financial') is not False
            or value.get('financial_data') is not None):
        raise ValueError('unexpected fixture analysis')
    summary = text(value['summary'], 500)
    if not summary or authentication_content(summary):
        raise ValueError('unsafe summary')
    return summary


def fixture_item():
    return {'sender': 'Prueba controlada', 'subject': SUBJECT}


class Ledger:
    """Metadata-only, fail-closed claim. Never retry a claimed message automatically."""
    def __init__(self, plan):
        self.root = private(plan['state_dir'], True)
        key = hashlib.sha256(json.dumps([plan['account'].casefold(),
                                        plan['message_id'].lower()]).encode()).hexdigest()
        self.path = self.root / (key + '.json')
        self.route = hashlib.sha256(json.dumps([plan['telegram_account_id'],
                                              plan['telegram_chat_id']]).encode()).hexdigest()

    def available(self):
        if self.path.exists() or self.path.is_symlink():
            raise ValueError('already claimed; reconcile, do not retry')

    def write(self, state, receipt=None, *, claim=False):
        data = {'state': state, 'route_hash': self.route, 'receipt': receipt,
                'at_unix': int(time.time()), 'effective_scopes_verified': False}
        if claim:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, 'w') as out:
                    json.dump(data, out)
                    out.flush()
                    os.fsync(out.fileno())
                folder = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(folder)
                finally:
                    os.close(folder)
            except Exception:
                # Retain even a partial claim: fail closed across crashes.
                raise
        else:
            # Truncation failure retains the claim; a corrupt record also blocks retry.
            fd = os.open(self.path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
            with os.fdopen(fd, 'w') as out:
                json.dump(data, out)
                out.flush()
                os.fsync(out.fileno())


class OneShot:
    def __init__(self, plan, runtime_root, *, runner=subprocess.run, clock=time.monotonic,
                 accept_discovery_metadata=False):
        self.plan = dict(plan)
        self.runtime_root, self.runner, self.clock = Path(runtime_root), runner, clock
        self.deadline = clock() + 600
        self.lock = threading.Lock()
        self.done = self.saved = self.error = False
        self.running = False
        self.preflight_passed = False
        self.tool_summary_measured = False
        self.discovery = plan['message_id'] is None
        self.accept_discovery_metadata = accept_discovery_metadata
        self.ledger = None if self.discovery else Ledger(plan)
        self.analyzer = OpenClawAnalyzer(analysis_config(plan), self.model_run)

    def local_preflight(self):
        if self.discovery and not self.accept_discovery_metadata:
            raise ValueError('explicit discovery metadata decision required')
        if self.ledger is not None:
            self.ledger.available()

    def expire(self):
        return self.clock() >= self.deadline or time.time() >= self.plan['expires_at']

    def clear(self):
        pass

    def model_run(self, args, **kwargs):
        # Password and ambient shared OpenClaw auth/config overrides never enter model.
        # Silence subsystem logging, not contractual errors or direct stderr writes.
        command = [args[0], '--log-level', 'silent', *args[1:]]
        result = self.runner(command, env=clean_env(), **kwargs)
        if result.returncode or result.stderr:
            raise ValueError('model preflight/cleanup requires review')
        envelope = json.loads(result.stdout)
        if not isinstance(envelope, dict):
            raise ValueError('invalid model envelope')
        if 'bridgeCalls' in envelope:
            bridge = envelope['bridgeCalls']
            if (not isinstance(bridge, dict) or set(bridge) != {'search', 'describe', 'call'}
                    or any(type(count) is not int or count != 0 for count in bridge.values())):
                raise ValueError('unexpected bridge activity')
        # Bridge counters alone do not measure all tools. Missing/null telemetry
        # cannot stand in for an explicit zero, even with a successful final JSON.
        tools = envelope.get('toolSummary')
        if (not isinstance(tools, dict) or set(tools) != {'calls', 'tools'}
                or type(tools['calls']) is not int or tools['calls'] != 0
                or tools['tools'] != []):
            raise ValueError('explicit zero tool activity required')
        self.tool_summary_measured = True
        return result

    def preflight(self):
        self.local_preflight()
        if self.expire():
            raise ValueError('expired plan')
        private(self.runtime_root, True)
        private(self.runtime_root / 'gog', True)
        account = private(self.runtime_root / 'gmail-account.pending').read_text().strip()
        if account.casefold() != self.plan['account'].casefold():
            raise ValueError('account mismatch')
        # Prove dedicated owner works on synthetic data BEFORE any Gmail/auth network.
        safe_analysis(self.analyzer.analyze(fixture_item(), {'body': BODY}, []))
        if self.expire():
            raise ValueError('preflight expired')
        self.preflight_passed = True

    def gog_read(self, password, command, args):
        env = clean_env()
        env.update(GOG_HOME=str(self.runtime_root / 'gog'), GOG_KEYRING_BACKEND='file',
                   GOG_KEYRING_PASSWORD=password)
        try:
            result = self.runner([str(Path.home() / '.local/bin/gog'), '--home',
                                  str(self.runtime_root / 'gog'), '--client', 'default',
                                  '--account', self.plan['account'], '--readonly', '--gmail-no-send',
                                  '--enable-commands-exact', command, '--no-input', '--json',
                                  *args], env=env, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=45)
            if result.returncode or result.stderr:
                raise ValueError('read failed')
            return json.loads(result.stdout)
        finally:
            env.pop('GOG_KEYRING_PASSWORD', None)

    def discover_fixture(self, password):
        if not self.accept_discovery_metadata or self.expire():
            raise ValueError('discovery not authorized or expired')
        result = self.gog_read(password, 'gmail.messages.search',
                               ['gmail', 'messages', 'search', FIXTURE_QUERY, '--max', '2'])
        # Never follow pagination, filter down duplicates, or pick the first result.
        if (not isinstance(result, dict) or set(result) != {'messages', 'nextPageToken'}
                or result['nextPageToken'] not in (None, '')
                or not isinstance(result['messages'], list) or len(result['messages']) != 1):
            raise ValueError('fixture missing or ambiguous')
        candidate = result['messages'][0]
        if (not isinstance(candidate, dict) or not isinstance(candidate.get('id'), str)
                or not ID.fullmatch(candidate['id']) or candidate.get('subject') != SUBJECT):
            raise ValueError('invalid fixture candidate')
        self.plan['message_id'] = candidate['id']
        self.ledger = Ledger(self.plan)
        # Same canonical account+message key as explicit-ID mode, before full read.
        self.ledger.available()

    def read_fixture(self, password):
        if self.expire():
            raise ValueError('expired before read')
        msg = self.gog_read(password, 'gmail.get',
                            ['gmail', 'get', self.plan['message_id'], '--format', 'full',
                             '--sanitize-content'])['message']
        # No sender/recipients/snippet/thread metadata is passed to the model.
        if (msg.get('id') != self.plan['message_id']
                or not isinstance(msg.get('labelIds'), list)
                or not all(isinstance(x, str) for x in msg['labelIds'])
                or (self.discovery and 'INBOX' not in msg['labelIds'])
                or {'SPAM', 'TRASH', 'SENT', 'DRAFT'}.intersection(msg['labelIds'])
                or msg.get('attachments') != []
                or msg.get('headers', {}).get('subject') != SUBJECT
                or not isinstance(msg.get('body'), str)
                or ' '.join(msg['body'].split()) != BODY):
            raise ValueError('not the approved safe fixture')
        return {'body': BODY, 'truncated': False, 'attachments_read': False}

    def send_run(self, args, **kwargs):
        result = self.runner(args, env=clean_env(), **kwargs)
        envelope = json.loads(result.stdout)
        data = envelope.get('payload', envelope)
        delivery = data.get('result', {})
        target = delivery.get('target')
        if target is not None:
            if not isinstance(target, dict) or target.get('kind') != 'chat':
                raise ValueError('receipt destination incompatible')
            chat = target.get('id')
        else:
            chat = delivery.get('chatId')
        if (str(chat) != self.plan['telegram_chat_id']
                or ('chatId' in delivery and str(delivery['chatId']) != self.plan['telegram_chat_id'])
                or delivery.get('channel', 'telegram') != 'telegram'):
            raise ValueError('receipt destination unconfirmed')
        return result

    def execute(self, form):
        password = form.get('password', [''])[0]
        try:
            if self.done or self.expire() or not self.preflight_passed:
                raise ValueError('one-shot ended')
            self.done = True  # One POST attempt per process, including failure.
            self.running = True
            self.local_preflight()
            if not 16 <= len(password) <= 1024:
                raise ValueError('invalid password')
            probe = Verification(self.runtime_root, check_google=True, runner=self.runner)
            probe.verify({'password': [password]}, persist=False)
            if self.discovery:
                self.discover_fixture(password)
            content = self.read_fixture(password)
            password = None
            summary = safe_analysis(self.analyzer.analyze(fixture_item(), content, []))
            if self.expire():
                raise ValueError('deadline before send')
            sender = OpenClawSender({'delivery_enabled': True}, self.send_run)
            row = {'route': json.dumps({'account': self.plan['telegram_account_id'],
                                        'chat': self.plan['telegram_chat_id']}),
                   'payload': 'Bichi Mail · prueba controlada\n' + summary}
            # Irreversible boundary: preserve claim even on timeout/crash/invalid receipt.
            self.ledger.write('inflight', claim=True)
            try:
                receipt = sender.send(row)
                if not re.fullmatch(r'[1-9][0-9]{0,39}', receipt):
                    raise ValueError('invalid receipt')
                self.ledger.write('sent', receipt)
                self.saved = True
            except Exception:
                self.ledger.write('unknown')
                raise
        except Exception:
            raise ConsentError('Prueba detenida. Revisar estado sanitizado con Bichi; no reintentar automáticamente.') from None
        finally:
            self.running = False
            password = None
            form.clear()

    def page(self, csrf):
        if self.saved:
            body = '<h1>Aviso confirmado</h1><p>Un mensaje de prueba leído y un aviso. Bichi debe validar el resultado y cerrar el portal. Scopes efectivos no verificados.</p>'
        elif self.running:
            body = '<h1>Prueba en curso</h1><p>No reenviar ni recargar el formulario. Bichi reconciliará el recibo si se corta la conexión.</p>'
        elif self.done or self.expire():
            body = '<h1>Prueba detenida</h1><p>No reintentar sin revisar el ledger con Bichi.</p>'
        else:
            discovery_notice = ('Se buscará el fixture público recibido en Inbox en los últimos dos días; '
                                'la búsqueda consulta etiquetas y metadatos de hasta dos coincidencias. '
                                'Si no hay una única coincidencia se detiene sin leer cuerpos ni enviar. '
                                if self.discovery else '')
            body = ('<h1>Prueba única Bichi Mail</h1><p>Solo el mensaje de prueba seleccionado, análisis y un aviso al Telegram privado previamente fijado. '
                    + discovery_notice +
                    'No activa el asistente. Scopes efectivos no verificados; se usan comandos readonly. '
                    'Ingresá la contraseña EXISTENTE del almacén, nunca una nueva.</p>'
                    f'<form method="post" action="/run"><input type="hidden" name="csrf" value="{csrf}">'
                    '<label>Contraseña <input type="password" name="password" minlength="16" maxlength="1024" '
                    'autocomplete="current-password" required></label><button>Ejecutar una prueba</button></form>')
        return '<!doctype html><html lang="es"><meta charset="utf-8"><title>Prueba única Bichi Mail</title>' + body + '</html>'


def oneshot_handler(state, public_url):
    return handler_for(state, public_url, page_renderer=state.page,
                       actions={'/run': state.execute},
                       error_body='<h1>Prueba detenida</h1><p>Consultar a Bichi. No reintentar sin reconciliar el ledger.</p>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--accept-unverified-effective-scopes', action='store_true')
    parser.add_argument('--accept-fixture-discovery-metadata', action='store_true')
    parser.add_argument('command', choices=['preflight', 'serve'])
    args = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    lock_fd = None
    try:
        plan = load_plan(args.plan)
        state = OneShot(plan, Path.home() / '.config/bichi-mail',
                        accept_discovery_metadata=args.accept_fixture_discovery_metadata)
        if args.command == 'preflight':
            # Purely local config validation. Does NOT imply credentials usable.
            state.local_preflight()
            print('{"local_config_valid":true,"network_checked":false,"effective_scopes_verified":false}')
            return 0
        if not args.accept_unverified_effective_scopes:
            raise ValueError('explicit scope-risk decision required')
        # Lock an existing directory inode: no lockfile or persistent gate changes.
        lock_fd = os.open(plan['state_dir'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Validate URL before model/network, no listener until synthetic gate passes.
        handler = oneshot_handler(state, os.environ['PUBLIC_URL'])
        state.preflight()
        state.deadline = state.clock() + 600
        server = ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])), handler)
        def expire():
            while not state.done and not state.expire():
                time.sleep(1)
            time.sleep(2)
            server.shutdown()
        threading.Thread(target=expire, daemon=True).start()
        try:
            server.serve_forever()
        finally:
            # Join active handlers; never orphan a password-bearing subprocess.
            server.server_close()
        return 0 if state.saved else 1
    except Exception:
        print('{"blocked":true,"details":"Review private plan, owner preflight, deadline and ledger; no automatic retry."}')
        return 1
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == '__main__':
    raise SystemExit(main())
