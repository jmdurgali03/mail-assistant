"""Synthetic end-to-end CLI smoke test; temporary state, zero integrations."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from test_assistant import CONFIG, event, message, snapshot


def main():
    with tempfile.TemporaryDirectory(prefix="bichi-mail-demo-") as directory:
        root = Path(directory)
        config = root / "config.json"
        config.write_text(json.dumps(CONFIG))
        command = [sys.executable, str(Path(__file__).with_name("assistant.py")),
                   "--config", str(config), "--db", str(root / "state.sqlite3")]

        def run(action, data=None):
            result = subprocess.run([*command, action], input=json.dumps(data),
                                    capture_output=True, text=True, check=True)
            return json.loads(result.stdout)

        now = datetime.now(timezone.utc)
        run("ingest", snapshot([], stamp=now))
        incoming = snapshot([message("ab2", ["INBOX", "CATEGORY_PROMOTIONS"])], stamp=now)
        incoming["arrivals"] = incoming["messages"]
        run("ingest", incoming)
        run("ingest", incoming)  # delivery retry, not a second incoming email
        run("assess", {"message_id": "ab2", "analysis": {
            "priority": "media", "topic": "Universidad", "summary": "Factura ficticia para revisar.",
            "promotional": True, "financial": True}})
        run("correct", event({"priority": "alta"}, mid="ab2"))
        notices = run("notifications")["notifications"]
        digest = run("digest")
        assert len(notices) == 1 and notices[0]["item"]["priority"] == "alta"
        assert notices[0]["state"] == "ready" and notices[0]["delivery"] == "not_sent"
        assert digest["open_count"] == 1
        assert {x["kind"] for x in notices[0]["item"]["proposals"]} == {"archive", "labels", "sure_entry"}
        run("ingest", snapshot([], stamp=now))
        assert run("digest")["open_count"] == 0
        print(json.dumps({"synthetic_only": True, "individual_notices": len(notices),
                          "digest_before_external_archive": 1, "digest_after_external_archive": 0,
                          "external_reads": 0, "external_writes": 0, "messages_sent": 0,
                          "notification_html": notices[0]["html"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
