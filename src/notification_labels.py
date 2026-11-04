"""Read-only thematic label catalog and evidence-backed display suggestions."""
import re
import unicodedata

from assistant import authentication_content


def key(name):
    return unicodedata.normalize('NFKC', name).casefold()


def safe_name(name, maximum=200):
    return (isinstance(name, str) and 1 <= len(name) <= maximum
            and name == name.strip() and all(c.isprintable() for c in name)
            and not any(unicodedata.category(c).startswith('C') for c in name)
            and not re.search(r'https?://|www\.|[<>`]', name)
            and not authentication_content(name))


def thematic(name):
    normalized = key(name)
    return (normalized not in {'prioridad', 'pendiente'}
            and not normalized.startswith('prioridad/'))


def catalog(data):
    rows = data.get('labels') if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError('invalid label catalog')
    names = {}
    for row in rows:
        if (not isinstance(row, dict) or row.get('type') not in {'system', 'user'}
                or not isinstance(row.get('name'), str)):
            raise ValueError('invalid catalog entry')
        name = row['name']
        if row['type'] == 'user' and safe_name(name) and thematic(name):
            names.setdefault(key(name), name)
    return sorted(names.values(), key=key)


def validate_selection(value, names, facts):
    if (not isinstance(value, dict) or set(value) != {'kind', 'name', 'fact_ids'}
            or value['kind'] not in {'existing', 'suggested', 'none'}
            or not isinstance(value['fact_ids'], list)
            or any(not isinstance(fid, str) or fid not in facts for fid in value['fact_ids'])
            or len(set(value['fact_ids'])) != len(value['fact_ids'])):
        raise ValueError('invalid label selection')
    kind, name = value['kind'], value['name']
    if kind == 'none':
        if name is not None or value['fact_ids']:
            raise ValueError('invalid empty label selection')
        return {'kind': 'none', 'name': None}
    if not value['fact_ids'] or not safe_name(name) or not thematic(name):
        raise ValueError('unsupported label selection')
    existing = {key(n): n for n in names}
    if key(name) in existing:
        # Case/Unicode variants must never become duplicate suggestions.
        return {'kind': 'existing', 'name': existing[key(name)]}
    if kind == 'existing' or len(name) > 60:
        raise ValueError('label not in current catalog or suggestion too long')
    return {'kind': 'suggested', 'name': name}


def validate_display(value):
    """Recheck the minimized result at the final session boundary (no fact quotes)."""
    if (not isinstance(value, dict) or set(value) != {'kind', 'name'}
            or value['kind'] not in {'existing', 'suggested', 'none'}):
        raise ValueError('invalid display label')
    if value['kind'] == 'none':
        if value['name'] is not None:
            raise ValueError('invalid empty display label')
    elif (not safe_name(value['name'], 60 if value['kind'] == 'suggested' else 200)
          or not thematic(value['name'])):
        raise ValueError('invalid display label name')
    return value
