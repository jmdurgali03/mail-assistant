"""Private one-time setup; no OAuth or email/provider/Telegram network calls.

The operator must explicitly choose the private-file storage mode before entering
the EXISTING keyring password. No value travels through agent tools/chat.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import warnings

from mail_watch import SECRET_NAME, preflight, ReadTransport, provider_diagnostics
from inbox_readonly import json_object
from oneshot import private


def store(path, password):
    if not 16 <= len(password) <= 1024 or '\n' in password or '\r' in password:
        raise ValueError('invalid value')
    # Never overwrite a credential or follow a symlink; setup is not rotation.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(password)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accept-private-file-storage', action='store_true')
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    root = Path.home() / '.config/bichi-mail'
    config, db = root / 'watch.json', Path.home() / '.local/state/bichi-mail/watch.sqlite3'
    value = confirmation = None
    try:
        cfg = json_object(private(config).read_text())
        selected = preflight(cfg, root)
        target = root / SECRET_NAME
        if not args.accept_private_file_storage:
            raise ValueError('explicit storage decision required')
        if not target.exists():
            if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
                raise ValueError('private terminal required')
            print('Se guardará la contraseña EXISTENTE en un archivo local 0600, NO cifrado, '
                  'fuera del repo. systemd la cargará en RAM. Accesible al usuario del servicio/root; '
                  'persiste tras reinicio. No se cambia OAuth ni se registra la entrada.')
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                value = getpass.getpass('Contraseña existente del almacén: ')
                confirmation = getpass.getpass('Repetir contraseña: ')
            if value != confirmation:
                raise ValueError('confirmation mismatch')
            store(target, value)
        private(target)
        value = confirmation = None
        print('Referencia privada preparada. No se contactaron proveedores.')
        if args.activate:
            health = provider_diagnostics()
            import time
            if (not health['openai_profile_present'] or not health['jev_configured']
                    or any((health[k] or 0) > time.time() * 1000 for k in
                           ('openai_cooldown_until_ms', 'openai_disabled_until_ms'))):
                raise ValueError('provider readiness blocked')
            # Local token unwrap/scope validation; not OAuth refresh or Gmail.
            value = target.read_text()
            ReadTransport(selected, cfg['account'], value).authenticate()
            value = None
            # ARM before enabling, persist boundary exactly once. Service cannot
            # self-arm, so installing/starting by accident never scans history.
            subprocess.run([sys.executable, str(Path(__file__).with_name('mail_watch.py')),
                            'arm', '--config', str(config), '--db', str(db)],
                           check=True, stdout=subprocess.DEVNULL)
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
            subprocess.run(['systemctl', '--user', 'enable', '--now', 'bichi-mail.timer'], check=True)
            print('Timer habilitado. Verificar status y primer recibo: timer activo no prueba E2E real.')
        return 0
    except Exception:
        print('Setup detenido. Revisar configuración/consentimiento privado; no se imprimen valores.',
              file=sys.stderr)
        return 1
    finally:
        value = confirmation = None


if __name__ == '__main__':
    raise SystemExit(main())
