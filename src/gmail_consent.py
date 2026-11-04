"""Temporary loopback app behind an authenticated HTTPS OpenClaw portal.

Only performs gog OAuth registration/exchange. No mail API commands or job controls.
Passwords and redirect URLs are never written to disk or application logs.
"""
import html
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

READONLY = 'https://www.googleapis.com/auth/gmail.readonly'
IDENTITY = {'openid', 'email', 'https://www.googleapis.com/auth/userinfo.email'}
REJECTED = ('<h1>Solicitud rechazada</h1><p>Volvé a abrir o recargar el portal '
            'en una pestaña nueva e intentá otra vez.</p><a href="/">Volver</a>')


class ConsentError(Exception):
    pass


def oauth_query(url):
    # Do not validate one value while the OAuth client could consume another.
    if url.fragment:
        raise ConsentError('URL OAuth ambigua.')
    query = parse_qs(url.query, keep_blank_values=True, max_num_fields=32)
    if any(len(values) != 1 or not values[0] for values in query.values()):
        raise ConsentError('Parámetros OAuth ambiguos.')
    return query


class Consent:
    def __init__(self, root, runner=subprocess.run, clock=time.monotonic):
        self.root, self.runner, self.clock = Path(root), runner, clock
        self.password = None
        self.url = None
        self.deadline = 0
        self.saved = False
        self.error = False
        self.lock = threading.Lock()

    def clear(self):
        self.password = self.url = None
        self.deadline = 0

    def expire(self):
        if self.password is not None and self.clock() >= self.deadline:
            self.clear()
            self.error = True

    def run(self, args, password):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GOG_')}
        env.update(GOG_HOME=str(self.root / 'gog'), GOG_KEYRING_BACKEND='file',
                   GOG_KEYRING_PASSWORD=password)
        base = [str(Path.home() / '.local/bin/gog'), '--home', str(self.root / 'gog'),
                '--client', 'default', '--json', '--no-input']
        try:
            result = self.runner(base + args, env=env, capture_output=True,
                                 text=True, timeout=60)
            if result.returncode:
                raise ConsentError('No se pudo completar esta etapa con Google o el almacén.')
            return json.loads(result.stdout)
        finally:
            env.pop('GOG_KEYRING_PASSWORD', None)

    def args(self):
        return ['auth', 'add', self.account, '--services', 'gmail', '--gmail-scope',
                'readonly', '--readonly', '--gmail-no-send', '--remote']

    def begin(self, password, confirmation):
        self.expire()
        if self.saved or self.password is not None:
            raise ConsentError('Ya hay un consentimiento iniciado o terminado.')
        if len(password) < 16 or password != confirmation:
            raise ConsentError('Usá una contraseña de al menos 16 caracteres y repetila igual.')
        for name in ['google-desktop-client.json', 'gmail-account.pending']:
            p = self.root / name
            if p.is_symlink() or stat.S_IMODE(p.stat().st_mode) != 0o600:
                raise ConsentError('Revisar permisos de la configuración privada del servidor.')
        client = json.loads((self.root / 'google-desktop-client.json').read_text())['installed']
        if (client.get('auth_uri') != 'https://accounts.google.com/o/oauth2/auth'
                or client.get('token_uri') != 'https://oauth2.googleapis.com/token'):
            raise ConsentError('El cliente no usa los endpoints de Google esperados.')
        self.account = (self.root / 'gmail-account.pending').read_text().strip()
        saved = self.run(['auth', 'credentials', 'set',
                          str(self.root / 'google-desktop-client.json')], password)
        if not saved.get('saved') or not saved.get('client_secret_in_keyring'):
            raise ConsentError('No se confirmó el cliente cifrado.')
        url = self.run(self.args() + ['--step', '1'], password)['auth_url']
        u = urlsplit(url)
        q = oauth_query(u)
        scopes = set(q.get('scope', [''])[0].split())
        redirect = urlsplit(q.get('redirect_uri', [''])[0])
        if (u.scheme != 'https' or u.netloc != 'accounts.google.com'
                or q.get('client_id') != [client['client_id']]
                or not q.get('state') or q.get('response_type') != ['code']
                or READONLY not in scopes or scopes - {READONLY} - IDENTITY
                or q.get('include_granted_scopes', ['false']) != ['false']
                or redirect.scheme != 'http'
                or redirect.hostname not in {'localhost', '127.0.0.1', '::1'}):
            raise ConsentError('El enlace no cumple la política Gmail de solo lectura.')
        self.password, self.url = password, url
        self.deadline = self.clock() + 600
        self.error = False

    def finish(self, redirect):
        self.expire()
        if self.saved or self.password is None:
            raise ConsentError('La sesión venció o ya terminó. Volvé a iniciar si hace falta.')
        try:
            expected_query = oauth_query(urlsplit(self.url))
            expected = urlsplit(expected_query['redirect_uri'][0])
            actual = urlsplit(redirect.strip())
            params = oauth_query(actual)
            if ((actual.scheme, actual.netloc, actual.path) !=
                    (expected.scheme, expected.netloc, expected.path)
                    or params.get('state') != expected_query['state']
                    or not params.get('code')):
                raise ConsentError('La redirección no coincide; no se intercambió el código.')
            result = self.run(self.args() + ['--step', '2', '--auth-url', redirect.strip()],
                              self.password)
            if (not result.get('stored') or result.get('services') != ['gmail']
                    or result.get('email', '').casefold() != self.account.casefold()):
                raise ConsentError('El resultado necesita revisión antes de cualquier lectura.')
            self.saved = True
            receipt = self.root / 'gmail-remote-consent-receipt.json'
            fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump({'oauth_stored': True, 'account_matched': True,
                           'service': 'gmail', 'requested_data_scope': READONLY,
                           'messages_read': False, 'worker_activated': False}, f)
        finally:
            self.clear()


def handler_for(consent, public_url, *, page_renderer=None, actions=None,
                error_body=None):
    public = urlsplit(public_url)
    if (public.scheme != 'https' or not public.hostname or public.username
            or public.password or public.fragment
            or public.path not in {'', '/'} or public.query):
        raise ValueError('A token-free root HTTPS PUBLIC_URL is required')
    origin = f'{public.scheme}://{public.netloc}'
    session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass

        def reply(self, status, body, set_cookie=False):
            payload = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy',
                             "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
            if set_cookie:
                # OpenClaw's HTTPS portal can embed this page cross-site. Its proxy
                # partitions backend cookies; Strict would suppress the session
                # cookie on the form POST while the portal auth cookie still works.
                self.send_header('Set-Cookie', f'consent_session={session}; Secure; HttpOnly; SameSite=None; Partitioned; Path=/')
            self.end_headers()
            self.wfile.write(payload)

        def page(self):
            consent.expire()
            if page_renderer is not None:
                return page_renderer(csrf)
            hidden = f'<input type="hidden" name="csrf" value="{csrf}">'
            if consent.saved:
                content = '<h2>Consentimiento guardado</h2><p>No se leyeron correos ni se activó el asistente. Avisá a Bichi.</p>'
            elif consent.url:
                link = html.escape(consent.url, quote=True)
                content = f'''<h2>2. Autorizá en Google</h2><p><a target="_blank" rel="noopener noreferrer" href="{link}">Abrir consentimiento de Google</a></p>
<p>Elegí la cuenta confirmada y revisá el acceso Gmail de solo lectura. Si la aplicación está en pruebas, tu cuenta debe estar habilitada como usuario de prueba.</p>
<h2>3. Completá la conexión</h2><p>Al terminar, el navegador irá a localhost y puede mostrar un error de conexión. Copiá la URL completa de esa barra y pegala aquí, no en el chat. Tenés hasta 10 minutos; si Google indica vencimiento, reiniciá.</p>
<form method="post" action="/finish">{hidden}<label>URL final de Google <input type="password" name="redirect" autocomplete="off" required></label><button>Completar consentimiento</button></form>'''
            else:
                note = '<p>El intento anterior venció o falló. Podés reintentar con la misma contraseña del almacén.</p>' if consent.error else ''
                content = f'''{note}<h2>1. Protegé el almacén</h2><p>Elegí una contraseña de al menos 16 caracteres y guardala en tu gestor de contraseñas. Si ya la definiste en un intento previo, usá la misma.</p>
<p>Se usa para cifrar las credenciales en el servidor. Esta página la mantiene temporalmente en memoria, no la guarda en archivos ni en el chat. Hará falta de nuevo para desbloquear el almacén en el futuro.</p>
<form method="post" action="/start">{hidden}<label>Contraseña <input type="password" name="password" minlength="16" autocomplete="new-password" required></label>
<label>Repetir contraseña <input type="password" name="confirmation" minlength="16" autocomplete="new-password" required></label><button>Preparar acceso solo lectura</button></form>'''
            return '''<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Conectar Gmail</title><style>body{font:17px system-ui;max-width:660px;margin:40px auto;padding:0 20px;color:#202124;line-height:1.5}input{display:block;box-sizing:border-box;width:100%;padding:12px;margin:8px 0 18px}button{padding:12px;background:#6544b8;color:white;border:0;border-radius:7px;cursor:pointer}a{color:#6544b8}h1{font-size:28px}</style><h1>Conectar Gmail de forma privada</h1><p>Solo lectura. El asistente y su programación permanecen apagados.</p>''' + content + '</html>'

        def do_GET(self):
            if urlsplit(self.path).path != '/':
                return self.reply(404, 'No encontrado')
            with consent.lock:
                self.reply(200, self.page(), set_cookie=True)

        def do_POST(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
                valid_cookie = secrets.compare_digest(cookie['consent_session'].value, session)
                length = int(self.headers.get('Content-Length', '0'))
                request_origin = self.headers.get('Origin')
                # The portal forces no-referrer. Native form POSTs consequently
                # carry Origin: null (Fetch's append-request-Origin algorithm).
                # Accept that case only with browser-controlled same-origin
                # metadata, never same-site/cross-site/missing metadata. Session
                # cookie and the secret form CSRF token remain mandatory below.
                valid_origin = request_origin == origin or (
                    request_origin == 'null'
                    and self.headers.get('Sec-Fetch-Site') == 'same-origin')
                if (not valid_cookie or not valid_origin
                        or self.headers.get_content_type() != 'application/x-www-form-urlencoded'
                        or not 0 < length <= 16384):
                    return self.reply(403, REJECTED)
                form = parse_qs(self.rfile.read(length).decode(), max_num_fields=4,
                                keep_blank_values=True)
                if any(len(values) != 1 for values in form.values()):
                    return self.reply(403, REJECTED)
                if not secrets.compare_digest(form.get('csrf', [''])[0], csrf):
                    return self.reply(403, REJECTED)
            except (KeyError, ValueError):
                return self.reply(403, REJECTED)
            with consent.lock:
                try:
                    if actions is not None:
                        action = actions.get(self.path)
                        if action is None:
                            return self.reply(404, 'No encontrado')
                        action(form)
                    elif self.path == '/start':
                        consent.begin(form.get('password', [''])[0], form.get('confirmation', [''])[0])
                    elif self.path == '/finish':
                        consent.finish(form.get('redirect', [''])[0])
                    else:
                        return self.reply(404, 'No encontrado')
                except (ConsentError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                    consent.clear()
                    consent.error = True
                    return self.reply(400, error_body or '<h1>No se completó el consentimiento</h1><p>Revisá la contraseña del almacén y la configuración de Google (API Gmail y usuario de prueba). No se activó el asistente.</p><a href="/">Volver</a>')
                self.reply(200, self.page(), set_cookie=True)

    return Handler


def main():
    os.umask(0o077)
    state = Consent(Path.home() / '.config/bichi-mail')
    server = ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])),
                                 handler_for(state, os.environ['PUBLIC_URL']))
    # Bound lifetime, no durable background schedule. Password expires after 10m.
    def expire():
        for _ in range(360):
            time.sleep(10)
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
