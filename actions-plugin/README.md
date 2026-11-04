# Acciones de notificaciones

Plugin opcional de OpenClaw para los callbacks `bm2` de Telegram: revisado,
posponer y volver. Solo actualiza el estado local y la presentación del aviso;
no modifica Gmail.

Requiere `registerInteractiveHandler` en el runtime y rutas absolutas para
`assistantRoot`, `assistantConfig` y `database` según `openclaw.plugin.json`.
La configuración privada debe incluir `notification_actions_enabled: true`, una
cuenta Telegram y el mismo ID privado en `telegram_chat_id` y `telegram_user_id`.
Conservá desactivado el plugin hasta verificar identidad y compatibilidad.

Prueba sin runtime ni conexión: `node --test actions-plugin/test_plugin.mjs`
desde la raíz del repositorio. No inicia otro consumidor Telegram.
