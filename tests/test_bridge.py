"""Offline trusted-IPC authorization, canonical replay and persistence tests."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from assistant import Assistant
from bridge import apply, authorize, closed_json
from test_assistant import CONFIG, NOW, message, snapshot

CFG = {**CONFIG, "telegram_account_id": "bichi", "correction_bridge_enabled": True}


def envelope(content='/mail_correct ab1 {"priority":"alta"}', mid="500"):
    return {"event": {"channel": "telegram", "isGroup": False, "senderId": "123", "messageId": mid,
                       "content": content}, "context": {"channelId": "telegram", "accountId": "bichi",
                       "conversationId": "123", "senderId": "123", "messageId": mid}}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(CFG))
        self.db = self.root / "state.db"
        app = Assistant(self.db, CFG)
        app.ingest(snapshot([message("ab1")]), NOW)
        app.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_canonical_replay_across_reopen_and_conflict(self):
        self.assertEqual(apply(self.config, self.db, envelope())["status"], "recorded_locally")
        self.assertEqual(apply(self.config, self.db, envelope())["status"], "already_recorded")
        with self.assertRaises(ValueError):
            apply(self.config, self.db, envelope('/mail_correct ab1 {"priority":"media"}'))
        request = authorize(CFG, envelope())["request_id"]
        self.assertTrue(request.startswith("telegram-message:v1:"))
        self.assertNotEqual(request, "telegram:500")
        other = {**CFG, "telegram_account_id": "bichi-other"}
        data = envelope()
        data["context"]["accountId"] = "bichi-other"
        self.assertNotEqual(request, authorize(other, data)["request_id"])
        other_chat = {**CFG, "telegram_user_id": "456", "telegram_chat_id": "456"}
        data = envelope()
        data["context"].update(senderId="456", conversationId="456")
        data["event"]["senderId"] = "456"
        self.assertNotEqual(request, authorize(other_chat, data)["request_id"])

    def test_missing_conflicting_group_or_forged_identity_rejected(self):
        for block, field, value in (("event", "isGroup", True), ("event", "isGroup", None),
                ("event", "channel", "webchat"), ("event", "senderId", "456"),
                ("event", "messageId", "501"), ("context", "senderId", "456"),
                ("context", "accountId", "albert"), ("context", "conversationId", "-123"),
                ("context", "conversationId", "456"), ("context", "channelId", "webchat"),
                ("context", "messageId", ""), ("context", "messageId", 500)):
            data = envelope()
            data[block][field] = value
            with self.subTest(field=field, value=value), self.assertRaises((ValueError, PermissionError)):
                apply(self.config, self.db, data)
        for block in ("event", "context"):
            for field in envelope()[block]:
                data = envelope()
                del data[block][field]
                with self.subTest(block=block, field=field), self.assertRaises(ValueError):
                    apply(self.config, self.db, data)

    def test_disabled_or_albert_config_fails_before_database_creation(self):
        for config in ({**CFG, "correction_bridge_enabled": False}, {**CFG, "telegram_account_id": "Albert-prod"},
                       {**CFG, "telegram_chat_id": "456"}):
            self.config.write_text(json.dumps(config))
            absent = self.root / "absent.db"
            with self.assertRaises(PermissionError):
                apply(self.config, absent, envelope())
            self.assertFalse(absent.exists())

    def test_explicit_command_schema_and_actions(self):
        for text in ('subí la prioridad', '/mail_correct ab1 {"send":true}',
                     '/mail_correct ab1 {"approve":true}', '/mail_correct ab1 {"archive":true}',
                     '/mail_correct ab1 {"context":{"user_id":"123"}}',
                     '/mail_correct ab1 {"priority":"alta","priority":"media"}',
                     '/mail_correct ab1 {"priority":NaN}', '/mail_correct ab1 {}',
                     '/mail_correct ab1 {"topic":"inventada"}',
                     '/mail_correct ab1 {"priority":"alta"} trailing',
                     '/mail_correct@other ab1 {"priority":"alta"}', 'x' * 2049):
            with self.subTest(text=text[:80]), self.assertRaises((ValueError, TypeError)):
                apply(self.config, self.db, envelope(text))

    def test_cli_errors_are_generic_and_read_is_bounded(self):
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "src/bridge.py"),
                   "--config", str(self.config), "--db", str(self.db)]
        for raw in ('SYNTHETIC_PRIVATE_COMMAND', 'x' * 8193):
            result = subprocess.run(command, input=raw, text=True, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout), {"error": "correction_rejected"})
            self.assertEqual(result.stderr, "")

    def test_unknown_closed_mail_and_symlink_rejected(self):
        with self.assertRaises(ValueError):
            apply(self.config, self.db, envelope('/mail_correct aff {"priority":"alta"}'))
        link = self.root / "link.db"
        link.symlink_to(self.db)
        with self.assertRaises(ValueError):
            apply(self.config, link, envelope())
        app = Assistant(self.db, CFG)
        app.ingest(snapshot([]), NOW)
        app.close()
        with self.assertRaises(ValueError):
            apply(self.config, self.db, envelope())


if __name__ == "__main__":
    unittest.main()
