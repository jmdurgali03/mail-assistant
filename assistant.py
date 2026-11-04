"""Offline-first mail planning. No external write or delivery capability."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from urllib.parse import quote

SALTA = ZoneInfo("America/Argentina/Salta")
PRIORITIES = {"alta": "Prioridad/Alta", "media": "Prioridad/Media", None: None}
ID = re.compile(r"[a-fA-F0-9]{1,64}\Z")
FIELDS = {"priority", "topic", "summary", "promotional", "financial"}
FINANCIAL_FIELDS = ("account", "amount", "currency", "date", "category")
AUTH_SUMMARY = "Código de acceso o credenciales recibidos; revisar en Gmail."
AUTH_SUBJECT = "Correo de autenticación; revisar en Gmail"
AUTH_PATTERN = re.compile(
    r"\b(?:otp|2fa|mfa|totp|password|passcode|contrase\w*|credencial\w*|credentials?|"
    r"verification|verificaci[oó]n|authentication|autenticaci[oó]n|recovery|recuperaci[oó]n|"
    r"codes?|c[oó]digos?|claves?|pin|magic[ -]?link|token[=:]|"
    r"one[ -]time|security code|access code|login code|sign[ -]?in|"
    r"c[oó]digo.{0,40}(?:acceso|seguridad|inicio|sesi[oó]n)|"
    r"(?:code|c[oó]digo|pin)\s*[:=\-]?\s*[0-9]{4,10}|"
    r"(?:api|secret)[ _-]?key|bearer|access[ _-]?token|refresh[ _-]?token)\b", re.I)


def authentication_content(value):
    # Conservative lexical gate, before persistence/model input. Never log matches.
    return bool(AUTH_PATTERN.search(value) or re.fullmatch(r"\s*[0-9]{4,10}\s*", value))


def authentication_analysis():
    return {"priority": None, "topic": None, "summary": AUTH_SUMMARY,
            "promotional": False, "financial": False, "financial_data": None}


def validate_financial_data(data, source=None):
    """Untrusted extraction, not Sure API data. Retain only bounded quoted facts."""
    if data is None:
        return None
    if not isinstance(data, dict) or set(data) != set(FINANCIAL_FIELDS):
        raise ValueError("financial_data requires all fields, missing values as null")
    result = {}
    for field in FINANCIAL_FIELDS:
        fact = data[field]
        if fact is None:
            result[field] = None
            continue
        if not isinstance(fact, dict) or set(fact) != {"value", "evidence"}:
            raise ValueError("financial fact requires value and evidence")
        value, evidence = fact["value"], fact["evidence"]
        if (not isinstance(value, str) or not value or value != text(value, 80)
                or not isinstance(evidence, str) or not evidence
                or evidence != text(evidence, 160)):
            raise ValueError("invalid financial fact")
        if source is not None and evidence not in source:
            raise ValueError("financial evidence absent from supplied content")
        if field == "amount":
            # Positive decimal string, never float/exponent/negative or silent rounding.
            if not re.fullmatch(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,2})?", value) or Decimal(value) <= 0:
                raise ValueError("invalid amount")
            # Normalize only explicit decimal notation; 1,234 / 1.234 are ambiguous.
            tokens = re.findall(r"(?<![\w.,])[0-9]+(?:[.,][0-9]+)*(?!\w|[.,][0-9])", evidence)
            matched = False
            for token in tokens:
                if re.fullmatch(r"[0-9]+", token):
                    normalized = token
                elif re.fullmatch(r"[0-9]+[.,][0-9]{1,2}", token):
                    normalized = token.replace(",", ".")
                elif re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{3})+,[0-9]{1,2}", token):
                    normalized = token.replace(".", "").replace(",", ".")
                elif re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})+\.[0-9]{1,2}", token):
                    normalized = token.replace(",", "")
                else:
                    continue
                matched |= Decimal(normalized) == Decimal(value)
            if not matched:
                raise ValueError("amount not supported by unambiguous evidence")
        elif field == "currency":
            # No inference from '$', sender location or account; no external FX lookup.
            if (value not in {"ARS", "USD", "EUR", "BRL", "UYU", "CLP", "GBP", "CAD", "AUD",
                              "JPY", "MXN", "COP", "PEN", "CHF", "CNY", "BOB", "PYG"}
                    or not re.search(r"(?<!\w)" + value + r"(?!\w)", evidence)):
                raise ValueError("explicit currency code required")
        elif field == "date":
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError("ISO transaction date required")
            stamp = datetime.strptime(value, "%Y-%m-%d")
            supported = value in evidence
            # Day > 12 rules out MM/DD ambiguity. Never substitute a mail timestamp.
            if stamp.day > 12:
                supported |= bool(re.search(r"(?<![0-9])" + stamp.strftime("%d/%m/%Y") + r"(?![0-9])", evidence))
            if not supported:
                raise ValueError("unambiguous transaction date evidence required")
        elif value not in evidence:
            raise ValueError("account/category must be literal references")
        result[field] = {"value": value, "evidence": evidence}
    return result


def iso(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return stamp.astimezone(timezone.utc)


def text(value, limit=500):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid text field")
    # Do not let sender-controlled line breaks impersonate digest entries.
    return " ".join(value.split())


def configured(config):
    if not re.fullmatch(r"[A-Za-z0-9._+\-]{1,64}@[A-Za-z0-9.\-]{1,189}", config.get("account", "")):
        raise ValueError("explicit Gmail account required")
    if config.get("incoming_notifications") != "individual_all":
        raise ValueError("incoming policy must be individual_all")
    if config.get("initial_sync_mode") != "baseline":
        raise ValueError("initial sync must establish a historical baseline")


class GogReader:
    """Fixed read-only Gmail commands. Never accept shell or query from mail/chat."""

    def __init__(self, config, run=subprocess.run):
        configured(config)
        if config.get("live_reads_enabled") is not True:
            raise PermissionError("live Gmail reads are disabled")
        self.config, self.run = config, run

    def _read(self, args):
        command = ["gog", "--account", self.config["account"], "--readonly",
                   "--no-input", "--json", "gmail", *args]
        try:
            result = self.run(command, capture_output=True, text=True, timeout=120,
                              check=True, stdin=subprocess.DEVNULL)
        except (subprocess.SubprocessError, OSError):
            # Do not surface gog stderr: it can contain account or message content.
            raise RuntimeError("Gmail read failed; no state changed") from None
        return json.loads(result.stdout)

    def snapshot(self, now, since=None):
        labels = self._read(["labels", "list"])
        data = self._read(["messages", "search", "--all", "--max", "100",
                           "--timezone", "UTC", "--sanitize-content",
                           "in:inbox -in:spam -in:trash -in:sent -in:drafts"])
        if not isinstance(data, dict) or "messages" not in data or data.get("nextPageToken"):
            raise ValueError("incomplete or incompatible gog response")
        if not isinstance(labels, dict) or "labels" not in labels:
            raise ValueError("incompatible labels response")
        def normalize(item):
            return {"id": item["id"], "thread_id": item["threadId"],
                    "sender": item.get("from", ""), "subject": item.get("subject", ""),
                    "labels": item.get("labels", [])}
        messages = [normalize(item) for item in data["messages"]]
        boundary = iso(since) if since else now
        query = f"in:anywhere after:{int(boundary.timestamp()) - 120} -in:sent -in:drafts -in:spam -in:trash"
        incoming = self._read(["messages", "search", "--all", "--max", "100",
                               "--timezone", "UTC", "--sanitize-content", query])
        if not isinstance(incoming, dict) or "messages" not in incoming or incoming.get("nextPageToken"):
            raise ValueError("incomplete incoming-mail response")
        # Gmail internalDate, not the sender-controlled Date header. Overlap absorbs
        # short indexing delays; message IDs deduplicate the overlap atomically.
        arrivals = [normalize(item) for item in incoming["messages"]
                    if boundary - timedelta(seconds=120) <= iso(item["internalDateIso"]) <= now]
        # Taken at read start, not finish: a slow read cannot appear fresher than it is.
        return {"account": self.config["account"], "complete": True,
                "observed_at": now.isoformat(), "labels": labels["labels"],
                "messages": messages, "arrivals": arrivals}


class Assistant:
    def __init__(self, path, config):
        configured(config)
        self.config = config
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Refuse symlinks; create with private mode before SQLite opens the file.
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mail (
                id TEXT PRIMARY KEY, facts TEXT NOT NULL, analysis TEXT NOT NULL,
                open INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS corrections (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                message_id TEXT NOT NULL, patch TEXT NOT NULL, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS assessments (
                message_id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS notices (
                message_id TEXT PRIMARY KEY, state TEXT NOT NULL);
        """)
        account = self.meta("account")
        if account and account != config["account"]:
            self.db.close()
            raise PermissionError("database belongs to another account")
        with self.db:
            self.set_meta("account", config["account"])

    def close(self):
        self.db.close()

    def meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def ingest(self, snapshot, now):
        if snapshot.get("account") != self.config["account"] or snapshot.get("complete") is not True:
            raise ValueError("complete account-bound Inbox snapshot required")
        observed = iso(snapshot["observed_at"])
        previous = self.meta("observed_at")
        if observed > now or (previous and observed < iso(previous)):
            raise ValueError("future or out-of-order snapshot")
        labels = snapshot["labels"]
        if not isinstance(labels, list):
            raise ValueError("label catalog must be a list")
        topics = []
        for label in labels:
            name = label["name"]
            if not isinstance(name, str) or not name or len(name) > 200:
                raise ValueError("invalid label name")
            if label["type"] not in {"system", "user"}:
                raise ValueError("invalid label type")
            if (label["type"] == "user" and not name.startswith("Prioridad/")
                    and name not in {"Prioridad", "Pendiente"}):
                topics.append(name)
        messages, arrivals = snapshot["messages"], snapshot.get("arrivals", [])
        if not isinstance(messages, list) or not isinstance(arrivals, list):
            raise ValueError("messages must be a list")
        validated, inbox_ids = {}, set()
        arrival_ids = set()
        for is_inbox, batch in ((False, arrivals), (True, messages)):
            seen = set()
            for item in batch:
                mid, tid = item["id"], item["thread_id"]
                if not ID.fullmatch(mid) or not ID.fullmatch(tid) or mid in seen:
                    raise ValueError("invalid or duplicate Gmail message ID")
                seen.add(mid)
                ls = item["labels"]
                if not isinstance(ls, list) or any(not isinstance(x, str) for x in ls):
                    raise ValueError("invalid message labels")
                if is_inbox:
                    if "INBOX" not in ls or {"SPAM", "TRASH", "SENT", "DRAFT"}.intersection(ls):
                        raise ValueError("snapshot must contain only open Inbox messages")
                    inbox_ids.add(mid)
                elif {"SENT", "DRAFT"}.intersection(ls):
                    raise ValueError("arrivals cannot contain sent messages or drafts")
                if not is_inbox and not {"SPAM", "TRASH"}.intersection(ls):
                    arrival_ids.add(mid)
                sensitive = authentication_content(item["subject"] + " " + item["sender"])
                sensitive |= authentication_content(item["subject"])
                validated[mid] = {"id": mid, "thread_id": tid,
                                  "sender": "Remitente de autenticación" if sensitive else text(item["sender"]),
                                  "subject": AUTH_SUBJECT if sensitive else text(item["subject"]),
                                  "authentication": sensitive, "labels": ls}
        # Validate the whole batch before closing missing IDs or changing metadata.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.meta("observed_at")
            if previous and observed < iso(previous):
                raise ValueError("out-of-order snapshot")
            self.db.execute("UPDATE mail SET open=0")
            for facts in validated.values():
                known = self.db.execute("SELECT 1 FROM mail WHERE id=?", (facts["id"],)).fetchone()
                if previous is not None and not known and facts["id"] in arrival_ids:
                    self.db.execute("INSERT INTO notices VALUES (?, 'awaiting_analysis')", (facts["id"],))
                self.db.execute("""INSERT INTO mail VALUES (?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET facts=excluded.facts, open=excluded.open""",
                    (facts["id"], json.dumps(facts), "{}", int(facts["id"] in inbox_ids)))
            self.set_meta("topics", sorted(set(topics)))
            self.set_meta("observed_at", observed.isoformat())
        return {"open": len(inbox_ids), "external_writes": 0}

    def validate_patch(self, patch):
        if not isinstance(patch, dict) or not patch or set(patch) - (FIELDS | {"financial_data"}):
            raise ValueError("only analysis fields are allowed; no actions")
        if "priority" in patch and patch["priority"] not in PRIORITIES:
            raise ValueError("priority must be alta, media or null")
        if "topic" in patch and patch["topic"] is not None and patch["topic"] not in (self.meta("topics") or []):
            raise ValueError("topic must already exist in Gmail")
        if "summary" in patch:
            patch = {**patch, "summary": text(patch["summary"], 500)}
            if not patch["summary"]:
                raise ValueError("summary cannot be empty")
        for key in ("promotional", "financial"):
            if key in patch and type(patch[key]) is not bool:
                raise ValueError("flags must be boolean")
        if "financial_data" in patch:
            patch = {**patch, "financial_data": validate_financial_data(patch["financial_data"])}
        return patch

    def assess(self, event):
        """Trusted LOCAL intelligence input, not a chat/HTTP endpoint; plans only."""
        analysis = self.validate_patch(event["analysis"])
        if set(analysis) - {"financial_data"} != FIELDS:
            raise ValueError("assessment must explicitly provide all analysis fields")
        if not analysis["financial"] and analysis.get("financial_data") is not None:
            raise ValueError("nonfinancial assessment cannot carry financial data")
        mid = event["message_id"]
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not self.db.execute("SELECT 1 FROM mail WHERE id=?", (mid,)).fetchone():
                raise ValueError("unknown message")
            self.db.execute("INSERT OR REPLACE INTO assessments VALUES (?,?)", (mid, json.dumps(analysis)))
            self.db.execute("UPDATE notices SET state='ready' WHERE message_id=?", (mid,))
        return {"status": "assessed_locally", "external_writes": 0}

    def correct(self, event, now):
        # event.context must be supplied by the trusted OpenClaw router, NOT copied
        # from user-authored text or from model output/email headers.
        ctx = event["context"]
        user, chat = self.config.get("telegram_user_id"), self.config.get("telegram_chat_id")
        if (not isinstance(user, str) or not user.isdigit() or not isinstance(chat, str)
                or not chat.isdigit() or ctx != {"channel": "telegram", "chat_type": "private",
                                                "user_id": user, "chat_id": chat}):
            raise PermissionError("trusted private Telegram actor required")
        request_id = text(event["request_id"], 120)
        if not request_id:
            raise ValueError("request ID required")
        patch = self.validate_patch(event["patch"])
        mid = event["message_id"]
        fingerprint = hashlib.sha256(json.dumps([mid, patch], sort_keys=True).encode()).hexdigest()
        with self.db:
            # Serialize read/modify/write across local callers; no lost corrections.
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute("SELECT fingerprint FROM corrections WHERE request_id=?",
                                       (request_id,)).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise ValueError("request ID reused with different content")
                return {"status": "already_recorded", "external_writes": 0}
            row = self.db.execute("SELECT analysis FROM mail WHERE id=? AND open=1", (mid,)).fetchone()
            if not row:
                raise ValueError("message is not an open pending item")
            analysis = {**json.loads(row[0]), **patch}
            self.db.execute("UPDATE mail SET analysis=? WHERE id=?", (json.dumps(analysis), mid))
            self.db.execute("INSERT INTO corrections VALUES (?,?,?,?,?)",
                            (request_id, fingerprint, mid, json.dumps(patch), now.isoformat()))
        return {"status": "recorded_locally", "external_writes": 0}

    def items(self, open_only=True):
        items = []
        topics = self.meta("topics") or []
        query = "SELECT * FROM mail" + (" WHERE open=1" if open_only else "") + " ORDER BY id"
        for row in self.db.execute(query):
            facts, overrides = json.loads(row["facts"]), json.loads(row["analysis"])
            assessment = self.db.execute("SELECT value FROM assessments WHERE message_id=?", (row["id"],)).fetchone()
            assessed = json.loads(assessment[0]) if assessment else {}
            ls = facts["labels"]
            analysis = {"priority": ("alta" if "Prioridad/Alta" in ls else
                                     "media" if "Prioridad/Media" in ls else None),
                        "topic": next((x for x in ls if x in topics), None),
                        "summary": "Sin análisis; revisar en Gmail.",
                        "promotional": "CATEGORY_PROMOTIONS" in ls,
                        "financial": False, **assessed, **overrides}
            topic = analysis["topic"] if analysis["topic"] in topics else None
            add = [x for x in (topic, PRIORITIES[analysis["priority"]]) if x and x not in facts["labels"]]
            # Replace managed priorities only; preserve unrelated user/system labels.
            remove = [x for x in facts["labels"]
                      if x in {"Prioridad/Alta", "Prioridad/Media", "Prioridad/Baja"}
                      and x != PRIORITIES[analysis["priority"]]]
            proposals = []
            if add or remove:
                proposals.append({"kind": "labels", "add": add, "remove": remove,
                                  "status": "proposed", "requires_confirmation": True})
            if analysis["promotional"]:
                proposals.append({"kind": "archive", "status": "proposed", "requires_confirmation": True})
            if analysis["financial"]:
                extracted = validate_financial_data(analysis.get("financial_data"))
                values = {key: extracted[key]["value"] if extracted and extracted[key] else None
                          for key in FINANCIAL_FIELDS}
                proposals.append({"kind": "sure_entry", "status": "proposed",
                                  "requires_confirmation": True,
                                  "data": values, "evidence": extracted,
                                  "source_message_id": facts["id"],
                                  "missing": [key for key, value in values.items() if value is None],
                                  "ready_for_execution": False,
                                  "note": "Propuesta no verificada: validar comprobante, tipo de movimiento, "
                                          "moneda, cuenta/categoría en Sure y duplicados. Sin ejecutor."})
            items.append({**facts, **analysis, "topic": topic, "proposals": proposals,
                          "open": bool(row["open"]),
                          "analysis_status": ("corrected_locally" if overrides else
                                              "assessed_locally" if assessed else "needs_review"),
                          "gmail_url": f"https://mail.google.com/mail/u/{quote(self.config['account'], safe='')}/#all/{facts['thread_id']}"})
        return sorted(items, key=lambda item: {"alta": 0, "media": 1, None: 2}[item["priority"]])

    def pending(self):
        return self.items(open_only=True)

    @staticmethod
    def render_item(item):
        priority = item["priority"] or "sin asignar"
        names = {"labels": "Actualizar etiquetas", "archive": "Proponer archivo",
                 "sure_entry": "Proponer carga en Sure"}
        proposals = "; ".join(names[p["kind"]] + " (requiere confirmación)"
                              for p in item["proposals"]) or "Revisar correo"
        financial = ""
        for proposal in item["proposals"]:
            if proposal["kind"] == "sure_entry":
                labels = {"account": "Cuenta", "amount": "Importe", "currency": "Moneda",
                          "date": "Fecha", "category": "Categoría"}
                details = "; ".join(labels[key] + ": " + html.escape(value or "faltante")
                                    for key, value in proposal["data"].items())
                financial = f"Sure (propuesta no verificada): {details}. Requiere revisión y confirmación; sin ejecutor.\n"
        return (f"<b>{html.escape(item['subject'][:80])}</b>\n"
                f"De: {html.escape(item['sender'][:80])}\n"
                f"Resumen: {html.escape(item['summary'][:180])}\n"
                f"Etiqueta: {html.escape((item['topic'] or 'sin asignar')[:60])} · Prioridad: {priority}\n"
                f"Acción propuesta: {proposals}\n"
                f"{financial}"
                f'<a href="{html.escape(item["gmail_url"], quote=True)}">Abrir Gmail</a>')

    def notifications(self):
        states = {row["message_id"]: row["state"] for row in self.db.execute("SELECT * FROM notices")}
        return [{"key": f"incoming:{item['id']}", "message_id": item["id"],
                 "state": states[item["id"]], "delivery": "not_sent",
                 "html": self.render_item(item), "item": item}
                for item in self.items(open_only=False) if item["id"] in states
                and not {"SPAM", "TRASH", "SENT", "DRAFT"}.intersection(item["labels"])]

    def digest(self, now):
        observed = self.meta("observed_at")
        if not observed or not timedelta(0) <= now - iso(observed) <= timedelta(minutes=15):
            raise ValueError("refresh complete Inbox snapshot before preparing digest")
        local = now.astimezone(SALTA)
        due_at = local.replace(hour=8, minute=30, second=0, microsecond=0)
        blocks = [f"<b>Pendientes abiertos · {local:%d/%m/%Y}</b>"]
        items = self.pending()
        for item in items:
            blocks.append(self.render_item(item))
        if not items:
            blocks.append("No hay pendientes abiertos.")
        chunks, current = [], ""
        for block in blocks:
            if len(current) + len(block) + 2 > 3500:
                chunks.append(current)
                current = ""
            current += ("\n\n" if current else "") + block
        if current:
            chunks.append(current)
        return {"key": f"digest:{local.date()}", "due": local >= due_at,
                "due_at": due_at.isoformat(), "timezone": "America/Argentina/Salta",
                "open_count": len(items), "chunks_html": chunks,
                "delivery": "not_sent", "incoming_notifications": "individual_all"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("command", choices=["ingest", "assess", "correct", "pending", "digest", "notifications", "sync"])
    args = parser.parse_args()
    os.umask(0o077)
    app = None
    try:
        config = json.loads(Path(args.config).read_text())
        app = Assistant(args.db, config)
        now = datetime.now(timezone.utc)
        if args.command == "ingest":
            result = app.ingest(json.load(sys.stdin), now)
        elif args.command == "sync":
            result = app.ingest(GogReader(config).snapshot(now, app.meta("observed_at")), datetime.now(timezone.utc))
        elif args.command == "correct":
            result = app.correct(json.load(sys.stdin), now)
        elif args.command == "assess":
            result = app.assess(json.load(sys.stdin))
        elif args.command == "notifications":
            result = {"notifications": app.notifications(), "delivery": "not_sent"}
        elif args.command == "digest":
            result = app.digest(now)
        else:
            result = {"items": app.pending(), "observed_at": app.meta("observed_at"),
                      "delivery": "not_sent"}
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, KeyError, TypeError, PermissionError, OSError, RuntimeError, sqlite3.Error):
        print(json.dumps({"error": "Operation rejected or failed; inspect configuration/schema. No external writes."}), file=sys.stderr)
        return 1
    finally:
        if app:
            app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
