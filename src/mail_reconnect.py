"""Private-terminal Gmail reconnection into a NEW encrypted readonly store.

Preserves the original keyring and all previous ledgers. Reuses gog's remote
OAuth flow, then the existing single-mail reader/analyzer. No listeners/jobs.
"""
import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
import tempfile
import warnings

from gmail_consent import Consent, ConsentError
from inbox_readonly import json_object
from mail_readonly import MailSession, MESSAGES, credential_root
from oneshot import clean_env, private


def source_preflight(legacy):
    private(legacy, True)
    account = private(legacy / 'gmail-account.pending').read_text().strip()
    client = json_object(private(legacy / 'google-desktop-client.json').read_text()).get('installed')
    if (not re.fullmatch(r'[\w.+-]+@[\w.-]+', account) or not isinstance(client, dict)
            or not isinstance(client.get('client_id'), str) or not client['client_id']
            or not isinstance(client.get('client_secret'), str) or not client['client_secret']
            or client.get('auth_uri') != 'https://accounts.google.com/o/oauth2/auth'
            or client.get('token_uri') != 'https://oauth2.googleapis.com/token'):
        raise ValueError('invalid existing OAuth client/account')


def new_store(legacy):
    source_preflight(legacy)
    stores = legacy / 'readonly-stores'
    stores.mkdir(mode=0o700, exist_ok=True)
    private(stores, True)
    root = stores / ('recovery-' + os.urandom(12).hex())
    root.mkdir(mode=0o700)
    (root / 'gog').mkdir(mode=0o700)
    # Copy only original desktop client and account, NEVER old encrypted entries.
    for name in ('google-desktop-client.json', 'gmail-account.pending'):
        data = private(legacy / name).read_bytes()
        fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return root


def activate_store(legacy, root, state):
    if (not state.receipt.get('stored_scopes_readonly')
            or not state.receipt.get('refresh_checked') or state.root != root):
        raise ValueError('validated readonly auth required')
    private(legacy, True)
    stores = private(legacy / 'readonly-stores', True)
    private(root, True)
    if root.parent != stores or not re.fullmatch(r'recovery-[a-f0-9]{24}', root.name):
        raise ValueError('unexpected recovery store')
    if (private(root / 'gmail-account.pending').read_text().strip().casefold()
            != private(legacy / 'gmail-account.pending').read_text().strip().casefold()):
        raise ValueError('recovery account mismatch')
    # Atomic reversible selection only for the on-demand reader, not worker config.
    pointer = legacy / 'readonly-credentials.json'
    if pointer.exists() or pointer.is_symlink():
        if credential_root(legacy) != root:
            raise ValueError('another store is already selected; reconcile first')
        return
    fd, name = tempfile.mkstemp(prefix='.readonly-selection-', dir=legacy)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'version': 1, 'store': root.name}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, pointer)
        directory = os.open(legacy, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class RecoveryConsent(Consent):
    def __init__(self, root, runner=subprocess.run, state=None):
        self.process_runner = runner
        self.receipt_state = state
        super().__init__(root, runner=self._run_private)

    def args(self):
        # A new store needs a refresh token even when Google already has a grant.
        return super().args() + ['--force-consent']

    def _run_private(self, args, **kwargs):
        # Keep only minimal environment; no provider tokens, proxies or debug flags.
        env = clean_env()
        env.update({key: kwargs['env'][key] for key in
                    ('GOG_HOME', 'GOG_KEYRING_BACKEND', 'GOG_KEYRING_PASSWORD')})
        kwargs['env'] = env
        kwargs['stdin'] = subprocess.DEVNULL
        operation = args[args.index('--no-input') + 1:]
        if operation[:3] == ['auth', 'credentials', 'set']:
            command = 'auth.credentials.set'
        elif operation[:2] == ['auth', 'add']:
            command = 'auth.add'
        else:
            env.pop('GOG_KEYRING_PASSWORD', None)
            raise ConsentError('Operación OAuth no admitida.')
        args = [args[0], '--enable-commands-exact', command, '--gmail-no-send', *args[1:]]
        diagnostic = {'command': command, 'outcome': 'process_unavailable',
                      'returncode': None, 'stderr_present': None}
        if self.receipt_state is not None:
            self.receipt_state.receipt.setdefault('oauth_diagnostics', []).append(diagnostic)
            self.receipt_state._persist_receipt()
        try:
            result = self.process_runner(args, **kwargs)
            rc = result.returncode
            diagnostic.update(returncode=rc if type(rc) is int and -255 <= rc <= 255 else None,
                              stderr_present=bool(result.stderr),
                              outcome='accepted' if rc == 0 else 'oauth_or_storage_rejected')
            if rc != 0:
                # Inspect only in RAM; persist a closed category, never raw errors.
                raw = (result.stderr + result.stdout).lower()
                for marker, category in (
                        ('invalid_client', 'oauth_client_rejected'),
                        ('redirect_uri_mismatch', 'oauth_redirect_rejected'),
                        ('access_denied', 'google_consent_denied'),
                        ('invalid_grant', 'oauth_code_expired_or_rejected'),
                        ('integrity check failed', 'new_store_integrity_failure'),
                        ('permission denied', 'local_permission_denied'),
                        ('timeout', 'oauth_timeout'),
                        ('no such host', 'network_unavailable')):
                    if marker in raw:
                        diagnostic['outcome'] = category
                        break
            return result
        except subprocess.TimeoutExpired:
            diagnostic['outcome'] = 'oauth_timeout'
            raise
        finally:
            env.pop('GOG_KEYRING_PASSWORD', None)
            if self.receipt_state is not None:
                self.receipt_state._persist_receipt()


def hidden(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        return getpass.getpass(prompt)


def run_reconnect(legacy, config, state_dir):
    password = confirmation = redirect = None
    consent = state = None
    try:
        print('Reconectar Gmail: se conserva el almacén anterior sin cambios.')
        print('Elegí una NUEVA contraseña de al menos 16 caracteres y guardala en tu gestor.')
        password = hidden('Nueva contraseña del almacén (no se muestra): ')
        confirmation = hidden('Repetí la NUEVA contraseña (no se muestra): ')
        if not 16 <= len(password) <= 1024 or confirmation != password:
            raise ConsentError('Nueva contraseña inválida o confirmación distinta; no se inició OAuth.')
        confirmation = None
        root = new_store(legacy)
        state = MailSession(root, config, state_dir)
        state.local_preflight()
        state._claim()
        state._phase('oauth_preparing')
        consent = RecoveryConsent(root, state=state)
        consent.begin(password, password)
        state._phase('awaiting_google_consent')
        print('\nAbrí este enlace en el navegador de tu Mac y autorizá solo la lectura de Gmail:')
        print(consent.url)
        print('\nAl terminar puede aparecer un error de conexión a localhost; es esperado.')
        print('Copiá la URL COMPLETA de esa página y pegala en el siguiente prompt oculto, no en el chat.')
        redirect = hidden('URL final de localhost (no se muestra): ')
        state._phase('oauth_exchanging')
        consent.finish(redirect)
        redirect = None
        state.receipt['oauth_reconnected'] = True
        # Consent may take several minutes; reading gets its own bounded window.
        state.deadline = state.clock() + 600
        try:
            state.execute({'password': [password]})
        finally:
            # Even if the analyzer fails, keep validated credentials for diagnosis
            # without making the user consent again. Never activate rejected auth.
            if state.receipt.get('refresh_checked') and state.receipt.get('stored_scopes_readonly'):
                activate_store(legacy, root, state)
                state.receipt['recovery_store_selected'] = True
                state._persist_receipt()
        print('\nConexión readonly restablecida.')
        value = state.analysis or {}
        summary = ''.join(ch for ch in value.get('summary', '') if ch.isprintable() or ch == '\n')
        print('Análisis del correo:\n' + summary)
        if value.get('priority') and '\nPrioridad: ' not in value.get('summary', ''):
            print('Prioridad: ' + value['priority'])
        if state.receipt.get('decision', {}).get('status') == 'ok':
            print('GPT-6.1 Sol prepara → Jev 1.13.0 decide → GPT-6.1 Sol compone la salida.')
        print('Sin envíos ni cambios de correo. Contraseña nueva no guardada por el programa.')
        print('Recibo técnico (sin correo ni credenciales): ' + str(state_dir / 'result.json'))
        return 0
    except (KeyboardInterrupt, EOFError):
        print('Cancelado. El almacén anterior sigue intacto.', file=sys.stderr)
        return 130
    except Exception:
        print(MESSAGES.get(state.failure if state else None,
                           'Reconexión no completada. Revisar la fase del recibo con Bichi; no repetir.'),
              file=sys.stderr)
        if state is not None and state.state_dir.exists():
            state.receipt['reconnect_failed'] = True
            state._persist_receipt()
        return 1
    finally:
        password = confirmation = redirect = None
        if consent is not None:
            consent.clear()
        if state is not None:
            state.clear(force=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'run'))
    args = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    legacy = Path.home() / '.config/bichi-mail'
    config = Path.home() / '.openclaw-bichi-mail-analysis/openclaw.json'
    sessions = Path(__file__).resolve().parent / '.mail-readonly-sessions'
    try:
        source_preflight(legacy)
        MailSession(legacy, config, sessions / 'reconnect-preflight').local_preflight()
        if args.action == 'preflight':
            print(json.dumps({'existing_oauth_client_valid': True, 'readonly_reconnect_ready': True,
                              'original_store_preserved': True, 'network_checked': False}))
            return 0
        if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
            print('Usá una terminal privada con ssh -t, sin redirecciones.', file=sys.stderr)
            return 2
        # Never start another consent while a replacement is already selected.
        if credential_root(legacy) != legacy:
            print('Ya hay un almacén reconectado; usá mail_readonly.py run.', file=sys.stderr)
            return 2
        sessions.mkdir(mode=0o700, exist_ok=True)
        private(sessions, True)
        lock_fd = os.open(sessions / 'session.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return run_reconnect(legacy, config, sessions / ('reconnect-' + os.urandom(12).hex()))
        finally:
            os.close(lock_fd)
    except Exception:
        print('No se inició la reconexión. Revisar configuración o sesión concurrente con Bichi.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
