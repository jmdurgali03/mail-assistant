"""On-demand readonly mail session: unlock existing credentials and analyze Inbox.

No scheduler, delivery, OAuth mutation or password persistence. Prior proof ledgers
are never opened. Only fixed diagnostics and counters are written to receipts.
"""
import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import resource
import re
import sys
import warnings

from gmail_consent import ConsentError
from gmail_unlock import Verification
from inbox_readonly import InboxProof, json_object, validate_analysis
from oneshot import private, analysis_config
from pipeline import OpenClawAnalyzer
from jev_decisions import JevDecider, DecisionNeedsReview
from mail_stages import (source_for, preparation_prompt, validate_preparation,
                         rendering_prompt, validate_rendering, validate_decisions)


def credential_root(legacy):
    """Select only a validated, private recovery store; malformed pointers fail closed."""
    legacy = private(Path(legacy), True)
    pointer = legacy / 'readonly-credentials.json'
    if not pointer.exists() and not pointer.is_symlink():
        return legacy
    selected = json_object(private(pointer).read_text())
    if (set(selected) != {'version', 'store'} or type(selected['version']) is not int
            or selected['version'] != 1 or not isinstance(selected['store'], str)
            or not re.fullmatch(r'recovery-[a-f0-9]{24}', selected['store'])):
        raise ValueError('invalid readonly credential selection')
    parent = private(legacy / 'readonly-stores', True)
    root = private(parent / selected['store'], True)
    private(root / 'gog', True)
    private(root / 'gmail-account.pending')
    # A recovery store cannot silently change the authorized account.
    if (private(legacy / 'gmail-account.pending').read_text().strip().casefold()
            != (root / 'gmail-account.pending').read_text().strip().casefold()):
        raise ValueError('recovery account mismatch')
    return root


def auth_error_category(error):
    """Classify in RAM; never return fragments of the untrusted/private error."""
    if not isinstance(error, str) or not error:
        return 'unclassified'
    message = error.lower()
    if 'aes.keyunwrap' in message or 'integrity check failed' in message:
        return 'keyring_password_mismatch_or_corrupt_entry'
    if 'invalid_rapt' in message:
        return 'google_reauthentication_required'
    if 'invalid_grant' in message:
        return 'oauth_grant_expired_or_revoked'
    if 'no tty' in message or 'gog_keyring_password' in message:
        return 'keyring_unlock_required'
    if 'permission denied' in message:
        return 'keyring_permission_denied'
    if 'deadline exceeded' in message or 'timeout' in message:
        return 'auth_timeout'
    return 'unclassified'


MESSAGES = {
    'decision_needs_review':
        'needs_review: Jev no pudo decidir. No se asignó prioridad ni flags de respaldo. '
        'No repitas la lectura; revisar el recibo técnico con Bichi.',
    'preparation_needs_review':
        'needs_review: no se completó la preparación GPT con evidencia verificable. '
        'No se pidió una decisión Jev ni se repite la lectura.',
    'analysis_needs_review':
        'needs_review: no se completó la composición final GPT fiel a Jev. '
        'No repitas la lectura; revisar el recibo técnico con Bichi.',
    'keyring_password_mismatch_or_corrupt_entry':
        'El almacén no pudo descifrarse: la contraseña no coincide o la entrada está dañada. '
        'No repitas esa contraseña; usá la reconexión readonly preparada con Bichi. '
        'No se consultaron correos.',
    'keyring_unlock_required': 'Hace falta desbloquear el almacén con su contraseña original.',
    'google_reauthentication_required': 'Google requiere renovar el inicio de sesión. Bichi preparará el enlace privado.',
    'oauth_grant_expired_or_revoked': 'Google rechazó el permiso guardado por vencimiento o revocación. Bichi preparará la reconexión readonly.',
    'keyring_permission_denied': 'El servidor no puede acceder al almacén. Bichi debe corregir sus permisos.',
    'auth_timeout': 'La autenticación agotó su plazo. Bichi revisará la conexión.',
}


class MailSession(InboxProof):
    def __init__(self, *args, decider=None, **kwargs):
        super().__init__(*args, reviewed_contract=True, accept_scope_limit=True, **kwargs)
        self.auth_attempts = 0
        self.phase = 'awaiting_unlock'
        self.failure = None
        self.decider = decider if decider is not None else JevDecider()
        self.decisions = None
        self.prepared = None
        self.receipt.update(contract='mail_readonly_session_v1', auth_attempts=[],
                            phase=self.phase, analysis_available=False, pipeline_stages=[],
                            pipeline_contract='gpt-jev-gpt-evidence-v1')

    def _model_invocation_allowed(self):
        # Fixed order and one call per GPT stage; never a retry or third turn.
        expected = {'preparing_gpt': 0, 'summarizing_gpt': 1}.get(self.phase)
        return expected is not None and self.receipt['model_invocations'] == expected

    def _validate_model_analysis(self, value):
        if self.phase == 'preparing_gpt':
            return validate_preparation(value, self.preparation_source, self.body_truncated)
        if self.phase == 'summarizing_gpt' and self.prepared is not None:
            return validate_rendering(value, self.prepared, self.decisions,
                                      getattr(self, 'notification_subject', None),
                                      getattr(self, 'notification_labels', None))
        raise ValueError('unexpected GPT stage')

    def _gpt_stage(self, phase, prompt, failure):
        self._phase(phase)
        stage = {'stage': phase, 'provider': 'openai', 'model': 'gpt-6.1-sol', 'status': 'pending'}
        self.receipt['pipeline_stages'].append(stage)
        self._persist_receipt()
        diagnostic_pending = self.receipt.get('diagnostic_review_required', False)
        invocations_before = self.receipt['model_invocations']
        # Previous-stage stderr metadata must not masquerade as this stage's.
        for key in ('model_returncode', 'model_stderr_bytes', 'model_stderr_sha256'):
            self.receipt.pop(key, None)
        try:
            self._within_window()
            analyzer = OpenClawAnalyzer(analysis_config({'analysis_config': str(self.config_path)}),
                                       self._model_run)
            value = analyzer.request_json(prompt)
            validated = self._validate_model_analysis(value)
            stage.update(status='ok')
            return validated
        except (RuntimeError, ValueError, OSError, TypeError, KeyError, RecursionError):
            stage.update(status='needs_review')
            self.failure = failure
            self.receipt['needs_review'] = True
            raise RuntimeError('needs_review: GPT stage failed') from None
        finally:
            stage['invocations'] = self.receipt['model_invocations'] - invocations_before
            for key in ('model_returncode', 'model_stderr_bytes', 'model_stderr_sha256'):
                if key in self.receipt:
                    stage[key] = self.receipt[key]
            self.receipt['diagnostic_review_required'] = (
                diagnostic_pending or self.receipt.get('diagnostic_review_required', False))
            self._persist_receipt()

    def _analyze_message(self, item, content):
        self.notification_subject = item['subject'] if getattr(self, 'concise_notifications', False) else None
        self.preparation_source = source_for(item, content)
        self.body_truncated = content.get('truncated', False)
        self.prepared = self._gpt_stage('preparing_gpt',
            preparation_prompt(self.preparation_source, self.body_truncated), 'preparation_needs_review')
        self._phase('deciding_jev')
        stage = {'stage': 'deciding_jev', 'provider': 'typesafe', 'model': 'jev-1.13.0', 'status': 'pending'}
        self.receipt['pipeline_stages'].append(stage)
        self._persist_receipt()
        try:
            self._within_window()
            self.decisions = validate_decisions(self.decider.decide(self.prepared))
            stage.update(status='ok')
        except (DecisionNeedsReview, ValueError, KeyError, TypeError):
            stage.update(status='needs_review')
            self.failure = 'decision_needs_review'
            self.receipt['needs_review'] = True
            raise DecisionNeedsReview('invalid_or_unavailable') from None
        finally:
            self.receipt['decision'] = self.decider.metadata
            self._persist_receipt()
        value = self._gpt_stage('summarizing_gpt', rendering_prompt(self.prepared, self.decisions,
                                                                 self.notification_subject,
                                                                 getattr(self, 'notification_labels', None)),
                                'analysis_needs_review')
        self.receipt.update(summary_provider='openai', summary_model='gpt-6.1-sol',
                            extraction_provider='openai', extraction_model='gpt-6.1-sol', needs_review=False)
        return value

    def _phase(self, phase):
        self.phase = self.receipt['phase'] = phase
        self._persist_receipt()

    def _validate_auth(self, verifier, result, account, checked):
        rows = result.get('accounts')
        if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict):
            category = auth_error_category(rows[0].get('error'))
            if rows[0].get('error'):
                self.failure = category
                self.receipt['auth_diagnostics'][-1]['cause'] = category
        return super()._validate_auth(verifier, result, account, checked)

    def _functional_passed(self):
        return (self.receipt.get('provider_model_verified') is True
                and self.receipt.get('decision', {}).get('status') == 'ok')

    def execute(self, form):
        if self.saved or self.attempted or self.auth_attempts >= 3:
            form.clear()
            raise ConsentError('Sesión cerrada; no repetir la lectura.')
        password = form.get('password', [''])[0]
        try:
            self._within_window()
            if not isinstance(password, str) or not 16 <= len(password) <= 1024:
                raise ValueError('invalid unlock input')
            if not self.state_dir.exists():
                self.local_preflight()
                self._claim()
            self.auth_attempts += 1
            self.failure = None
            # User-initiated unlock corrections may retry only auth, never mail.
            for key in ('auth_local_invocations', 'auth_refresh_invocations'):
                self.receipt[key] = 0
            self.receipt.pop('auth_diagnostics', None)
            record = {'number': self.auth_attempts, 'phase': 'auth_reserved'}
            self.receipt['auth_attempts'].append(record)
            self._phase('unlocking')
            account = private(self.root / 'gmail-account.pending').read_text().strip()
            if not re.fullmatch(r'[\w.+-]+@[\w.-]+', account):
                raise ValueError('invalid pending account')
            verifier = Verification(self.root)
            try:
                for checked, counter in ((False, 'auth_local_invocations'), (True, 'auth_refresh_invocations')):
                    result = self._gog(password, account, 'auth.list',
                                       ['auth', 'list'] + (['--check', '--timeout=15s'] if checked else []), counter)
                    self._validate_auth(verifier, result, account, checked)
            finally:
                record.update(diagnostics=self.receipt.get('auth_diagnostics', []),
                              local_invocations=self.receipt['auth_local_invocations'],
                              refresh_invocations=self.receipt['auth_refresh_invocations'],
                              phase='auth_failed')
            record['phase'] = 'authenticated'
            self.receipt.pop('failure_category', None)
            self.receipt.update(stored_scopes_readonly=True, refresh_checked=True)
            # Durable reservation BEFORE any mailbox operation; no read replay.
            self.attempted = True
            self._phase('reading_reserved')
            self.read_inbox(password, account)
            self._within_window()
            self.saved = True
            self.receipt.update(functional_passed=self._functional_passed(),
                                analysis_available=True)
            self._phase('completed')
        except Exception:
            self.error = True
            self.saved = False
            self.analysis = None
            self.receipt.update(functional_passed=False, analysis_available=False)
            if self.state_dir.exists():
                self.receipt['failure_category'] = self.failure or 'operation_failed_requires_review'
                try:
                    self._phase('needs_review' if self.failure in ('preparation_needs_review', 'decision_needs_review', 'analysis_needs_review') else
                                'read_failed' if self.attempted else 'unlock_failed')
                except OSError:
                    pass  # Keep reservation; never expose filesystem error details.
            raise ConsentError('No se completó el flujo. Revisá la categoría segura en la terminal.') from None
        finally:
            password = None
            self.prepared = None
            self.preparation_source = None
            form.clear()

def run_terminal(state):
    # Refuse redirected/recorded IO and getpass's echoing fallback.
    if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        print('Abrí una terminal privada interactiva con ssh -t; no redirijas la salida.', file=sys.stderr)
        return 2
    print('Desbloqueo privado. Usá la contraseña del almacén seleccionado, no la de Google.')
    print('A continuación: validar conexión y analizar un correo de Inbox, sin modificarlo.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            password = getpass.getpass('Contraseña del almacén (no se muestra): ')
        form = {'password': [password]}
        password = None
        try:
            state.execute(form)
        finally:
            form.clear()
    except getpass.GetPassWarning:
        print('No se pudo desactivar el eco. No ingreses la contraseña aquí.', file=sys.stderr)
        return 2
    except ConsentError:
        print(MESSAGES.get(state.failure, 'Operación detenida; revisar el recibo de etapas con Bichi.'), file=sys.stderr)
        # One explicit terminal operation, no repeated failed password prompts.
        return 1
    else:
        value = state.analysis or {}
        summary = ''.join(ch for ch in value.get('summary', '') if ch.isprintable() or ch == '\n')
        print('\nAnálisis del correo:\n' + summary)
        if value.get('priority') and '\nPrioridad: ' not in summary:
            print('Prioridad: ' + value['priority'])
        if state.receipt.get('decision', {}).get('status') == 'ok':
            print('GPT-6.1 Sol prepara → Jev 1.13.0 decide → GPT-6.1 Sol compone la salida.')
        print('Solo lectura. Sin envíos ni cambios de correo.')
        print('Recibo técnico (sin contenido privado): ' + str(state.state_dir / 'result.json'))
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'run'))
    args = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    repo = Path(__file__).resolve().parent
    sessions = repo / '.mail-readonly-sessions'
    # preflight never starts authentication or creates an operation ledger.
    try:
        state = MailSession(credential_root(Path.home() / '.config/bichi-mail'),
                            Path.home() / '.openclaw-bichi-mail-analysis/openclaw.json',
                            sessions / ('preflight' if args.action == 'preflight' else
                                        'session-' + os.urandom(12).hex()))
        metadata = state.local_preflight()
        if args.action == 'preflight':
            print(json.dumps(metadata, sort_keys=True))
            return 0
        sessions.mkdir(mode=0o700, exist_ok=True)
        private(sessions, True)
        # Only one operational reader may be active; no parallel mail selection.
        lock_fd = os.open(sessions / 'session.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            return run_terminal(state)
        finally:
            state.clear(force=True)
            os.close(lock_fd)
    except (KeyboardInterrupt, EOFError):
        print('Cancelado; contraseña no guardada.', file=sys.stderr)
        return 130
    except Exception:
        print('No se pudo iniciar el flujo. Revisar configuración privada con Bichi.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
