"""Trusted local IPC for the inactive OpenClaw correction hook. No network."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

from assistant import Assistant, ID

COMMAND = re.compile(r"/mail_correct ([a-fA-F0-9]{1,64}) ([\s\S]+)\Z")
NUMBER = re.compile(r"[1-9][0-9]{0,29}\Z")
ACCOUNT = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def closed_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result
    def invalid(_):
        raise ValueError("non-JSON number")
    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


def authorize(config, envelope):
    """Envelope is IPC from a native hook, not user-authored JSON or an HTTP API."""
    account, user, chat = (config.get(key) for key in
                           ("telegram_account_id", "telegram_user_id", "telegram_chat_id"))
    if (config.get("correction_bridge_enabled") is not True
            or not isinstance(account, str) or not ACCOUNT.fullmatch(account)
            or "albert" in account.lower()
            or any(not isinstance(x, str) or not NUMBER.fullmatch(x) for x in (user, chat))
            or user != chat):
        raise PermissionError("explicit private non-Albert bridge configuration required")
    if not isinstance(envelope, dict) or set(envelope) != {"event", "context"}:
        raise ValueError("invalid hook envelope")
    event, ctx = envelope["event"], envelope["context"]
    if (not isinstance(event, dict) or set(event) != {"channel", "isGroup", "senderId", "messageId", "content"}
            or not isinstance(ctx, dict) or set(ctx) != {"channelId", "accountId", "conversationId", "senderId", "messageId"}):
        raise ValueError("incomplete hook identity")
    mid = ctx["messageId"]
    if (event["channel"] != "telegram" or ctx["channelId"] != "telegram"
            or event["isGroup"] is not False or ctx["accountId"] != account
            or ctx["conversationId"] != chat or ctx["senderId"] != user or event["senderId"] != user
            or not isinstance(mid, str) or not NUMBER.fullmatch(mid) or event["messageId"] != mid):
        raise PermissionError("runtime identity mismatch")
    command = event["content"]
    if not isinstance(command, str) or len(command) > 2048:
        raise ValueError("bounded explicit command required")
    match = COMMAND.fullmatch(command)
    if not match or not ID.fullmatch(match[1]):
        raise ValueError("explicit command required")
    patch = closed_json(match[2])
    # Telegram message identity, NOT an update ID. Stable across process restarts,
    # account/chat scoped, deliberately separate from legacy telegram:<updateId>.
    identity = json.dumps(["telegram", account, chat, mid], separators=(",", ":"))
    request_id = "telegram-message:v1:" + hashlib.sha256(identity.encode()).hexdigest()
    return {"context": {"channel": "telegram", "chat_type": "private", "user_id": user, "chat_id": chat},
            "request_id": request_id, "message_id": match[1], "patch": patch}


def apply(config_path, db_path, envelope):
    config = closed_json(Path(config_path).read_text())
    correction = authorize(config, envelope)  # Before opening/creating any DB.
    path = Path(db_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("existing private assistant database required")
    app = Assistant(path, config)
    try:
        return app.correct(correction, datetime.now(timezone.utc))
    finally:
        app.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(8193)
        if len(raw) > 8192:
            raise ValueError("oversized IPC")
        result = apply(args.config, args.db, closed_json(raw))
        print(json.dumps(result))
        return 0
    except (ValueError, KeyError, TypeError, PermissionError, OSError, sqlite3.Error):
        # Never echo command, data, config, account, paths or exception details.
        print('{"error":"correction_rejected"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
