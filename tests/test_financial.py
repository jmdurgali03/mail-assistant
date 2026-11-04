"""Synthetic boundary tests; no Gmail, model, Telegram or Sure connections."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from assistant import Assistant, FINANCIAL_FIELDS, validate_financial_data, AUTH_SUMMARY
from pipeline import OpenClawAnalyzer, ContentReader, Outbox, cycle
from test_assistant import CONFIG, NOW, snapshot, message, event


def facts():
    return {"account": {"value": "Personal", "evidence": "Cuenta: Personal"},
            "amount": {"value": "1234.56", "evidence": "Total: ARS 1.234,56"},
            "currency": {"value": "ARS", "evidence": "Total: ARS 1.234,56"},
            "date": {"value": "2026-10-23", "evidence": "Fecha de compra: 2026-10-23"},
            "category": {"value": "Transporte", "evidence": "Categoría: Transporte"}}


BODY = "Cuenta: Personal. Total: ARS 1.234,56. Fecha de compra: 2026-10-23. Categoría: Transporte."


def assessment(data=None):
    return {"priority": "media", "topic": None, "summary": "Compra sintética; revisar propuesta.",
            "promotional": False, "financial": True,
            "financial_data": facts() if data is None else data}


class FinancialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Assistant(self.root / "state.db", {**CONFIG, "telegram_account_id": "bichi"})
        self.app.ingest(snapshot([]), NOW)
        incoming = snapshot([message("ab1")])
        incoming["arrivals"] = incoming["messages"]
        self.app.ingest(incoming, NOW)

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def analyzer(self, output):
        config = {"tools": {"deny": ["*"]}, "agents": {"defaults": {"model": {"primary": "fixture"}}}}
        path = self.root / "model.json"
        path.write_text(json.dumps(config))
        def fake(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, json.dumps(
                {"ok": True, "status": "ok", "final": json.dumps(output)}))
        return OpenClawAnalyzer({"analysis_enabled": True, "analysis_config": str(path),
                                 "analysis_model": "fixture"}, fake)

    def test_complete_receipt_reaches_reviewable_outbox_and_survives_restart(self):
        analysis = self.analyzer(assessment()).analyze(message("ab1"), {"body": BODY}, [])
        self.app.assess({"message_id": "ab1", "analysis": analysis})
        proposal = next(p for p in self.app.pending()[0]["proposals"] if p["kind"] == "sure_entry")
        self.assertEqual(proposal["data"]["amount"], "1234.56")
        self.assertEqual(proposal["missing"], [])
        self.assertFalse(proposal["ready_for_execution"])
        self.assertTrue(proposal["requires_confirmation"])
        payload = next(r["payload"] for r in Outbox(self.app).prepare(NOW) if r["kind"] == "incoming")
        for value in ("1234.56", "ARS", "Personal", "Transporte", "2026-10-23", "sin ejecutor"):
            self.assertIn(value, payload)
        self.app.close()
        self.app = Assistant(self.root / "state.db", {**CONFIG, "telegram_account_id": "bichi"})
        self.assertEqual(self.app.pending()[0]["financial_data"], facts())

    def test_partial_receipt_has_only_real_missing_fields(self):
        data = facts()
        for key in ("account", "date", "category"):
            data[key] = None
        result = self.analyzer(assessment(data)).analyze(message("ab1"), {"body": "Total: ARS 1.234,56"}, [])
        self.app.assess({"message_id": "ab1", "analysis": result})
        proposal = self.app.pending()[0]["proposals"][-1]
        self.assertEqual(proposal["missing"], ["account", "date", "category"])
        self.assertIsNone(proposal["data"]["account"])

    def test_ambiguous_or_attachment_only_data_stays_explicitly_missing(self):
        output = assessment(dict.fromkeys(FINANCIAL_FIELDS))
        output["summary"] = "Varios totales, símbolo $ o comprobante adjunto; revisar sin elegir movimiento."
        for body in ("Total $ 100; saldo $ 200; vencimiento 10/11/2026", "Comprobante en adjunto"):
            result = self.analyzer(output).analyze(message("ab1"), {"body": body}, [])
            self.assertTrue(all(v is None for v in result["financial_data"].values()))

    def test_invented_quote_or_action_output_rejected_before_persistence(self):
        for output in (assessment(), {**assessment(), "execute": True}):
            with self.assertRaises(RuntimeError):
                self.analyzer(output).analyze(message("ab1"), {"body": "No receipt facts here."}, [])
        self.assertIsNone(self.app.db.execute("SELECT value FROM assessments").fetchone())

    def test_value_cannot_disagree_with_quote(self):
        for key, value in (("amount", "999.00"), ("currency", "USD"),
                           ("date", "2026-10-24"), ("account", "Other"), ("category", "Other")):
            data = facts()
            data[key]["value"] = value
            with self.subTest(field=key), self.assertRaises(ValueError):
                validate_financial_data(data, BODY)

    def test_malformed_amount_currency_date_and_schema_rejected(self):
        for key, value in (("amount", True), ("amount", 1234.56), ("amount", "NaN"),
                           ("amount", "-1"), ("amount", "0"), ("amount", "1e3"),
                           ("amount", "1234567890123"), ("currency", "$"), ("currency", "ZZZ"),
                           ("date", "2026-02-30"), ("date", "10/11/2026")):
            data = facts()
            data[key] = {"value": value, "evidence": str(value)}
            with self.subTest(field=key, value=value), self.assertRaises(ValueError):
                validate_financial_data(data)
        for data in ({}, {**facts(), "send": True}, {**facts(), "amount": {"value": "1"}},
                     {**facts(), "category": {"value": "x", "evidence": "x" * 161}}):
            with self.assertRaises(ValueError):
                validate_financial_data(data)

    def test_explicit_decimal_formats_and_ambiguous_separator(self):
        for token in ("1.234,56", "1,234.56", "1234.56", "1234,56", "1.234,56."):
            data = dict.fromkeys(FINANCIAL_FIELDS)
            data["amount"] = {"value": "1234.56", "evidence": "Total " + token}
            self.assertEqual(validate_financial_data(data)["amount"]["value"], "1234.56")
        for token in ("1.234", "1,234"):
            data["amount"] = {"value": "1234", "evidence": token}
            with self.assertRaises(ValueError):
                validate_financial_data(data)

    def test_unambiguous_local_date_normalizes_but_ambiguous_date_does_not(self):
        data = facts()
        data["date"] = {"value": "2026-10-23", "evidence": "Compra 23/10/2026"}
        self.assertEqual(validate_financial_data(data)["date"]["value"], "2026-10-23")
        data["date"] = {"value": "2026-11-10", "evidence": "Compra 10/11/2026"}
        with self.assertRaises(ValueError):
            validate_financial_data(data)

    def test_private_correction_replaces_financial_facts_and_refreshes_notice(self):
        self.app.assess({"message_id": "ab1", "analysis": assessment()})
        box = Outbox(self.app)
        box.prepare(NOW)
        updated = facts()
        updated["category"] = {"value": "Movilidad", "evidence": "Corrección privada: Movilidad"}
        self.app.correct(event({"financial_data": updated}), NOW)
        payload = next(r["payload"] for r in box.prepare(NOW) if r["kind"] == "incoming")
        self.assertIn("Movilidad", payload)
        self.app.correct(event({"financial": False}, request_id="telegram:999"), NOW)
        self.assertFalse(any(p["kind"] == "sure_entry" for p in self.app.pending()[0]["proposals"]))

    def test_financial_text_is_escaped_not_executed(self):
        data = facts()
        data["category"] = {"value": "<script>x</script>", "evidence": "<script>x</script>"}
        self.app.assess({"message_id": "ab1", "analysis": assessment(data)})
        self.assertNotIn("<script>", self.app.render_item(self.app.pending()[0]))

    def test_auth_subject_and_body_never_reach_model_db_or_outbox(self):
        self.app.assess({"message_id": "ab1", "analysis": assessment()})
        for index, (subject, body) in enumerate((
                ("Tu código de acceso: 854921", "Acceso a tu cuenta"),
                ("Aviso de cuenta", "Recovery codes: SYNTHETIC-RECOVERY-SECRET"),
                ("API key: SYNTHETIC-KEY-SECRET", "Nueva clave"),
                ("Tus códigos de respaldo", "SYNTHETIC-RECOVERY-SECRET"),
                ("Aviso", "Verification code: 854921"))):
            with self.subTest(index=index):
                mid = "ac" + str(index)
                incoming = snapshot([message(mid)])
                incoming["messages"][0]["subject"] = subject
                incoming["arrivals"] = deepcopy(incoming["messages"])
                class Reader:
                    def snapshot(self, now, since):
                        return incoming
                    def content(self, mid):
                        return {"body": body}
                class ForbiddenAnalyzer:
                    def analyze(self, *args):
                        raise AssertionError("auth mail reached model")
                cycle(self.app, Reader(), ForbiddenAnalyzer(), NOW)
                item = next(x for x in self.app.pending() if x["id"] == mid)
                self.assertEqual(item["summary"], AUTH_SUMMARY)
                payload = next(r["payload"] for r in Outbox(self.app).rows() if r["key"] == "incoming:" + mid)
                self.assertIn("revisar en Gmail", payload)
                dump = " ".join(self.app.db.iterdump())
                for secret in ("854921", "SYNTHETIC-RECOVERY-SECRET", "SYNTHETIC-KEY-SECRET"):
                    self.assertNotIn(secret, dump)
                    self.assertNotIn(secret.encode(), (self.root / "state.db").read_bytes())

    def test_content_reader_and_direct_analyzer_also_gate_auth(self):
        body = "Password reset. Recovery code: SYNTHETIC-SECRET"
        def fake(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"message": {"id": "ab1", "body": body}}))
        content = ContentReader({**CONFIG, "live_reads_enabled": True}, fake).content("ab1")
        self.assertEqual(content["body"], "")
        self.assertTrue(content["authentication"])
        analyzer = self.analyzer({})
        analyzer.run = lambda *a, **kw: self.fail("must not invoke model")
        self.assertEqual(analyzer.analyze(message("ab1"), {"body": body}, [])["summary"], AUTH_SUMMARY)
        sender_item = {**message("ab1"), "sender": "API key: SYNTHETIC-KEY-SECRET"}
        self.assertEqual(analyzer.analyze(sender_item, {"body": "Aviso"}, [])["summary"], AUTH_SUMMARY)


if __name__ == "__main__":
    unittest.main()
