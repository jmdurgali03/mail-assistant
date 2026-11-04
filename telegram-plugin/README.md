# Correcciones locales por Telegram

Plugin opcional de OpenClaw usando `api.on("before_dispatch", handler)`.
Requiere el núcleo Python, una base privada existente y rutas absolutas en
`config.disabled.example.json`. El ejemplo está deshabilitado; revisá la API
contra tu runtime antes de activarlo. No inicia un consumidor Telegram.

Comando exacto: `/mail_correct ab1 {"priority":"alta"}`. Reemplazá `ab1` por
el ID Gmail existente. No se infiere identidad desde texto, asuntos o replies.
El canal, la cuenta, el remitente y el chat privado deben coincidir con la
configuración y los metadatos autorizados del runtime. El ledger evita replay.
Solo se corrige el análisis local; no se ejecutan acciones de Gmail ni finanzas.

Los gates del plugin y `correction_bridge_enabled` deben habilitarse
explícitamente. No reemplaces la configuración existente de OpenClaw: combiná
solo las entradas necesarias tras revisarlas.

Prueba sintética: `node --test telegram-plugin/test_plugin.mjs` desde la raíz.
