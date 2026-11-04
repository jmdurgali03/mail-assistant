"""Compact Telegram cards. Presentation is data, never an executable email action."""
import json
import re
from urllib.parse import quote, urlencode

FORMAT = 'bichi-mail-notice-v2'


def gmail_url(account, thread_id, rfc_message_id=None):
    base = 'https://mail.google.com/mail/?' + urlencode({'authuser': account})
    # RFC Message-ID search survives Gmail web/API ID differences and archived mail.
    if rfc_message_id:
        return base + '#search/' + quote('rfc822msgid:' + rfc_message_id.strip('<>'), safe='')
    return base + '#all/' + quote(thread_id, safe='')


def md(value):
    # Email/model content must not create Markdown links, HTML or controls.
    value = value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return re.sub(r'([\\`*_{}\[\]()#+.!|~\-])', r'\\\1', value)


def controls(token, url):
    return [{'type': 'buttons', 'buttons': [
        {'label': 'Abrir', 'action': {'type': 'url', 'url': url}},
        {'label': 'Revisado', 'action': {'type': 'callback', 'value': f'bm2:done:{token}'}},
        {'label': 'Después', 'action': {'type': 'callback', 'value': f'bm2:later:{token}'}},
    ]}]


def card(facts, result, account, token, rfc_message_id=None):
    notice = result.get('notification') or {}
    subject = re.sub(r'https?://\S+|www\.\S+', 'Enlace', facts['subject'])
    title = notice.get('title') or re.sub(r'^(?:(?:re|fw|fwd):\s*)+', '', subject, flags=re.I)[:70] or 'Nuevo correo'
    sender = facts['sender']
    summary = result['summary']
    priority = result.get('priority')
    if result.get('review_required'):
        label = '⚪ Prioridad sin evaluar'
    else:
        label = {'alta': '🔴 Prioridad alta', 'media': '🟡 Prioridad media', None: '🟢 Prioridad baja'}[priority]
    category = (result.get('category') or facts.get('notification_category')
                or ('Seguridad y acceso' if facts.get('authentication') else None)
                or ('Finanzas' if result.get('financial') else None)
                or ('Promocional' if result.get('promotional') else None)
                or 'General')
    theme = notice.get('label') or {}
    if theme.get('kind') == 'existing':
        theme_line = '🏷️ Etiqueta: ' + md(theme['name'])
    elif theme.get('kind') == 'suggested':
        theme_line = '🏷️ Etiqueta sugerida: ' + md(theme['name'])
    elif theme.get('kind') == 'none':
        theme_line = '🏷️ Sin etiqueta temática clara'
    else:
        theme_line = '🏷️ Etiqueta sin evaluar'
    lines = [f'👤 **{md(sender)}**', f'**{md(title)}**', '',
             label + ' · Categoría: ' + md(category), theme_line, '', md(summary)]
    if notice.get('action'):
        lines += ['', '🎯 ' + md(notice['action'])]
    url = gmail_url(account, facts['thread_id'], rfc_message_id)
    return {'format': FORMAT, 'text': '\n'.join(lines),
            'presentation': {'blocks': controls(token, url)}}


def decode(payload):
    if not payload.startswith('{'):
        return None
    data = json.loads(payload)
    if (set(data) != {'format', 'text', 'presentation'} or data['format'] != FORMAT
            or not isinstance(data['text'], str) or not isinstance(data['presentation'], dict)):
        raise ValueError('invalid notification payload')
    return data
