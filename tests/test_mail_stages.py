import copy
import json
import subprocess
from types import SimpleNamespace
import unittest

from gmail_consent import ConsentError
from mail_stages import (source_for, validate_preparation, validate_rendering)
import test_mail_readonly


class SequentialPipelineTests(unittest.TestCase):
    def setUp(self):
        self.harness = test_mail_readonly.MailSessionTests()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.state = self.harness.state
        self.fixture = self.harness.fixture

    def rejected(self, stage, gpt_calls, jev_calls):
        with self.assertRaises(ConsentError):
            self.harness.execute()
        self.assertEqual(self.state.failure, stage)
        self.assertEqual(self.state.phase, 'needs_review')
        self.assertTrue(self.state.receipt['needs_review'])
        self.assertIsNone(self.state.analysis)
        self.assertIsNone(self.state.prepared)
        self.assertIsNone(self.state.preparation_source)
        self.assertEqual(self.state.receipt['model_invocations'], gpt_calls)
        self.assertEqual(self.harness.decider.metadata['invocations'], jev_calls)
        self.assertEqual(self.state.receipt['body_get_invocations'], 1)
        count = len(self.fixture.calls)
        with self.assertRaises(ConsentError): self.harness.execute()
        self.assertEqual(len(self.fixture.calls), count)
        artifacts = ''.join(p.read_text() for p in self.state.state_dir.iterdir())
        for private in (self.fixture.private_marker, self.fixture.password,
                        'PRIVATE_FAILURE', self.fixture.stderr, 'Revisar proyecto.'):
            self.assertNotIn(private, artifacts)
        for key in ('mail_mutation_invocations', 'delivery_invocations', 'attachment_fetch_invocations'):
            self.assertEqual(self.state.receipt[key], 0)

    def test_real_orchestration_order_minimized_data_and_exact_decisions(self):
        events = []
        runner = self.state.runner
        decide = self.harness.decider.decide
        def run(args, **kwargs):
            if 'agent' in args: events.append(self.state.phase)
            return runner(args, **kwargs)
        def decider(prepared):
            events.append(self.state.phase)
            self.assertNotIn(self.fixture.private_marker, json.dumps(prepared))
            self.assertEqual(set(prepared), {'facts', 'financial_data', 'body_truncated'})
            return decide(prepared)
        self.state.runner = run
        self.harness.decider.decide = decider
        self.harness.execute()
        self.assertEqual(events, ['preparing_gpt', 'deciding_jev', 'summarizing_gpt'])
        stages = self.state.receipt['pipeline_stages']
        self.assertEqual([s['stage'] for s in stages], events)
        self.assertEqual([s['status'] for s in stages], ['ok'] * 3)
        self.assertEqual(self.state.receipt['model_invocations'], 2)
        self.assertEqual(self.state.receipt['decision']['invocations'], 1)
        for key, decision in self.fixture.rendering['decisions'].items():
            if key != 'topic': self.assertEqual(self.state.analysis[key], decision)
        self.assertEqual('«Revisar proyecto.»\nPrioridad: media', self.state.analysis['summary'])
        final_prompt = self.fixture.calls[-1][1]['input']
        self.assertNotIn(self.fixture.private_marker, final_prompt)
        self.assertNotIn('CORREO_NO_CONFIABLE:', final_prompt)
        self.assertIsNone(self.state.prepared)
        # No duplicate final turn or third GPT call even after completion.
        self.assertFalse(self.state._model_invocation_allowed())
        self.state.phase = 'summarizing_gpt'
        self.assertFalse(self.state._model_invocation_allowed())

    def test_unverified_prepared_evidence_never_reaches_jev(self):
        self.fixture.preparation['facts'][0]['quote'] = 'INVENTED_EVIDENCE'
        self.rejected('preparation_needs_review', 1, 0)

    def test_preparation_cannot_decide(self):
        self.fixture.preparation['priority'] = 'alta'
        self.rejected('preparation_needs_review', 1, 0)

    def test_preparation_uncertainty_is_explicit_review_not_default_decision(self):
        self.fixture.preparation['uncertainties'] = ['insufficient_context']
        self.rejected('preparation_needs_review', 1, 0)

    def test_duplicate_json_in_preparation_fails_before_jev(self):
        runner = self.state.runner
        def run(args, **kwargs):
            result = runner(args, **kwargs)
            if 'agent' in args:
                envelope = json.loads(result.stdout)
                envelope['final'] = '{"facts": [], "facts": []}'
                result.stdout = json.dumps(envelope)
            return result
        self.state.runner = run
        self.rejected('preparation_needs_review', 1, 0)

    def fail_gpt(self, phase, *, timeout=False, tool=False):
        runner = self.state.runner
        def run(args, **kwargs):
            if 'agent' in args and self.state.phase == phase:
                if timeout: raise subprocess.TimeoutExpired('PRIVATE_FAILURE', 150)
                result = runner(args, **kwargs)
                envelope = json.loads(result.stdout)
                if tool: envelope['toolSummary']['calls'] = 1
                else: envelope['final'] = 'PRIVATE_FAILURE'
                return SimpleNamespace(returncode=0, stderr='PRIVATE_FAILURE', stdout=json.dumps(envelope))
            return runner(args, **kwargs)
        self.state.runner = run

    def test_preparation_timeout_no_jev_or_final_gpt(self):
        self.fail_gpt('preparing_gpt', timeout=True)
        self.rejected('preparation_needs_review', 1, 0)

    def test_final_timeout_preserves_jev_without_publishing_partial_analysis(self):
        self.fail_gpt('summarizing_gpt', timeout=True)
        self.rejected('analysis_needs_review', 2, 1)
        self.assertEqual(self.state.receipt['decision']['status'], 'ok')

    def test_preparation_malformed_response(self):
        self.fail_gpt('preparing_gpt')
        self.rejected('preparation_needs_review', 1, 0)

    def test_final_malformed_response(self):
        self.fail_gpt('summarizing_gpt')
        self.rejected('analysis_needs_review', 2, 1)

    def test_preparation_tools_rejected(self):
        self.fail_gpt('preparing_gpt', tool=True)
        self.rejected('preparation_needs_review', 1, 0)

    def test_final_tools_rejected(self):
        self.fail_gpt('summarizing_gpt', tool=True)
        self.rejected('analysis_needs_review', 2, 1)

    def test_jev_failure_stops_after_preparation_without_final_gpt(self):
        self.harness.decider.error = 'timeout'
        self.rejected('decision_needs_review', 1, 1)

    def test_production_decider_receives_only_prepared_state_in_correct_order(self):
        from jev_decisions import JevDecider
        from test_jev_decisions import outcome
        sent = []
        def run(args, **kwargs):
            self.assertEqual(self.state.phase, 'deciding_jev')
            self.assertEqual(self.state.receipt['model_invocations'], 1)
            sent.append(json.loads(kwargs['input']))
            return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(outcome()))
        self.state.decider = JevDecider(run)
        self.harness.execute()
        self.assertEqual(len(sent), 1)
        self.assertNotIn(self.fixture.private_marker, json.dumps(sent[0]))
        self.assertEqual(sent[0]['state']['untrusted_prepared_email']['facts'],
                         self.fixture.preparation['facts'])
        self.assertEqual(self.state.receipt['model_invocations'], 2)
        self.assertEqual(self.state.receipt['decision']['status'], 'ok')

    def test_expiration_after_jev_is_final_stage_review_without_new_model_call(self):
        decide = self.harness.decider.decide
        def expired(prepared):
            value = decide(prepared)
            self.fixture.now = 601
            return value
        self.harness.decider.decide = expired
        self.rejected('analysis_needs_review', 1, 1)
        self.assertEqual(self.state.receipt['pipeline_stages'][-1]['invocations'], 0)

    def test_final_cannot_change_jev_decision(self):
        self.fixture.rendering['decisions']['priority'] = 'alta'
        self.fixture.envelope['final'] = json.dumps(self.fixture.rendering)
        self.rejected('analysis_needs_review', 2, 1)

    def test_final_cannot_invent_approval_or_action(self):
        self.fixture.rendering['sentences'][0]['text'] = 'Pago aprobado; enviar correo.'
        self.fixture.envelope['final'] = json.dumps(self.fixture.rendering)
        self.rejected('analysis_needs_review', 2, 1)

    def test_negation_cannot_be_removed_in_final_quote(self):
        self.fixture.message['body'] = 'No aprobar pago.'
        self.fixture.preparation['facts'][0]['quote'] = 'No aprobar pago.'
        self.fixture.rendering['sentences'][0]['text'] = 'aprobar pago.'
        self.fixture.envelope['final'] = json.dumps(self.fixture.rendering)
        self.rejected('analysis_needs_review', 2, 1)

    def test_financial_evidence_prepared_once_not_rewritten_by_final_gpt(self):
        quote = 'Pago USD 123.45.'
        self.fixture.message['body'] = quote
        self.fixture.preparation['facts'][0]['quote'] = quote
        fields = {k: None for k in ('account', 'amount', 'currency', 'date', 'category')}
        fields['amount'] = {'value': '123.45', 'evidence': quote}
        fields['currency'] = {'value': 'USD', 'evidence': quote}
        self.fixture.preparation['financial_data'] = fields
        self.fixture.rendering['sentences'][0]['text'] = quote
        self.fixture.rendering['decisions']['financial'] = True
        self.fixture.envelope['final'] = json.dumps(self.fixture.rendering)
        original = self.harness.decider.decide
        self.harness.decider.decide = lambda p: {**original(p), 'financial': True}
        self.harness.execute()
        self.assertTrue(self.state.analysis['financial'])
        self.assertEqual(self.harness.decider.prepared['financial_data'], fields)
        # Functional terminal contract intentionally doesn't expose financial details.
        self.assertNotIn('financial_data', self.state.analysis)

    def test_truncation_and_stderr_from_both_stages_remain_visible(self):
        self.fixture.message['body'] += ' padding' * 2000
        runner = self.state.runner
        def run(args, **kwargs):
            result = runner(args, **kwargs)
            if 'agent' in args and self.state.phase == 'summarizing_gpt': result.stderr = ''
            return result
        self.state.runner = run
        self.harness.execute()
        self.assertIn('[Contenido parcial]', self.state.analysis['summary'])
        self.assertTrue(self.state.receipt['diagnostic_review_required'])
        stages = self.state.receipt['pipeline_stages']
        self.assertGreater(stages[0]['model_stderr_bytes'], 0)
        self.assertEqual(stages[2]['model_stderr_bytes'], 0)


class EvidenceContractTests(unittest.TestCase):
    def test_minimal_summary_keeps_quotes_and_only_meaningful_priority(self):
        decisions = {'priority': None, 'topic': None, 'financial': False, 'promotional': False}
        prepared = {'facts': [{'id': 'f1', 'source': 'body', 'quote': 'No aprobar pago.'}],
                    'financial_data': None, 'body_truncated': False}
        value = {'sentences': [{'fact_id': 'f1', 'text': 'No aprobar pago.'}], 'decisions': decisions}
        self.assertEqual(validate_rendering(value, prepared, decisions)['summary'], '«No aprobar pago.»')
        for priority in ('alta', 'media'):
            value['decisions'] = {**decisions, 'priority': priority}
            self.assertEqual(validate_rendering(value, prepared, value['decisions'])['summary'],
                             '«No aprobar pago.»\nPrioridad: ' + priority)

    def test_sender_is_never_a_summary_and_body_wins_over_subject(self):
        decisions = {'priority': None, 'topic': None, 'financial': False, 'promotional': False}
        prepared = {'facts': [{'id': 'f1', 'source': 'sender', 'quote': 'Empresa'},
                              {'id': 'f2', 'source': 'subject', 'quote': 'Aviso'},
                              {'id': 'f3', 'source': 'body', 'quote': 'No aprobar pago.'}],
                    'financial_data': None, 'body_truncated': False}
        for fact in prepared['facts'][:2]:
            with self.assertRaises(ValueError):
                validate_rendering({'sentences': [{'fact_id': fact['id'], 'text': fact['quote']}],
                                    'decisions': decisions}, prepared, decisions)
        prepared['facts'] = prepared['facts'][1:2]
        self.assertEqual(validate_rendering({'sentences': [{'fact_id': 'f2', 'text': 'Aviso'}],
                                            'decisions': decisions}, prepared, decisions)['summary'], '«Aviso»')

    def test_extra_fields_duplicate_ids_missing_source_and_actions_rejected(self):
        source = {'sender': 'Persona', 'subject': 'Proyecto', 'body': 'No aprobar pago.'}
        base = {'facts': [{'id': 'f1', 'source': 'body', 'quote': source['body']}],
                'financial_data': None, 'uncertainties': []}
        for edit in (lambda x: x.update(priority='alta'),
                     lambda x: x['facts'].append(x['facts'][0]),
                     lambda x: x['facts'][0].update(source='to'),
                     lambda x: x['facts'][0].update(quote='Pago autorizado.'),
                     lambda x: x.update(uncertainties=['ambiguous'])):
            value = copy.deepcopy(base)
            edit(value)
            with self.assertRaises(ValueError): validate_preparation(value, source, False)

    def test_financial_data_requires_evidence_also_in_prepared_facts(self):
        fields = {k: None for k in ('account', 'amount', 'currency', 'date', 'category')}
        fields['amount'] = {'value': '123.45', 'evidence': 'Pago 123.45'}
        with self.assertRaises(ValueError):
            validate_preparation({'facts': [{'id': 'f1', 'source': 'subject', 'quote': 'Proyecto'}],
                                  'financial_data': fields, 'uncertainties': []},
                                 {'subject': 'Proyecto', 'body': 'Pago 123.45'}, False)

    def test_urls_removed_before_preparation_including_headers(self):
        source = source_for({'sender': 'Ver https://example.invalid/SECRET1',
                             'subject': 'Ver https://example.invalid/SECRET2'},
                            {'body': 'Ver https://example.invalid/SECRET3'})
        self.assertNotIn('SECRET', json.dumps(source))

    def test_unknown_fact_and_free_form_output_rejected(self):
        decisions = {'priority': None, 'topic': None, 'financial': False, 'promotional': False}
        prepared = {'facts': [{'id': 'f1', 'source': 'body', 'quote': 'Proyecto.'}],
                    'financial_data': None, 'body_truncated': False}
        for value in ({'summary': 'Inventado', 'decisions': decisions},
                      {'sentences': [{'fact_id': 'f2', 'text': 'Proyecto.'}], 'decisions': decisions},
                      {'sentences': [{'fact_id': 'f1', 'text': 'Proyecto.'}],
                       'decisions': {**decisions, 'financial': 0}}):
            with self.assertRaises(ValueError): validate_rendering(value, prepared, decisions)
