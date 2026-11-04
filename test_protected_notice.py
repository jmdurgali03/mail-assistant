import json
import unittest

from mail_watch import normalize
from notifications import card
from protected_notice import context, analysis, safe_sender
from test_mail_watch import metadata, CFG


class ProtectedNoticeTests(unittest.TestCase):
    def test_header_otp_preserves_sender_and_specific_reason_without_secret(self):
        row = metadata()
        row.update(subject='Tu código de acceso es 987654', **{'from': 'Acme <login@acme.example>'})
        facts = normalize(row)
        notice = facts['authentication_notice']
        payload = card(facts, analysis(notice), CFG['account'], 'synthetic-token')
        self.assertIn('Acme', payload['text'])
        self.assertIn('login@acme', payload['text'])
        self.assertIn('Código de acceso', payload['text'])
        self.assertIn('Prioridad baja', payload['text'])
        self.assertIn('Categoría: Seguridad y acceso', payload['text'])
        self.assertNotIn('987654', json.dumps([facts, payload]))

    def test_sender_itself_cannot_smuggle_code_token_or_url(self):
        for raw, secret in [('Código 987654 <login@acme.example>', '987654'),
                            ('Acme <987654@acme.example>', '987654'),
                            ('Acme https://evil.example/secret <mail@acme.example>', 'https://'),
                            ('Acme <abcdefghijklmnopqrstuvwxyz1234567890@acme.example>', 'abcdefghijklmnopqrstuvwxyz1234567890')]:
            with self.subTest(raw=raw):
                self.assertNotIn(secret, safe_sender(raw))
                self.assertIn('acme.example', safe_sender(raw))

    def test_reasons_never_include_password_token_or_magic_link(self):
        for subject, body, title in [
            ('Restablecer contraseña', 'password: SyntheticSecret987', 'Recuperación de acceso'),
            ('Credenciales', 'api_key=sk-synthetic-private-value', 'Credenciales de acceso'),
            ('Verificación de cuenta', 'https://example.invalid/?token=private', 'Verificación de cuenta'),
            ('Inicio de sesión', 'magic link https://example.invalid/secret', 'Inicio de sesión'),
            ('Código promocional', 'Descuento disponible', 'Aviso con contenido protegido')]:
            with self.subTest(subject=subject):
                notice = context(subject, 'Acme <mail@acme.example>', body)
                self.assertEqual(notice['title'], title)
                self.assertIsNone(notice['priority'])
                for secret in ('SyntheticSecret987', 'sk-synthetic', 'https://', 'token=private'):
                    self.assertNotIn(secret, json.dumps(notice))

    def test_boilerplate_not_you_is_not_automatically_high_priority(self):
        self.assertIsNone(context('Código de acceso', 'Acme', 'Si no fuiste vos, ignorá este correo.')['priority'])
        self.assertEqual(context('Alerta de actividad sospechosa', 'Acme')['priority'], 'alta')

    def test_all_card_paths_show_explicit_category_and_priority(self):
        facts = {'subject': 'Mensaje', 'sender': 'Persona', 'thread_id': 'abc1'}
        for result, category in [({'summary': 'Factura', 'financial': True}, 'Finanzas'),
                                 ({'summary': 'Oferta', 'promotional': True}, 'Promocional'),
                                 ({'summary': 'Mensaje'}, 'General'),
                                 ({'summary': 'Revisar', 'review_required': True}, 'General')]:
            rendered = card(facts, result, CFG['account'], 'synthetic-token')['text']
            self.assertIn('Prioridad', rendered)
            self.assertIn('Categoría: ' + category, rendered)


if __name__ == '__main__':
    unittest.main()
