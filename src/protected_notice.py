"""Local, bounded authentication context. Never return raw subjects/bodies/URLs."""
from email.utils import parseaddr
import re
import unicodedata

from assistant import authentication_content, text


def safe_sender(raw):
    name, address = parseaddr(raw)
    # Header display names may themselves contain an OTP or credential.
    def safe(value):
        return (value and not authentication_content(value)
                and not re.search(r'\d{4,}|https?://|[=:]|[A-Za-z0-9_-]{32,}', value)
                and not any(unicodedata.category(c).startswith('C') for c in value))
    valid_address = re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@([A-Za-z0-9.-]+)', address)
    if valid_address:
        visible = address if safe(address) else valid_address[1]
        return text(f'{name} <{visible}>' if safe(name) else visible, 254)
    return text(raw, 100) if safe(raw) else 'Remitente no disponible; consultar Gmail'


def context(subject, sender, body=''):
    # Fixed descriptions, not regex-redacted excerpts: values cannot escape.
    value = (subject + ' ' + body).casefold()
    priority = None
    if re.search(r'actividad sospechosa|suspicious (?:activity|sign.in)|unrecognized (?:activity|sign.in)|(?:acceso|inicio de sesi[oó]n) no autorizad', value):
        title, reason, priority = ('Alerta de seguridad', 'El correo informa actividad sospechosa o no reconocida.', 'alta')
    elif re.search(r'reset|restablec|recuperaci[oó]n|recovery|cambi.{0,20}contrase', value):
        title, reason = 'Recuperación de acceso', 'Correo relacionado con recuperación de acceso o cambio de contraseña.'
    elif re.search(r'api[ _-]?key|secret[ _-]?key|bearer|access[ _-]?token|refresh[ _-]?token|credencial', value):
        title, reason = 'Credenciales de acceso', 'Correo relacionado con credenciales de acceso.'
    elif re.search(r'otp|2fa|mfa|one.time|access code|login code|security code|c[oó]digo.{0,40}(?:acceso|seguridad|sesi[oó]n)|(?:code|c[oó]digo|pin)\s*[:=\-]?\s*[0-9]{4,10}', value):
        title, reason = 'Código de acceso', 'Correo relacionado con un código para iniciar sesión o confirmar el acceso.'
    elif re.search(r'verifica|verification|confirm.{0,25}(?:cuenta|correo|email)', value):
        title, reason = 'Verificación de cuenta', 'Correo relacionado con la verificación de una cuenta o dirección de correo.'
    elif re.search(r'sign.in|magic.link|inicio de sesi[oó]n|iniciar sesi[oó]n', value):
        title, reason = 'Inicio de sesión', 'Correo relacionado con el inicio de sesión o un enlace de acceso.'
    else:
        title, reason = 'Aviso con contenido protegido', 'El correo menciona códigos, claves o autenticación; requiere lectura en Gmail para precisar el motivo.'
    return {'sender': safe_sender(sender), 'title': title,
            'summary': reason + ' Los valores sensibles se omiten.', 'priority': priority,
            'category': 'Seguridad y acceso'}


def analysis(notice):
    return {'summary': notice['summary'], 'priority': notice['priority'],
            'promotional': False, 'financial': False, 'category': notice['category'],
            'notification': {'title': notice['title'], 'action': None}}
