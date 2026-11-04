# Notification actions

Optional OpenClaw plugin for Telegram `bm2` callbacks: reviewed, snooze and return.
It updates only local state and notification presentation; it does not modify Gmail.

Requires runtime `registerInteractiveHandler` and absolute paths for
`assistantRoot`, `assistantConfig` and `database` as described in
`openclaw.plugin.json`. Set `assistantRoot` to the checkout's `src/` directory.
Private configuration must enable `notification_actions_enabled: true`, select a
Telegram account and use the same private ID for `telegram_chat_id` and
`telegram_user_id`. Keep the plugin disabled until identity and compatibility
have been verified.

Run from the repository root:

```bash
node --test plugins/actions-plugin/test_plugin.mjs
```

The synthetic test needs neither a runtime nor a connection. This plugin does not
start another Telegram consumer.
