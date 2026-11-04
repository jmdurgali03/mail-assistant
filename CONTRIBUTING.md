# Contributing to Mail Assistant

Use issues for bugs and proposals, and pull requests for code changes.

1. Fork the repository and work on your own branch.
2. Keep changes focused and describe the expected behavior.
3. Add tests for changes to contracts, permissions, state or deduplication.
4. Run `bash scripts/check.sh` from the repository root.
5. Document new requirements and limitations in the README.

## Code layout

- `src/`: Python modules and the JavaScript Jev gateway.
- `tests/`: Python tests; synthetic fixtures live in `tests/fixtures/`.
- `plugins/`: independent OpenClaw plugins and their Node.js tests.
- `examples/`: sanitized configuration templates.
- `systemd/` and `scripts/`: optional deployment units and development checks.

For an individual Python test, use:

```bash
PYTHONPATH=src:tests python3 -m unittest test_assistant -v
```

## Contracts to preserve

- Explicit configuration and validation of account, private chat and sender.
- Fail closed on ambiguous results or insufficient telemetry.
- Never automatically retry uncertain delivery.
- Do not grant tools to the analysis model or execute email instructions.
- Keep proposed actions distinct from executed operations.
- Tests must not require real credentials or messages.

Include the problem, change, test evidence and limitations in your PR. Always use
synthetic data. Do not publish inbox screenshots, personal IDs, credentials or
private logs. Read [SECURITY.md](SECURITY.md) for vulnerability reporting.
