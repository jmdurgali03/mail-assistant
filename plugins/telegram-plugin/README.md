# Local corrections through Telegram

Optional OpenClaw plugin using `api.on("before_dispatch", handler)`.
Requires the Python core, an existing private database and absolute paths in
`config.disabled.example.json`. Set `assistantRoot` to the checkout's `src/`
directory. The example is disabled; check your runtime API before activation.
It does not start a Telegram consumer.

Exact command: `/mail_correct ab1 {"priority":"alta"}`. Replace `ab1` with an
existing Gmail ID. `alta` is an existing serialized priority value, not an English
UI label. Identity is never inferred from text, subjects or replies. Channel,
account, sender and private chat must match authorized configuration and runtime
metadata. A ledger prevents replay. Corrections affect only local analysis;
they do not execute Gmail or financial operations.

Plugin gates and `correction_bridge_enabled` must be explicitly enabled.
Do not replace existing OpenClaw configuration; merge only reviewed entries.

Run the synthetic test from the repository root:

```bash
node --test plugins/telegram-plugin/test_plugin.mjs
```
