import copy
import json
import unittest

from mail_stages import rendering_prompt, validate_rendering
from notification_labels import catalog, validate_selection
from notifications import card
import test_mail_watch
from test_mail_watch import CFG, metadata, normalize


class LabelContracts(unittest.TestCase):
    def setUp(self):
        self.names = ['ChatGPT', 'Viaje', 'Trabajo', 'Notes', 'Internet', 'Universidad', 'Trámites']
        self.prepared = {'facts': [{'id': 'f1', 'source': 'body', 'quote': 'Viaje a Japón en 2027.'}],
                         'financial_data': None, 'body_truncated': False}
        self.decisions = {'priority': None, 'topic': None, 'financial': False, 'promotional': True}
        self.value = {'title': 'Información del viaje', 'sentences': [
            {'text': 'Informa sobre el viaje a Japón de 2027.', 'fact_ids': ['f1']}],
            'action': None, 'decisions': self.decisions,
            'label': {'kind': 'existing', 'name': 'Viaje', 'fact_ids': ['f1']}}

    def validate(self, names=None):
        return validate_rendering(self.value, self.prepared, self.decisions, 'Viaje',
                                  self.names if names is None else names)

    def test_current_catalog_filters_system_and_workflow_labels(self):
        rows = [{'name': name, 'type': 'user'} for name in self.names + ['Prioridad/Alta', 'Pendiente']]
        rows += [{'name': 'INBOX', 'type': 'system'}, {'name': 'https://bad.invalid', 'type': 'user'}]
        rows += [{'name': 'trabajo', 'type': 'user'}]
        self.assertEqual(set(catalog({'labels': rows})), set(self.names))
        for bad in ({}, {'labels': None}, {'labels': [{'name': 'Trabajo'}]}):
            with self.assertRaises(ValueError): catalog(bad)

    def test_existing_label_independent_from_promotional_category_and_priority(self):
        result = self.validate()
        rendered = card({'subject': 'Viaje', 'sender': 'Agencia', 'thread_id': 'abc1'},
                        result, CFG['account'], 'token')
        self.assertIn('Categoría: Promocional', rendered['text'])
        self.assertIn('🏷️ Etiqueta: Viaje', rendered['text'])
        self.assertIn('🟢 Prioridad baja', rendered['text'])
        self.assertTrue(rendered['text'].startswith('👤 **Agencia**\n**Información del viaje**'))
        self.assertIsNone(result['topic'])  # Jev decisions are never overwritten.
        buttons = rendered['presentation']['blocks'][0]['buttons']
        self.assertEqual([b['label'] for b in buttons], ['Abrir', 'Revisado', 'Después'])
        self.assertEqual([b['action']['type'] for b in buttons], ['url', 'callback', 'callback'])

    def test_new_label_is_display_only_and_empty_catalog_allows_suggestions(self):
        self.value['label'] = {'kind': 'suggested', 'name': 'Viajes', 'fact_ids': ['f1']}
        result = self.validate([])
        self.assertEqual(result['notification']['label'], {'kind': 'suggested', 'name': 'Viajes'})
        rendered = card({'subject': 'Viaje', 'sender': 'Agencia', 'thread_id': 'abc1'},
                        result, CFG['account'], 'token')
        self.assertIn('Etiqueta sugerida: Viajes', rendered['text'])

    def test_case_and_unicode_duplicate_suggestion_reuses_existing_name(self):
        self.value['label'].update(kind='suggested', name='tra\u0301mites')
        self.assertEqual(self.validate()['notification']['label'], {'kind': 'existing', 'name': 'Trámites'})

    def test_invalid_names_unknown_ids_and_fabricated_existing_rejected(self):
        for patch in ({'name': 'Inexistente'}, {'fact_ids': ['f99']}, {'fact_ids': []},
                      {'kind': 'suggested', 'name': 'Prioridad/Alta'},
                      {'kind': 'suggested', 'name': 'https://bad.invalid'},
                      {'kind': 'suggested', 'name': 'Nombre\ninyectado'},
                      {'kind': 'suggested', 'name': 'Código 123456'},
                      {'kind': 'suggested', 'name': 'a' * 61}):
            value = {**self.value['label'], **patch}
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_selection(value, self.names, {'f1': self.prepared['facts'][0]})

    def test_ambiguous_theme_does_not_force_label_and_unavailable_is_distinct(self):
        self.value['label'] = {'kind': 'none', 'name': None, 'fact_ids': []}
        result = self.validate()
        facts = {'subject': 'Viaje', 'sender': 'Agencia', 'thread_id': 'abc1'}
        self.assertIn('Sin etiqueta temática clara', card(facts, result, CFG['account'], 'token')['text'])
        del result['notification']['label']
        self.assertIn('Etiqueta sin evaluar', card(facts, result, CFG['account'], 'token')['text'])

    def test_prompt_receives_live_catalog_and_requires_evidence_without_new_call(self):
        prompt = rendering_prompt(self.prepared, self.decisions, 'Viaje', self.names)
        self.assertIn(json.dumps(self.names, ensure_ascii=False), prompt)
        self.assertIn('nunca crear ni aplicar', prompt)
        self.assertIn('fact_ids', prompt)
        self.assertNotIn('CATÁLOGO', rendering_prompt(self.prepared, self.decisions, 'Viaje'))

    def test_missing_required_selection_and_decision_override_rejected(self):
        original = copy.deepcopy(self.value)
        del self.value['label']
        with self.assertRaises(ValueError): self.validate()
        self.value = original
        self.value['decisions'] = {**self.decisions, 'priority': 'alta'}
        with self.assertRaises(ValueError): self.validate()


class CatalogPipeline(unittest.TestCase):
    def setUp(self):
        self.harness = test_mail_watch.EndToEndTests()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def tick(self, labels, failure=False):
        outer = self
        class Reader:
            def authenticate(self): pass
            def capture(self, *args): return [normalize(metadata())]
            def label_catalog(self):
                outer.calls += 1
                if failure: raise ValueError('synthetic catalog failure')
                return labels
        class Sender:
            def send(self, row):
                outer.harness.sent.append(row)
                return '501'
        return self.harness.watch.tick(Reader(), self.harness.factory, Sender(), self.harness.now)

    def test_current_catalog_flows_through_two_gpt_calls_and_survives_dedupe(self):
        self.calls = 0
        self.harness.f.rendering['label'] = {'kind': 'existing', 'name': 'Trabajo', 'fact_ids': ['f1']}
        self.harness.f.envelope['final'] = json.dumps(self.harness.f.rendering)
        result = self.tick(['Trabajo'])
        self.assertEqual(result['delivery'], {'sent': 1})
        self.assertIn('Etiqueta: Trabajo', json.loads(self.harness.sent[0]['payload'])['text'])
        self.assertEqual(self.harness.items[0].receipt['model_invocations'], 2)
        self.assertEqual(self.harness.items[0].receipt['decision']['invocations'], 1)
        self.assertEqual(self.harness.app.meta('topics'), [])  # No mutation proposals.
        self.tick(['Trabajo'])
        self.assertEqual(self.calls, 1)  # No catalog request for an empty queue.
        self.assertEqual(len(self.harness.sent), 1)

    def test_catalog_failure_discards_stale_names_but_keeps_summary(self):
        self.calls = 0
        self.harness.watch.notification_labels = ['Stale']
        self.tick([], failure=True)
        self.assertIsNone(self.harness.items[0].notification_labels)
        rendered = json.loads(self.harness.sent[0]['payload'])['text']
        self.assertIn('Etiqueta sin evaluar', rendered)
        self.assertIn('Solicita revisar el proyecto', rendered)
        self.assertNotIn('Stale', rendered)


if __name__ == '__main__': unittest.main()
