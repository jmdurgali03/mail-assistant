"""One explicitly authorized local auth.list; no refresh, mail or model path."""
import argparse
import html
import json
import os
from pathlib import Path
import re
import resource
import threading
import time

from gmail_consent import ConsentError, ThreadingHTTPServer, handler_for
from gmail_unlock import Verification
from inbox_readonly import InboxProof
from oneshot import private


class LocalAuthOnce(InboxProof):
    def __init__(self, *args, authorized_local_auth_once=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.authorized_local_auth_once = authorized_local_auth_once
        self.receipt.update(contract='local_auth_once_v1', local_auth_validated=False,
                            auth_local_reservations=0, phase='awaiting_post')

    def _gog(self, password, account, command, arguments, counter):
        if (command != 'auth.list' or arguments != ['auth', 'list']
                or counter != 'auth_local_invocations'):
            raise ValueError('only local auth.list is authorized')
        return super()._gog(password, account, command, arguments, counter)

    def _model_run(self, *args, **kwargs):
        raise ValueError('model is forbidden in local auth diagnostics')

    def execute(self, form):
        if self.attempted:
            form.clear()
            raise ConsentError('Intento consumido; no repetir.')
        password = form.get('password', [''])[0]
        claimed = False
        try:
            self.attempted = True
            if not self.authorized_local_auth_once:
                raise ValueError('explicit local auth authorization required')
            if not isinstance(password, str) or not 16 <= len(password) <= 1024:
                raise ValueError('invalid password shape')
            self.local_preflight()
            self._claim()
            claimed = True
            # Durable reservation before even reading the pending account or
            # launching auth. Crash/timeout never frees this separate budget.
            self.receipt.update(auth_local_reservations=1, phase='reserved_before_invocation')
            self._persist_receipt()
            account = private(self.root / 'gmail-account.pending').read_text().strip()
            if not re.fullmatch(r'[\w.+-]+@[\w.-]+', account):
                raise ValueError('invalid pending account')
            result = self._gog(password, account, 'auth.list', ['auth', 'list'],
                               'auth_local_invocations')
            self._validate_auth(Verification(self.root), result, account, False)
            self.receipt.update(local_auth_validated=True, phase='completed')
            self.saved = True
        except Exception:
            self.error = True
            self.receipt.update(local_auth_validated=False, phase='failed')
            raise ConsentError('Comprobación detenida. Revisar diagnóstico con Bichi; no repetir.') from None
        finally:
            password = None
            form.clear()
            if claimed:
                try:
                    self._persist_receipt()
                except Exception:
                    self.saved = False
                    self.receipt['local_auth_validated'] = False
                    raise ConsentError('Recibo no confirmado; no repetir.') from None

    def page(self, csrf):
        if self.saved:
            body = '<h1>Comprobación local terminada</h1><p>Cuenta y permisos registrados válidos. Sin refresh ni lectura de correo.</p>'
        elif self.attempted or self.expire():
            body = '<h1>Comprobación cerrada</h1><p>Revisar con Bichi; no repetir.</p>'
        else:
            body = ('<h1>Comprobar autenticación local una vez</h1>'
                    '<p>Únicamente auth list local. Sin refresh, correo, modelo ni envíos. '
                    'Usá la contraseña existente solo aquí; no la envíes por chat.</p>'
                    '<form method="post" action="/verify-local">'
                    f'<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">'
                    '<label>Contraseña del almacén <input type="password" name="password" '
                    'minlength="16" maxlength="1024" autocomplete="current-password" required></label>'
                    '<button>Comprobar una sola vez</button></form>')
        return '<!doctype html><html lang="es"><meta charset="utf-8"><title>Autenticación local única</title>' + body + '</html>'


def auth_handler(state, public_url):
    return handler_for(state, public_url, page_renderer=state.page,
                       actions={'/verify-local': state.execute},
                       error_body='<h1>Comprobación detenida</h1><p>Revisar con Bichi; no repetir.</p>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'serve-authorized'))
    parser.add_argument('--authorized-local-auth-once', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    state = LocalAuthOnce(Path.home() / '.config/bichi-mail',
                          Path.home() / '.openclaw-bichi-mail-analysis/openclaw.json',
                          Path(__file__).resolve().parent / '.auth-local-proof-state',
                          authorized_local_auth_once=args.authorized_local_auth_once)
    try:
        preflight = state.local_preflight()
        if args.action == 'preflight':
            print(json.dumps(preflight, sort_keys=True))
            return 0
        if not state.authorized_local_auth_once:
            raise ValueError('local authorization required')
        server = ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])),
                                     auth_handler(state, os.environ['PUBLIC_URL']))
        server.daemon_threads = False
        def finish():
            while not state.expire() and not state.attempted:
                time.sleep(0.2)
            server.shutdown()
        threading.Thread(target=finish, daemon=True).start()
        try:
            server.serve_forever()
        finally:
            server.server_close()  # joins the active POST before publishing metadata
            print(json.dumps({'receipt': state.receipt}, sort_keys=True))
            state.clear(force=True)
        return 0 if state.saved else 1
    except Exception:
        print(json.dumps({'local_preflight_or_serve_failed': True, 'private_data_printed': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
