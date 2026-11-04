# Live integrations: requirements and limitations

Start with the reproducible demo and tests. This guide describes the published
code's requirements; it does not certify a live installation.

## Gmail / gogcli

- The adapter expects gog at `~/.local/bin/gog` and a separate private keyring.
- Root `gog-provenance.json` records the reference Linux amd64 binary version and
  checksum; the repository does not download it.
- Consent requests Gmail readonly and validates identity. Archiving, labeling,
  sending email and writing to financial services are not implemented.
- `src/inbox_readonly.py` pins `GOG_SHA256` and `CONFIG_SHA256`. The latter refers
  to the reviewed original analysis configuration, **not** the examples. A new
  installation must review its configuration and explicitly update contracts and
  tests; do not remove validation just to force execution.
- Example paths such as `/home/REPLACE_USER` and `/private/state` are placeholders.
  This release does not provide a complete OAuth setup wizard.

## OpenClaw and models

The code pins profile `bichi-mail-analysis`, analysis agent `bichi-mail-analyzer`,
model `openai/gpt-6.1-sol` and Jev `typesafe/jev-1.13.0`. These inherited contracts
require availability and validation in your runtime. Free model access and
cross-version compatibility are not promised. Analysis examples disable all tools.

`src/jev_gateway.mjs` uses the OpenClaw Gateway SDK installed under
`~/.npm-global/lib/node_modules/openclaw` and a session of agent `main`.
The TypeSafe provider configuration references Secret Store entry
`TYPESAFE_API_KEY`; the key is not included. Review the SDK, agent policies and
provider configuration before connecting data. Original runtime-specific telemetry
patches are not distributed; missing required evidence must block execution.

## Telegram and continuous worker

Configure the private route explicitly: `telegram_chat_id` must equal the positive
numeric `telegram_user_id`. The continuous worker uses account `default`; the core
supports an explicit account. No real user's ID is included. The route is bound to
state so subsequent changes are rejected.

The worker additionally requires `watch_contract: new-inbox-gpt-jev-gpt-v1`,
`analysis_batch_limit: 3`, activation flags and a fresh `watch.sqlite3` database.
The core example keeps flags disabled and cannot activate the worker without review.
The `baseline` mode does not scan historical mail.

Plugins reuse OpenClaw's existing Telegram consumer. Check `before_dispatch` and
`registerInteractiveHandler` availability in your runtime. Examples stay inactive
until explicitly configured. Snooze buttons schedule a notice only when the user
requests it and an active worker exists. Set `assistantRoot` to the absolute
checkout's `src/` path.

## Optional systemd setup

`install-job.sh` copies unit files and refuses to overwrite existing units;
**it does not reload, enable or start services**. Examples assume a checkout at
`~/projects/mail-assistant`; adjust paths if needed. They require private
`~/.config/bichi-mail/watch.json`, `worker.env` and a credential outside the repo.
Review `src/watch_setup.py --activate` before running it: activation can start
real analysis and delivery.

## Excluded from publication

Private state, credentials, personal accounts, delivery ledgers, operational logs,
runtime copies and local instrumentation patches are not included. Publishing or
reorganizing this copy does not modify the original personal installation.
