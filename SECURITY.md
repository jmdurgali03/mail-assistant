# Security

This project is experimental. It is not guaranteed to be free of vulnerabilities.

## Private data

Keep OAuth credentials, keyring passwords, Telegram/provider tokens, personal
accounts and paths, SQLite databases, live configuration, receipts, sessions and
email bodies outside this repository. `.gitignore` is an additional defense,
not a secret scanner or a security guarantee.

Content needed to analyze ordinary email is sent to configured providers. Review
their policies and use a test account before connecting personal email.
Authentication-content filters are conservative but cannot detect every secret.
Local state can contain email metadata, excerpts and summaries; keep it private.

Do not expose OAuth portals directly to the Internet without reviewing origin,
transport, access and session controls. Do not start a second consumer using the
same Telegram token. Review systemd examples before installing them.

## Reporting a vulnerability

If GitHub offers **Report a vulnerability** in the Security tab, use it for a
private report. Otherwise open an issue without exploitable details or private
data, requesting a coordination channel. Never publish real tokens or emails
as proof.
