"""Ephemeral keyring verification. Start only after portal coordination with Bichi.

No mail commands, token export, OAuth registration, worker or Telegram operations.
"""
import argparse
import json
import os
from pathlib import Path
import resource
import stat
import subprocess
import tempfile
import threading
import time

from gmail_consent import ConsentError, IDENTITY, READONLY, handler_for, ThreadingHTTPServer


class Verification:
    def __init__(self, root, *, check_google=False, runner=subprocess.run,
                 clock=time.monotonic):
        self.root = Path(root)
        self.check_google, self.runner, self.clock = check_google, runner, clock
        self.deadline = clock() + 600
        self.attempts = 0
        self.saved = self.error = False
        self.lock = threading.Lock()

    def clear(self):
        # No password is stored in state, even between requests.
        pass

    def expire(self):
        return self.clock() >= self.deadline

    def private(self, path, mode, directory=False):
        info = path.lstat()
        if (stat.S_IMODE(info.st_mode) != mode or info.st_uid != os.getuid()
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
            raise ConsentError('Revisar permisos privados.')

    def validate(self, result, account, checked=False):
        accounts = result.get('accounts') if isinstance(result, dict) else None
        if not isinstance(accounts, list) or len(accounts) != 1:
            raise ConsentError('Resultado de cuenta incompatible.')
        row = accounts[0]
        if (not isinstance(row, dict) or row.get('email', '').casefold() != account.casefold()
                or row.get('client') != 'default' or row.get('auth') != 'oauth'
                or row.get('services') != ['gmail'] or row.get('error')
                or not isinstance(row.get('scopes'), list)
                or not all(isinstance(s, str) for s in row['scopes'])):
            raise ConsentError('Cuenta o permisos incompatibles.')
        scopes = set(row['scopes'])
        if READONLY not in scopes or scopes - {READONLY} - IDENTITY:
            raise ConsentError('Scope registrado incompatible.')
        if checked and row.get('valid') is not True:
            raise ConsentError('No se confirmó refresh.')

    def verify(self, form, *, persist=True):
        password = form.get('password', [''])[0]
        env = {}
        try:
            if self.expire() or self.saved or self.attempts >= 3:
                raise ConsentError('Sesión terminada.')
            self.attempts += 1
            if not 16 <= len(password) <= 1024:
                raise ConsentError('Contraseña inválida.')
            self.private(self.root, 0o700, True)
            self.private(self.root / 'gog', 0o700, True)
            pending = self.root / 'gmail-account.pending'
            self.private(pending, 0o600)
            account = pending.read_text().strip()
            if not account or '\n' in account or '@' not in account:
                raise ConsentError('Cuenta pendiente inválida.')
            # Do not inherit GOG overrides, proxy/debug variables or provider secrets.
            env = {k: os.environ[k] for k in ('HOME', 'PATH', 'LANG', 'LC_ALL') if k in os.environ}
            env.update(GOG_HOME=str(self.root / 'gog'), GOG_KEYRING_BACKEND='file',
                       GOG_KEYRING_PASSWORD=password)
            base = [str(Path.home() / '.local/bin/gog'), '--home', str(self.root / 'gog'),
                    '--client', 'default', '--account', account, '--json', '--no-input',
                    '--readonly', '--gmail-no-send', '--enable-commands-exact', 'auth.list',
                    'auth', 'list']
            for checked in ([False, True] if self.check_google else [False]):
                args = base + (['--check', '--timeout=15s'] if checked else [])
                result = self.runner(args, env=env, stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, timeout=45)
                if result.returncode:
                    raise ConsentError('Vuelve a verificar el almacén.')
                self.validate(json.loads(result.stdout), account, checked)
                if self.expire():
                    raise ConsentError('Sesión vencida.')
            receipt = {'account_matched': True, 'stored_scopes_readonly': True,
                       'refresh_checked': self.check_google, 'effective_scopes_verified': False,
                       'messages_read': False, 'telegram_sent': False, 'worker_activated': False,
                       'verified_at_unix': int(time.time())}
            # Atomic replacement: do not follow an existing receipt symlink or
            # overwrite permissions on an existing inode. No account/secret output.
            if persist:
                fd, temporary = tempfile.mkstemp(prefix='.verify-', dir=self.root)
                try:
                    with os.fdopen(fd, 'w') as out:
                        json.dump(receipt, out)
                    os.replace(temporary, self.root / 'gmail-keyring-verification.json')
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            self.saved = True
            return receipt
        except Exception:
            # Never propagate subprocess stderr, JSON, password or account values.
            raise ConsentError('No se confirmó la verificación. Revisar con Bichi.') from None
        finally:
            env.pop('GOG_KEYRING_PASSWORD', None)
            password = None
            form.clear()

    def page(self, csrf):
        if self.saved:
            body = ('<h1>Comprobación terminada</h1><p>Cuenta y scopes registrados coinciden. '
                    + ('Refresh confirmado con Google. ' if self.check_google else 'Sin contactar Google. ')
                    + 'Scopes efectivos todavía no verificados. No se leyeron correos ni se enviaron avisos. '
                    'Avisá a Bichi para cerrar el portal.</p>')
        elif self.expire() or self.attempts >= 3:
            body = '<h1>Sesión terminada</h1><p>Coordiná una nueva ventana con Bichi.</p>'
        else:
            body = (f'<h1>Verificar almacén Gmail</h1><p>Ingresá la contraseña que ya guardaste en tu gestor. '
                    f'No elijas una nueva. Ventana máxima: 10 minutos, tres intentos. '
                    + ('Se comprobará refresh con Google, sin leer correo.' if self.check_google
                       else 'Solo comprobación local; no se contactará Google.')
                    + f'</p><form method="post" action="/verify"><input type="hidden" name="csrf" value="{csrf}">'
                    '<label>Contraseña del almacén <input type="password" name="password" '
                    'minlength="16" maxlength="1024" autocomplete="current-password" required></label>'
                    '<button>Verificar</button></form>')
        return '<!doctype html><html lang="es"><meta charset="utf-8"><title>Verificar Gmail</title>' + body + '</html>'


def verification_handler(state, public_url):
    return handler_for(state, public_url, page_renderer=state.page,
                       actions={'/verify': state.verify},
                       error_body='<h1>No se confirmó la verificación</h1><p>Revisá con Bichi. No se activó el asistente.</p><a href="/">Volver</a>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-google', action='store_true',
                        help='Permit token refresh only; coordinate with Bichi first')
    args = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    state = Verification(Path.home() / '.config/bichi-mail', check_google=args.check_google)
    server = ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])),
                                 verification_handler(state, os.environ['PUBLIC_URL']))
    # Keep handlers joined on close: never exit while a child holds the password.
    # An in-flight check can take up to two 45s subprocess timeouts to drain.
    def expire():
        while not state.expire() and not state.saved and state.attempts < 3:
            time.sleep(1)
        # Allow the response to finish, then remove the listener.
        time.sleep(2)
        server.shutdown()
    threading.Thread(target=expire, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
