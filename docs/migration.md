# Repository layout migration

The public checkout now separates application code, tests, plugins and examples.
No database schema or serialized data contracts were changed.

| Previous path | New path |
|---|---|
| `*.py` application modules | `src/*.py` |
| `jev_gateway.mjs` | `src/jev_gateway.mjs` |
| `test_*.py` | `tests/test_*.py` |
| `fixtures/` | `tests/fixtures/` |
| `actions-plugin/` | `plugins/actions-plugin/` |
| `telegram-plugin/` | `plugins/telegram-plugin/` |
| Root `*example.json` files | `examples/` |

Run commands from the repository root, for example `python3 src/demo.py`.
For module imports outside the check script, set `PYTHONPATH=src:tests`.

For existing live deployments, review these paths before updating:

- Set both plugins' `assistantRoot` to the absolute `src/` directory, not the
  repository root. Update plugin installation paths to `plugins/...`.
- Update custom launchers to invoke `src/mail_watch.py` or the appropriate script.
  The bundled systemd service already references the new path. The installer still
  refuses to overwrite existing units and never starts or enables services.
- Some proof/recovery tools derive private state directories from their script
  location. They will now look under `src/`. Review and deliberately migrate any
  existing ledgers before running them; do not accidentally create a fresh ledger
  and repeat an operation. One-shot plans must match the script-relative state path.
- Keep existing private configuration and databases outside Git. Do not blindly
  replace them with example files or weaken checksum/identity validation.

This change does not modify the separate original personal installation.
