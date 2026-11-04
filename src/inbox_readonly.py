"""Reviewed, single-read functional proof; separate from the strict E2E gate.

Import/preflight are local only. No synthetic inference, delivery, production DB,
runtime changes or stderr observer. The real variant requires explicit contract
review and scope-evidence acknowledgement before opening its password form.
"""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import tempfile
import threading
import time

from assistant import authentication_analysis, authentication_content
from gmail_consent import ConsentError, ThreadingHTTPServer, handler_for
from gmail_unlock import Verification
from oneshot import analysis_config, clean_env, private
from pipeline import OpenClawAnalyzer

CONFIG_SHA256 = 'a436d949cefc1f01e851ad82ccfadea9a8157915b478989aee5eab73b4eac9bc'
GOG_SHA256 = '36551bb5725a7c7f22413826109ed455ce9de9a1aecaa81e918a5d461072787a'
QUERY = 'in:inbox -in:spam -in:trash -in:sent -in:drafts'
MESSAGE_ID = re.compile(r'[a-fA-F0-9]{1,64}\Z')
EXCLUDED = {'SPAM', 'TRASH', 'SENT', 'DRAFT'}


def json_object(raw):
    # Duplicate JSON keys must not hide errors or ambiguous tool counters.
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('ambiguous JSON')
            value[key] = item
        return value
    def nonfinite(_):
        raise ValueError('nonfinite JSON')
    result = json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)
    if not isinstance(result, dict):
        raise ValueError('JSON object required')
    return result


def eligible(labels):
    return (isinstance(labels, list) and all(isinstance(v, str) for v in labels)
            and 'INBOX' in labels and not EXCLUDED.intersection(labels))


def validate_analysis(value):
    if (not isinstance(value, dict)
            or set(value) not in ({'summary', 'priority', 'topic', 'promotional', 'financial', 'financial_data'},
                                  {'summary', 'priority', 'topic', 'promotional', 'financial', 'financial_data', 'notification'})
            or value['priority'] not in (None, 'alta', 'media')
            or value['topic'] is not None
            or type(value['promotional']) is not bool or type(value['financial']) is not bool
            or not isinstance(value['summary'], str) or not 1 <= len(value['summary'].strip()) <= 500
            or authentication_content(value['summary'])):
        raise ValueError('invalid functional analysis')
    # Financial evidence is validated against the supplied source by the adapter.
    # No account references/quotes are retained in the small private result.
    result = {k: value[k] for k in ('summary', 'priority', 'promotional', 'financial')}
    if 'notification' in value:
        from mail_stages import prose
        from notification_labels import validate_display
        notice = value['notification']
        if (not isinstance(notice, dict) or set(notice) not in ({'title', 'action'}, {'title', 'action', 'label'})
                or not prose(notice['title'], 70)
                or (notice['action'] is not None and not prose(notice['action'], 120))):
            raise ValueError('invalid notification')
        if 'label' in notice:
            validate_display(notice['label'])
        result['notification'] = notice
    return result


class InboxProof:
    def __init__(self, runtime_root, config_path, state_dir, *, runner=subprocess.run,
                 clock=time.monotonic, reviewed_contract=False, accept_scope_limit=False):
        self.root, self.config_path, self.state_dir = map(Path, (runtime_root, config_path, state_dir))
        self.runner, self.clock = runner, clock
        self.reviewed_contract, self.accept_scope_limit = reviewed_contract, accept_scope_limit
        self.deadline = clock() + 600
        self.lock = threading.Lock()
        self.attempted = self.saved = self.error = False
        self.analysis = None
        self.receipt = {'contract': 'inbox_readonly_functional_v1', 'functional_passed': False,
                        'canonical_gate_executed': False, 'stderr_provenance_verified': False,
                        'effective_scopes_verified': False, 'auth_local_invocations': 0,
                        'auth_refresh_invocations': 0, 'metadata_search_invocations': 0,
                        'body_get_invocations': 0, 'model_invocations': 0,
                        'mail_mutation_invocations': 0, 'attachment_fetch_invocations': 0,
                        'delivery_invocations': 0}

    def expire(self):
        return self.clock() >= self.deadline

    def clear(self, *, force=False):
        # The shared HTTP handler clears failures, including a rejected replay.
        # That must not erase a successful result before private completion.
        if force or not self.saved:
            self.analysis = None

    def _within_window(self):
        if self.expire():
            raise ValueError('proof expired')

    def local_preflight(self):
        self._within_window()
        private(self.root, True)
        private(self.root / 'gog', True)
        private(self.root / 'gmail-account.pending')  # Only stat, no account/credential read.
        config = private(self.config_path)
        if hashlib.sha256(config.read_bytes()).hexdigest() != CONFIG_SHA256:
            raise ValueError('approved configuration changed')
        if hashlib.sha256((Path.home() / '.local/bin/gog').read_bytes()).hexdigest() != GOG_SHA256:
            raise ValueError('reviewed gog changed')
        if Path(shutil.which('openclaw') or '').resolve() != (
                Path.home() / '.npm-global/lib/node_modules/openclaw/openclaw.mjs'):
            raise ValueError('unexpected OpenClaw executable')
        self.receipt.update(config_sha256=CONFIG_SHA256, gog_binary_sha256=GOG_SHA256)
        OpenClawAnalyzer(analysis_config({'analysis_config': str(config)}))
        if self.state_dir.exists() or self.state_dir.is_symlink():
            private(self.state_dir, True)
            for name in ('attempt.json', 'result.json'):
                if (self.state_dir / name).exists() or (self.state_dir / name).is_symlink():
                    raise ValueError('previous attempt; reconcile, never retry')
        return {'local_config_valid': True, 'network_checked': False,
                'contract_reviewed': self.reviewed_contract,
                'effective_scopes_verified': False}

    def _claim(self):
        self.state_dir.mkdir(mode=0o700, exist_ok=True)
        private(self.state_dir, True)
        fd = os.open(self.state_dir / 'attempt.json',
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'contract': self.receipt['contract'], 'state': 'attempted_no_retry'}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        folder = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)

    def _persist_receipt(self):
        fd, name = tempfile.mkstemp(prefix='.receipt-', dir=self.state_dir)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(self.receipt, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.state_dir / 'result.json')
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _gog_diagnostic(self, command, counter):
        # Diagnostics contain only fixed categories and a bounded process rc.
        # Never persist payloads, exception messages, stderr, identities or hashes
        # of credential-related output. This does not weaken any acceptance gate.
        if counter in ('auth_local_invocations', 'auth_refresh_invocations'):
            diagnostic = {'stage': 'refresh' if counter == 'auth_refresh_invocations' else 'local',
                          'outcome': 'runner_error', 'returncode': None, 'stderr_present': None}
            self.receipt.setdefault('auth_diagnostics', []).append(diagnostic)
            return diagnostic
        return None

    def _gog(self, password, account, command, arguments, counter):
        self._within_window()
        if self.receipt[counter] != 0:
            raise ValueError('invocation already attempted')
        self.receipt[counter] += 1
        diagnostic = self._gog_diagnostic(command, counter)
        env = clean_env()
        env.update(GOG_HOME=str(self.root / 'gog'), GOG_KEYRING_BACKEND='file',
                   GOG_KEYRING_PASSWORD=password)
        args = [str(Path.home() / '.local/bin/gog'), '--home', str(self.root / 'gog'),
                '--client', 'default', '--account', account, '--readonly', '--gmail-no-send',
                '--no-input', '--json', '--enable-commands-exact', command, *arguments]
        try:
            result = self.runner(args, env=env, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=45)
            if diagnostic is not None:
                rc = result.returncode
                diagnostic.update(returncode=rc if type(rc) is int and -255 <= rc <= 255 else None,
                                  stderr_present=bool(result.stderr),
                                  outcome='nonzero_exit' if rc != 0 else 'stderr_rejected')
            if result.returncode != 0 or result.stderr:
                raise ValueError('Gmail/auth command failed')
            if diagnostic is not None:
                diagnostic['outcome'] = 'deadline_expired'
            self._within_window()
            if diagnostic is not None:
                diagnostic['outcome'] = 'json_invalid'
            value = json_object(result.stdout)
            if diagnostic is not None:
                diagnostic['outcome'] = 'validation_pending'
            return value
        except subprocess.TimeoutExpired:
            if diagnostic is not None:
                diagnostic['outcome'] = 'timeout'
            raise
        except OSError:
            if diagnostic is not None:
                diagnostic['outcome'] = 'process_unavailable'
            raise
        finally:
            env.pop('GOG_KEYRING_PASSWORD', None)

    def _validate_auth(self, verifier, result, account, checked):
        diagnostic = self.receipt['auth_diagnostics'][-1]
        diagnostic['outcome'] = 'validation_rejected'
        # gog v0.46.0 can return rc=0 with an unreadable-token row. Classify
        # presence only, never its text/hint or an inferred password diagnosis.
        accounts = result.get('accounts')
        if (isinstance(accounts, list) and len(accounts) == 1
                and isinstance(accounts[0], dict) and accounts[0].get('error')):
            diagnostic['outcome'] = 'validation_reported_error'
        verifier.validate(result, account, checked)
        diagnostic['outcome'] = 'accepted'

    def _model_invocation_allowed(self):
        return self.receipt['model_invocations'] == 0

    def _model_run(self, args, **kwargs):
        self._within_window()
        prefix = ['openclaw', '--profile', 'bichi-mail-analysis', 'agent', 'exec']
        if (not self._model_invocation_allowed() or args[:5] != prefix or len(args) != 14
                or args[5] != '--config' or args[7] != '--cwd'
                or args[-5:] != ['--message-file', '-', '--json', '--timeout', '120']
                or kwargs.get('timeout') != 150):
            raise ValueError('unexpected model invocation')
        pinned = Path(args[6])
        config_bytes = self.config_path.read_bytes()
        if (hashlib.sha256(config_bytes).hexdigest() != CONFIG_SHA256
                or json_object(pinned.read_text()) != json_object(config_bytes)
                or args[8] != str(pinned.parent)):
            raise ValueError('model configuration drift')
        self.receipt['model_invocations'] += 1
        kwargs.pop('check', None)
        command = [args[0], '--log-level', 'silent', *args[1:]]
        result = self.runner(command, env=clean_env(), check=False, **kwargs)
        stderr = result.stderr.encode('utf8')
        self.receipt.update(model_returncode=result.returncode, model_stderr_bytes=len(stderr),
                            model_stderr_sha256=hashlib.sha256(stderr).hexdigest(),
                            diagnostic_review_required=bool(stderr))
        if result.returncode != 0:
            raise ValueError('model command failed')
        envelope = json_object(result.stdout)
        if (envelope.get('ok') is not True or envelope.get('status') != 'ok'
                or envelope.get('provider') != 'openai' or envelope.get('model') != 'gpt-6.1-sol'
                or envelope.get('error') is not None):
            raise ValueError('invalid functional envelope')
        tools, bridge = envelope.get('toolSummary'), envelope.get('bridgeCalls')
        if (not isinstance(tools, dict) or set(tools) != {'calls', 'tools'}
                or type(tools['calls']) is not int or tools['calls'] != 0 or tools['tools'] != []
                or not isinstance(bridge, dict) or set(bridge) != {'search', 'describe', 'call'}
                or any(type(v) is not int or v != 0 for v in bridge.values())):
            raise ValueError('explicit zero tools and bridge required')
        self._validate_model_analysis(json_object(envelope['final']))
        self.receipt.update(provider_model_verified=True, explicit_zero_tools=True,
                            explicit_zero_bridge=True, provider='openai', model='gpt-6.1-sol')
        self._within_window()
        return result  # stdout/stderr unchanged; canonical gate is not invoked or patched.

    def _validate_model_analysis(self, value):
        return validate_analysis(value)

    def _analyze_message(self, item, content):
        analyzer = OpenClawAnalyzer(analysis_config({'analysis_config': str(self.config_path)}),
                                   self._model_run)
        return analyzer.analyze(item, content, [])

    def read_inbox(self, password, account):
        """Shared readonly selection/analysis; caller must validate auth first."""
        listing = self._gog(password, account, 'gmail.messages.search',
                            ['gmail', 'messages', 'search', QUERY, '--max', '1', '--timezone',
                             'UTC', '--sanitize-content'], 'metadata_search_invocations')
        rows = listing.get('messages')
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError('one candidate required')
        # Other inbox pages are expected, never followed by this single reader.
        if not isinstance(listing.get('nextPageToken', ''), str) and listing.get('nextPageToken') is not None:
            raise ValueError('invalid page token')
        return self.read_selected(rows[0], password, account)

    def _eligible_message(self, labels):
        return eligible(labels)

    def validate_candidate(self, row):
        if not isinstance(row, dict):
            raise ValueError('invalid metadata candidate')
        mid = row.get('id')
        if (not isinstance(mid, str) or not MESSAGE_ID.fullmatch(mid)
                or not self._eligible_message(row.get('labels')) or 'body' in row):
            raise ValueError('invalid metadata candidate')
        for key in ('subject', 'from'):
            if not isinstance(row.get(key, ''), str):
                raise ValueError('invalid candidate header')

    def read_selected(self, row, password, account):
        """Analyze a previously selected message without another metadata search."""
        self.validate_candidate(row)
        mid = row['id']
        if authentication_content(row.get('subject', '') + ' ' + row.get('from', '')):
            self.analysis = {k: v for k, v in authentication_analysis().items()
                             if k in ('summary', 'priority', 'promotional', 'financial')}
            self.receipt['authentication_protected'] = True
        else:
            message = self._gog(password, account, 'gmail.get',
                                ['gmail', 'get', mid, '--format', 'full', '--sanitize-content'],
                                'body_get_invocations').get('message')
            if (not isinstance(message, dict) or message.get('id') != mid
                    or not self._eligible_message(message.get('labelIds'))
                    or not isinstance(message.get('headers'), dict)
                    or not isinstance(message.get('body'), str)
                    or not isinstance(message.get('attachments', []), list)):
                raise ValueError('selected message unavailable')
            headers, raw_body = message['headers'], message['body']
            if any(not isinstance(headers.get(k, ''), str) for k in ('subject', 'from')):
                raise ValueError('invalid selected headers')
            if authentication_content(headers.get('subject', '') + ' ' + headers.get('from', '') + ' ' + raw_body):
                self.analysis = {k: v for k, v in authentication_analysis().items()
                                 if k in ('summary', 'priority', 'promotional', 'financial')}
                self.receipt['authentication_protected'] = True
            else:
                body = ' '.join(re.sub(r'https?://\S+', '[url removed]', raw_body).split())
                if not body:
                    raise ValueError('no usable body; do not read another message')
                content = {'body': body[:12000], 'truncated': len(body) > 12000,
                           'attachments_read': False}
                value = self._analyze_message({'sender': headers.get('from', '')[:256],
                                               'subject': headers.get('subject', '')[:512]}, content)
                self.analysis = validate_analysis(value)
                self.receipt.update(body_truncated=content['truncated'],
                                    attachments_omitted=bool(message.get('attachments')))
        return dict(self.analysis)

    def execute(self, form):
        if self.attempted:
            form.clear()
            raise ConsentError('Intento ya consumido. Revisar con Bichi; no repetir.')
        password = form.get('password', [''])[0]
        claimed = False
        try:
            if self.attempted or not self.reviewed_contract or not self.accept_scope_limit:
                raise ValueError('single attempt and reviewed contract required')
            self.attempted = True
            if not isinstance(password, str) or not 16 <= len(password) <= 1024:
                raise ValueError('invalid keyring password shape')
            self.local_preflight()
            self._claim()
            claimed = True
            account = private(self.root / 'gmail-account.pending').read_text().strip()
            if not re.fullmatch(r'[\w.+-]+@[\w.-]+', account):
                raise ValueError('invalid account')
            verifier = Verification(self.root)
            for checked, counter in ((False, 'auth_local_invocations'), (True, 'auth_refresh_invocations')):
                result = self._gog(password, account, 'auth.list',
                                   ['auth', 'list'] + (['--check', '--timeout=15s'] if checked else []), counter)
                self._validate_auth(verifier, result, account, checked)
            self.receipt.update(stored_scopes_readonly=True, refresh_checked=True)
            self.read_inbox(password, account)
            self._within_window()
            self.receipt['functional_passed'] = self.receipt.get('provider_model_verified') is True
            self.saved = True
            return dict(self.analysis)  # Private, derived result in RAM only, never evidence.
        except Exception:
            self.clear()
            self.error = True
            self.receipt['functional_passed'] = False
            raise ConsentError('Prueba detenida. Revisar recibo con Bichi; no repetir.') from None
        finally:
            password = None
            form.clear()
            if claimed:
                try:
                    self._persist_receipt()
                except Exception:
                    self.saved = False
                    self.clear()
                    self.receipt['functional_passed'] = False
                    raise ConsentError('No se confirmó el recibo. No repetir la prueba.') from None

    def page(self, csrf):
        if self.saved and self.analysis is not None:
            body = ('<h1>Lectura única terminada</h1><p>' + html.escape(self.analysis['summary'])
                    + '</p><p>Solo lectura; sin envíos ni cambios en Gmail. '
                    'Este resultado no habilita el ensayo de correo ni automatizaciones.</p>')
            if self.receipt.get('diagnostic_review_required'):
                body += '<p>Hay diagnósticos stderr pendientes; validación funcional no equivale a gate estricto aprobado.</p>'
            if self.receipt.get('authentication_protected'):
                body += '<p>Contenido de autenticación protegido localmente; no se invocó el modelo.</p>'
        elif self.attempted or self.expire():
            body = '<h1>Prueba cerrada</h1><p>Revisar con Bichi. No reenviar el formulario ni repetir el intento.</p>'
        else:
            body = ('<h1>Analizar un correo de Inbox</h1><p>Una selección de metadatos y como máximo '
                    'un cuerpo y un análisis privado. Sin adjuntos descargados por separado, envíos, '
                    'respuestas o marcas de leído. No sigue otras páginas ni elige otro correo si falla. '
                    'Se verifican permisos readonly registrados y refresh; los scopes efectivos no se introspectan. '
                    'stderr se informa como diagnóstico pendiente y no invalida por sí solo el resultado funcional. '
                    'Usá la contraseña existente; nunca la envíes por chat.</p>'
                    f'<form method="post" action="/run"><input type="hidden" name="csrf" value="{csrf}">'
                    '<label>Contraseña del almacén <input type="password" name="password" '
                    'minlength="16" maxlength="1024" autocomplete="current-password" required></label>'
                    '<button>Leer y analizar una sola vez</button></form>')
        return '<!doctype html><html lang="es"><meta charset="utf-8"><title>Prueba Inbox readonly</title>' + body + '</html>'


def proof_handler(state, public_url):
    return handler_for(state, public_url, page_renderer=state.page, actions={'/run': state.execute},
                       error_body='<h1>Prueba detenida</h1><p>Revisar con Bichi. No repetir el POST.</p>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'serve-reviewed'))
    parser.add_argument('--reviewed-functional-contract', action='store_true')
    parser.add_argument('--accept-unverified-effective-scopes', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    state = InboxProof(Path.home() / '.config/bichi-mail',
                       Path.home() / '.openclaw-bichi-mail-analysis/openclaw.json',
                       Path(__file__).resolve().parent / '.inbox-proof-state',
                       reviewed_contract=args.reviewed_functional_contract,
                       accept_scope_limit=args.accept_unverified_effective_scopes)
    try:
        result = state.local_preflight()
        if args.action == 'preflight':
            print(json.dumps(result, sort_keys=True))
            return 0
        if not (state.reviewed_contract and state.accept_scope_limit):
            raise ValueError('review required before starting the real form')
        server = ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])),
                                     proof_handler(state, os.environ['PUBLIC_URL']))
        server.daemon_threads = False
        def finish():
            while not state.expire() and not state.attempted:
                time.sleep(0.2)
            # Join the active POST before closing. Result stays in RAM until shutdown.
            server.shutdown()
        threading.Thread(target=finish, daemon=True).start()
        try:
            server.serve_forever()
        finally:
            server.server_close()
            # Intended derived result to the operator's private completion only.
            # Never redirect this output to evidence files or public logs.
            print(json.dumps({'receipt': state.receipt,
                              'private_analysis': state.analysis if state.saved else None}, sort_keys=True))
            state.clear(force=True)
        return 0 if state.saved else 1
    except Exception:
        print(json.dumps({'local_preflight_or_serve_failed': True, 'private_data_printed': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
