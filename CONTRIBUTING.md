# Contribuir a Mail Assistant

Usá issues para errores o propuestas y pull requests para cambios de código.

1. Hacé un fork y trabajá en una rama propia.
2. Mantené el cambio acotado y explicá el comportamiento esperado.
3. Agregá pruebas cuando cambien contratos, permisos, estado o deduplicación.
4. Ejecutá `bash scripts/check.sh` desde la raíz.
5. Documentá requisitos y límites nuevos en el README.

## Contratos que deben preservarse

- Configuración explícita y validación de cuenta, chat privado y remitente.
- Fallar de forma cerrada ante resultados ambiguos o telemetría insuficiente.
- No repetir automáticamente una entrega incierta.
- No otorgar herramientas al modelo analizador ni ejecutar instrucciones del correo.
- No confundir acciones propuestas con operaciones ejecutadas.
- Ninguna prueba debe requerir credenciales o mensajes reales.

Incluí en el PR el problema, el cambio, la evidencia de pruebas y las limitaciones.
Usá siempre datos sintéticos. No publiques capturas de bandeja, IDs personales,
credenciales ni logs privados. Para vulnerabilidades, leé SECURITY.md.
