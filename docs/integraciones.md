# Integraciones reales: límites y configuración

La demo y las pruebas son el punto de entrada reproducible. Esta guía documenta
los requisitos del código publicado; no certifica una instalación en vivo.

## Gmail / gogcli

- El adaptador espera gog en `~/.local/bin/gog` y un almacén privado separado.
- `gog-provenance.json` registra la versión y el checksum del binario Linux amd64
  utilizado como referencia, no una dependencia descargada por este repo.
- El consentimiento pide Gmail readonly y valida la identidad. No se implementan
  archivado, cambio de etiquetas, envío de correo ni escritura a servicios financieros.
- `inbox_readonly.py` fija `GOG_SHA256` y `CONFIG_SHA256`. Este último corresponde a
  la configuración de análisis revisada en la implementación original, **no** a los
  archivos de ejemplo. Una instalación nueva debe revisar su propia configuración y
  actualizar explícitamente los contratos y pruebas; no eliminar estas validaciones
  para forzar una ejecución.
- Las rutas `/home/REPLACE_USER` y `/private/state` de ejemplos son placeholders.
  No hay un asistente completo de instalación OAuth en esta primera publicación.

## OpenClaw y modelos

El código fija el perfil `bichi-mail-analysis`, el agente analizador
`bichi-mail-analyzer`, el modelo `openai/gpt-6.1-sol` y Jev `typesafe/jev-1.13.0`.
Son contratos heredados que requieren disponibilidad y validación en tu runtime.
No se promete acceso gratuito a los modelos ni compatibilidad entre versiones.
Los ejemplos de análisis deshabilitan todas las herramientas del modelo.

`jev_gateway.mjs` usa el SDK Gateway de OpenClaw instalado bajo
`~/.npm-global/lib/node_modules/openclaw` y una sesión del agente `main`.
La configuración del proveedor TypeSafe usa una referencia al Secret Store
`TYPESAFE_API_KEY`; este repositorio no contiene esa clave. Revisá el SDK,
las políticas del agente y la configuración del proveedor antes de conectar datos.
Los parches de telemetría específicos del runtime original no se distribuyen;
si faltan las evidencias exigidas, el programa debe bloquearse.

## Telegram y worker

La ruta privada se configura explícitamente: `telegram_chat_id` debe ser el mismo
ID numérico positivo que `telegram_user_id`. El worker continuo conserva la cuenta
`default`, mientras el núcleo admite una cuenta explícita. No se incluye el ID de
ningún usuario real. La ruta se liga al estado para rechazar cambios posteriores.

El worker continuo requiere además `watch_contract: new-inbox-gpt-jev-gpt-v1`,
`analysis_batch_limit: 3`, flags de activación y una base nueva `watch.sqlite3`.
La configuración de ejemplo del núcleo mantiene los flags desactivados y no sirve
para activar el worker sin revisión. El modo `baseline` no recorre el histórico.

Los plugins usan el consumidor existente de Telegram en OpenClaw. Verificá las APIs
`before_dispatch` y `registerInteractiveHandler` en tu versión. Los ejemplos quedan
inactivos hasta configurarlos explícitamente. Los botones de posposición programan
avisos solo cuando el usuario realiza esa acción y existe un worker activo.

## systemd opcional

`install-job.sh` copia archivos y rechaza sobrescribir unidades existentes;
**no recarga, habilita ni inicia servicios**. Los ejemplos asumen un checkout en
`~/projects/mail-assistant`: ajustalos si elegiste otra ubicación. Requieren
`~/.config/bichi-mail/watch.json`, `worker.env` y una credencial privada fuera del repo.
No ejecutes `watch_setup.py --activate` sin revisar su almacenamiento y efectos:
la activación sí puede iniciar análisis y entrega reales.

## Qué no se publicó

No se copiaron estados, credenciales, cuentas personales, ledgers de entrega,
bitácoras de operación, copias del runtime, ni parches de instrumentación locales.
El código instalado originalmente no se modifica al publicar esta copia.
