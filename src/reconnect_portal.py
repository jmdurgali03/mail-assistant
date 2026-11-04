"""Bounded private renewal of the selected Gmail grant using the existing keyring.

Reuses gog remote OAuth and the consent app's CSRF/origin controls. Does not
read mail, call models, change the credential pointer, or activate the worker.
"""
import html
import json
import os
from pathlib import Path
import resource
import threading
import time

from gmail_consent import ConsentError, ThreadingHTTPServer, handler_for
from gmail_unlock import Verification
from mail_reconnect import RecoveryConsent
from mail_watch import preflight
from oneshot import private


class Renewal(RecoveryConsent):
    def __init__(self, legacy, *, runner=None):
        self.legacy = Path(legacy)
        cfg = json.loads(private(self.legacy / 'watch.json').read_text())
        root = preflight(cfg, self.legacy)
        kwargs = {} if runner is None else {'runner': runner}
        super().__init__(root, **kwargs)
        self.verification = None
        self.opened = self.clock()

    def start(self, form):
        password = None
        try:
            password = private(self.legacy / 'gmail-keyring-password').read_text()
            self.begin(password, password)
        finally:
            password = None
            form.clear()

    def complete(self, form):
        password = None
        try:
            self.finish(form.get('redirect', [''])[0])
            password = private(self.legacy / 'gmail-keyring-password').read_text()
            check = Verification(self.root, check_google=True, runner=self.process_runner)
            self.verification = check.verify({'password': [password]})
        finally:
            password = None
            form.clear()

    def page(self, csrf):
        hidden = '<input type="hidden" name="csrf" value="' + html.escape(csrf, quote=True) + '">'
        if self.verification:
            body = '<h1>Gmail reconectado</h1><p>Cuenta y permisos registrados de solo lectura comprobados. Renovación con Google confirmada. Avisale a Bichi para reanudar el asistente.</p>'
        elif self.saved:
            body = '<h1>Autorización guardada</h1><p>La comprobación posterior no terminó. Avisale a Bichi; no repitas el consentimiento.</p>'
        elif self.url:
            body = ('<h1>1. Autorizá en Google</h1><p><a target="_blank" rel="noopener noreferrer" href="'
                    + html.escape(self.url, quote=True) + '">Abrir autorización de Google</a></p>'
                    '<p>Elegí tu cuenta del asistente y permití solo lectura de Gmail.</p>'
                    '<h2>2. Completá la reconexión</h2><p>Al finalizar, Google te enviará a localhost. '
                    'Si aparece un error de conexión, es esperado: copiá la URL completa de la barra '
                    'y pegala abajo, no en el chat. El enlace vence en 10 minutos.</p>'
                    '<form method="post" action="/finish">' + hidden
                    + '<label>URL final de localhost<input type="password" name="redirect" autocomplete="off" required></label>'
                    '<button>Completar reconexión</button></form>')
        elif self.error:
            body = '<h1>No se completó esta etapa</h1><p>Avisale a Bichi para revisar el estado antes de volver a intentar.</p>'
        else:
            body = ('<h1>Reconectar Gmail</h1><p>Se reutiliza el almacén actual. No necesitás ingresar contraseñas aquí.</p>'
                    '<form method="post" action="/start">' + hidden
                    + '<button>Preparar autorización de Google</button></form>')
        return ('<!doctype html><html lang="es"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>Reconectar Gmail</title><style>body{font:17px system-ui;max-width:680px;'
                'margin:40px auto;padding:0 20px;line-height:1.5}input{display:block;width:100%;'
                'box-sizing:border-box;padding:12px;margin:10px 0 20px}button{padding:12px 20px;'
                'background:#6544b8;color:white;border:0;border-radius:8px}a{color:#6544b8}</style>'
                + body + '<p>El asistente permanece pausado durante este paso. No se leen correos ni se envían avisos.</p></html>')


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    state = Renewal(Path.home() / '.config/bichi-mail')
    server = ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])), handler_for(
        state, os.environ['PUBLIC_URL'], page_renderer=state.page,
        actions={'/start': state.start, '/finish': state.complete},
        error_body='<h1>No se completó esta etapa</h1><p>Avisale a Bichi; no pegues contraseñas ni URLs en el chat.</p><a href="/">Volver</a>'))

    def expire():
        while time.monotonic() - state.opened < 900:
            time.sleep(1)
            with state.lock:
                state.expire()
        server.shutdown()

    threading.Thread(target=expire, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        state.clear()
        server.server_close()


if __name__ == '__main__':
    main()
