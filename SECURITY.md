# Seguridad

Este proyecto es experimental. No se garantiza la ausencia de vulnerabilidades.

## Datos privados

Mantené fuera del repositorio las credenciales OAuth, contraseñas de almacenes,
tokens de Telegram/proveedores, cuentas y rutas personales, bases SQLite,
configuraciones reales, recibos, sesiones y cuerpos de correo. `.gitignore` es
una defensa adicional, no un escáner de secretos ni una garantía de seguridad.

El contenido necesario para el análisis de correos ordinarios se transmite a los
proveedores configurados. Revisá sus políticas y configurá una cuenta de prueba
antes de usar correo personal. Los filtros de autenticación son conservadores,
pero no garantizan detectar todos los secretos. El estado local puede contener
metadatos, extractos y resúmenes de correo; tratá la base de datos como privada.

No expongas los portales OAuth directamente a Internet sin revisar origen,
transporte, acceso y sesión. No actives un segundo consumidor del mismo token
Telegram. Revisá los ejemplos systemd antes de instalarlos.

## Reportar una vulnerabilidad

Si GitHub ofrece la opción **Report a vulnerability** en la pestaña Security,
usala para un reporte privado. Si no está disponible, abrí un issue sin detalles
explotables ni datos privados, solicitando un canal de coordinación. Nunca
publiques tokens o correos reales como demostración.
