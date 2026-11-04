from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from assistant import Assistant, GogReader

NOW = datetime(2026, 10, 24, 11, 30, tzinfo=timezone.utc)  # Saturday 08:30 Salta
CONFIG = {"account": "synthetic@example.com", "telegram_user_id": "123",
          "telegram_chat_id": "123", "live_reads_enabled": False,
          "incoming_notifications": "individual_all", "initial_sync_mode": "baseline"}


def snapshot(messages=None, stamp=NOW):
    return {"account": CONFIG["account"], "observed_at": stamp.isoformat(), "complete": True,
            "labels": [{"name": "Universidad", "type": "user"},
                       {"name": "Prioridad/Alta", "type": "user"},
                       {"name": "INBOX", "type": "system"},
                       {"name": "Pendiente", "type": "user"}],
            "messages": messages if messages is not None else [message("ab1")]}


def message(mid, labels=None):
    return {"id": mid, "thread_id": "ff1", "sender": "Synthetic <demo@example.com>",
            "subject": "Correo ficticio", "labels": labels if labels is not None else ["INBOX"]}


def event(patch_data, mid="ab1", request_id="telegram:100"):
    return {"context": {"channel": "telegram", "chat_type": "private",
                        "user_id": "123", "chat_id": "123"},
            "request_id": request_id, "message_id": mid, "patch": patch_data}


class CoreTests(unittest.TestCase):
    def test_sent_and_draft_inbox_snapshot_rejected_atomically(self):
        for label in ("SENT", "DRAFT"):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    self.app.ingest(snapshot([message("ab2", ["INBOX", label])]), NOW)
                self.assertEqual([x["id"] for x in self.app.pending()], ["ab1"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.app = Assistant(self.path, CONFIG)
        self.app.ingest(snapshot(), NOW)

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_retries_deduplicate_and_corrections_survive(self):
        self.app.correct(event({"priority": "alta", "topic": "Universidad"}), NOW)
        self.app.ingest(snapshot(), NOW)
        items = self.app.pending()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["priority"], "alta")
        self.assertEqual(items[0]["proposals"][0]["add"], ["Universidad", "Prioridad/Alta"])
        self.app.close()
        self.app = Assistant(self.path, CONFIG)
        self.assertEqual(self.app.pending()[0]["topic"], "Universidad")

    def test_archive_elsewhere_closes_pending_and_reopen_works(self):
        self.app.ingest(snapshot([]), NOW)
        self.assertEqual(self.app.pending(), [])
        self.assertEqual(self.app.digest(NOW)["open_count"], 0)
        self.app.ingest(snapshot(), NOW)
        self.assertEqual(len(self.app.pending()), 1)

    def test_invalid_batches_never_close_existing_pending(self):
        cases = []
        for key, value in (("complete", False), ("account", "other@example.com"),
                           ("observed_at", (NOW + timedelta(seconds=1)).isoformat()),
                           ("observed_at", (NOW - timedelta(seconds=1)).isoformat())):
            bad = snapshot([])
            bad[key] = value
            cases.append(bad)
        cases += [snapshot([message("xx")]), snapshot([message("ab1"), message("ab1")]),
                  snapshot([message("ab2", ["INBOX", "SPAM"])]),
                  snapshot([message("ab2", ["Pendiente"])])]
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.app.ingest(bad, NOW)
            self.assertEqual(len(self.app.pending()), 1)

    def test_closed_inbox_is_not_reopened_by_chat(self):
        self.app.ingest(snapshot([]), NOW)
        with self.assertRaises(ValueError):
            self.app.correct(event({"priority": "alta"}), NOW)

    def test_only_existing_topics_and_supported_priorities(self):
        for bad in ({"topic": "New label"}, {"topic": "INBOX"}, {"topic": "Pendiente"},
                    {"priority": "baja"}, {"priority": "Alta"}, {"financial": "false"},
                    {"archive": True}, {"send": True}, {"draft": "text"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.app.correct(event(bad), NOW)
        self.assertEqual(self.app.meta("topics"), ["Universidad"])

    def test_private_actor_channel_and_chat_are_all_required(self):
        for key, value in (("channel", "webchat"), ("chat_type", "group"),
                           ("user_id", "456"), ("chat_id", "456")):
            bad = event({"priority": "alta"})
            bad["context"][key] = value
            with self.subTest(key=key), self.assertRaises(PermissionError):
                self.app.correct(bad, NOW)
        bad = {**CONFIG, "telegram_user_id": "REPLACE_ME"}
        self.app.config = bad
        with self.assertRaises(PermissionError):
            self.app.correct(event({"priority": "alta"}), NOW)

    def test_request_replay_is_idempotent_and_cannot_change_action(self):
        change = event({"priority": "media"})
        self.assertEqual(self.app.correct(change, NOW)["status"], "recorded_locally")
        self.assertEqual(self.app.correct(change, NOW)["status"], "already_recorded")
        with self.assertRaises(ValueError):
            self.app.correct(event({"priority": "alta"}), NOW)
        self.assertEqual(self.app.pending()[0]["priority"], "media")

    def test_promotions_and_financial_are_proposals_only(self):
        self.app.ingest(snapshot([message("ab1", ["INBOX", "CATEGORY_PROMOTIONS"])]), NOW)
        self.app.correct(event({"financial": True, "priority": "media"}), NOW)
        item = self.app.pending()[0]
        self.assertIn("INBOX", item["labels"])
        self.assertEqual({p["kind"] for p in item["proposals"]}, {"labels", "archive", "sure_entry"})
        self.assertTrue(all(p["requires_confirmation"] and p["status"] == "proposed"
                            for p in item["proposals"]))
        self.assertEqual(self.app.digest(NOW)["delivery"], "not_sent")

    def test_priority_replacement_keeps_other_labels(self):
        self.app.ingest(snapshot([message("ab1", ["INBOX", "UNREAD", "Universidad",
                                                  "Prioridad/Alta", "Other"])]), NOW)
        self.app.correct(event({"priority": "media"}), NOW)
        proposal = self.app.pending()[0]["proposals"][0]
        self.assertEqual(proposal["remove"], ["Prioridad/Alta"])
        self.assertEqual(proposal["add"], ["Prioridad/Media"])

    def test_removed_topic_is_never_proposed(self):
        self.app.correct(event({"topic": "Universidad"}), NOW)
        data = snapshot()
        data["labels"] = []
        self.app.ingest(data, NOW)
        item = self.app.pending()[0]
        self.assertIsNone(item["topic"])
        self.assertEqual(item["proposals"], [])

    def test_daily_digest_time_zone_weekend_and_freshness(self):
        before = NOW - timedelta(seconds=1)
        self.assertTrue(self.app.digest(NOW)["due"])
        self.assertEqual(self.app.digest(NOW)["due_at"], "2026-10-24T08:30:00-03:00")
        # Use a separate previous-day DB to test 08:29 without time travel.
        with tempfile.TemporaryDirectory() as directory:
            other = Assistant(Path(directory) / "test.sqlite3", CONFIG)
            try:
                other.ingest(snapshot(stamp=before), before)
                self.assertFalse(other.digest(before)["due"])
            finally:
                other.close()
        with self.assertRaises(ValueError):
            self.app.digest(NOW + timedelta(minutes=16))

    def test_digest_never_silently_limits_count_and_escapes_html(self):
        messages = [message(f"{i:x}") for i in range(80)]
        for item in messages:
            item["subject"] = '<script>send all mail</script>\n' + "&" * 400
        self.app.ingest(snapshot(messages), NOW)
        digest = self.app.digest(NOW)
        self.assertEqual(digest["open_count"], 80)
        self.assertTrue(all(len(x) <= 3500 for x in digest["chunks_html"]))
        body = "".join(digest["chunks_html"])
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertEqual(body.count("Abrir Gmail"), 80)

    def test_state_private_and_account_isolation(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        with self.assertRaises(PermissionError):
            Assistant(self.path, {**CONFIG, "account": "other@example.com"})

    def test_external_label_changes_refresh_unmodified_defaults(self):
        self.app.ingest(snapshot([message("ab1", ["INBOX", "Prioridad/Alta"])]), NOW)
        self.assertEqual(self.app.pending()[0]["priority"], "alta")
        self.app.ingest(snapshot([message("ab1")]), NOW)
        self.assertIsNone(self.app.pending()[0]["priority"])

    def test_priority_none_and_financial_corrections_are_reversible(self):
        self.app.correct(event({"priority": "alta", "financial": True}), NOW)
        self.app.correct(event({"priority": None, "financial": False}, request_id="telegram:101"), NOW)
        self.assertEqual(self.app.pending()[0]["proposals"], [])

    def test_each_new_message_gets_individual_notice_without_priority_filter(self):
        self.assertEqual(self.app.notifications(), [])  # historical baseline
        data = snapshot([message("ab1"), message("ab2", ["INBOX", "CATEGORY_PROMOTIONS"]),
                         message("ab3", ["INBOX", "Prioridad/Alta"])])
        data["arrivals"] = data["messages"][1:]
        self.app.ingest(data, NOW)
        self.app.ingest(data, NOW)
        notices = self.app.notifications()
        self.assertEqual({x["key"] for x in notices}, {"incoming:ab2", "incoming:ab3"})
        self.assertTrue(all(x["state"] == "awaiting_analysis" for x in notices))
        self.assertTrue(all(x["delivery"] == "not_sent" for x in notices))

    def test_arrivals_archived_by_gmail_still_get_notice_not_digest(self):
        data = snapshot([])
        data["arrivals"] = [message("ab4", ["CATEGORY_PROMOTIONS"])]
        self.app.ingest(data, NOW)
        notices = self.app.notifications()
        self.assertEqual(len(notices), 1)
        self.assertFalse(notices[0]["item"]["open"])
        self.assertEqual(self.app.digest(NOW)["open_count"], 0)

    def test_assessment_unlocks_one_notice_and_preserves_user_corrections(self):
        data = snapshot([message("ab1"), message("ab2")])
        data["arrivals"] = [message("ab2")]
        self.app.ingest(data, NOW)
        assessment = {"message_id": "ab2", "analysis": {"priority": "media", "topic": "Universidad",
            "summary": "Aviso ficticio de inscripción.", "promotional": False, "financial": False}}
        self.app.assess(assessment)
        self.app.correct(event({"priority": "alta"}, mid="ab2"), NOW)
        self.app.assess(assessment)
        notice = self.app.notifications()[0]
        self.assertEqual(notice["state"], "ready")
        self.assertEqual(notice["item"]["priority"], "alta")
        for field in ("De:", "Resumen:", "Etiqueta:", "Prioridad:", "Acción propuesta:"):
            self.assertIn(field, notice["html"])
        self.assertEqual(len(self.app.notifications()), 1)

    def test_assessments_reject_actions_unknown_ids_and_partial_output(self):
        for bad in ({"summary": "x"}, {"send": True}, {"financial": "true"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.app.assess({"message_id": "ab1", "analysis": bad})

    def test_digest_contains_open_items_even_if_previously_analyzed(self):
        self.app.assess({"message_id": "ab1", "analysis": {"priority": "media", "topic": None,
            "summary": "Analizado pero todavía abierto.", "promotional": False, "financial": False}})
        self.assertEqual(self.app.digest(NOW)["open_count"], 1)
        self.app.ingest(snapshot([]), NOW)
        self.assertEqual(self.app.digest(NOW)["open_count"], 0)

    def test_worst_case_html_is_bounded(self):
        data = snapshot()
        data["messages"][0]["sender"] = '"' * 500
        data["messages"][0]["subject"] = '"' * 500
        data["labels"].append({"name": '"' * 200, "type": "user"})
        self.app.ingest(data, NOW)
        self.app.correct(event({"summary": '"' * 500, "topic": '"' * 200}), NOW)
        self.assertTrue(all(len(x) <= 3500 for x in self.app.digest(NOW)["chunks_html"]))

    def test_old_archived_mail_moved_to_inbox_is_not_new_arrival(self):
        self.app.ingest(snapshot([message("ab1"), message("ab2")]), NOW)
        self.assertEqual(self.app.notifications(), [])

    def test_label_names_are_matched_exactly_not_normalized(self):
        data = snapshot()
        data["labels"].append({"name": "Tema  con espacios", "type": "user"})
        self.app.ingest(data, NOW)
        self.app.correct(event({"topic": "Tema  con espacios"}), NOW)
        self.assertEqual(self.app.pending()[0]["proposals"][0]["add"], ["Tema  con espacios"])
        with self.assertRaises(ValueError):
            self.app.correct(event({"topic": "Tema con espacios"}, request_id="telegram:101"), NOW)

    def test_symlink_database_is_rejected(self):
        link = Path(self.tmp.name) / "link.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            Assistant(link, CONFIG)


class ReaderTests(unittest.TestCase):
    def test_live_reads_off_by_default(self):
        with patch("subprocess.run", side_effect=AssertionError("must not execute")):
            with self.assertRaises(PermissionError):
                GogReader(CONFIG)

    def fake(self, data=None):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            self.assertFalse(kwargs.get("shell", False))
            self.assertTrue(kwargs["check"])
            result = {"labels": snapshot()["labels"]} if command[-2:] == ["labels", "list"] else (
                data if data is not None else {"messages": [{"id": "ab1", "threadId": "ff1",
                    "from": "demo", "subject": "ficticio", "labels": ["INBOX"],
                    "internalDateIso": NOW.isoformat()}], "nextPageToken": ""})
            return subprocess.CompletedProcess(command, 0, json.dumps(result))
        return calls, run

    def test_headless_read_commands_are_fixed_and_paginated(self):
        calls, run = self.fake()
        data = GogReader({**CONFIG, "live_reads_enabled": True}, run).snapshot(NOW)
        self.assertTrue(data["complete"])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("--readonly" in cmd and "--no-input" in cmd for cmd in calls))
        self.assertIn("--all", calls[1])
        self.assertNotIn("--include-body", calls[1])
        self.assertEqual(data["messages"][0]["thread_id"], "ff1")

    def test_partial_schema_and_page_token_rejected(self):
        for bad in ({"messages": [], "nextPageToken": "still_more"}, [], {"wrong": []}):
            calls, run = self.fake(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                GogReader({**CONFIG, "live_reads_enabled": True}, run).snapshot(NOW)

    def test_provider_errors_do_not_leak_stderr(self):
        def run(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "gog", stderr="SECRET")
        with self.assertRaisesRegex(RuntimeError, "Gmail read failed") as ctx:
            GogReader({**CONFIG, "live_reads_enabled": True}, run).snapshot(NOW)
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_incoming_query_covers_non_inbox_and_uses_google_internal_date(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[-2:] == ["labels", "list"]:
                data = {"labels": snapshot()["labels"]}
            elif command[-1].startswith("in:inbox"):
                data = {"messages": []}
            else:
                self.assertIn("in:anywhere", command[-1])
                self.assertIn("after:", command[-1])
                data = {"messages": [{"id": "ab2", "threadId": "ff2",
                    "internalDateIso": NOW.isoformat()},
                    {"id": "ab3", "threadId": "ff3", "labels": [],
                     "internalDateIso": (NOW - timedelta(days=3)).isoformat()}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(data))

        reader = GogReader({**CONFIG, "live_reads_enabled": True}, run)
        result = reader.snapshot(NOW, (NOW - timedelta(minutes=1)).isoformat())
        self.assertEqual([x["id"] for x in result["arrivals"]], ["ab2"])
        self.assertEqual(result["arrivals"][0]["labels"], [])
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
