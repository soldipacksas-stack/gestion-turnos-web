# Gestión de turnos — prototipo web V0.1

## Ejecutar localmente
Python 3.11 o 3.12. `pip install -r requirements.txt`.
En PowerShell: `$env:ADMIN_PASSWORD='clave-administrador'`; `$env:OPERATOR_PASSWORD='clave-operadores'`; `$env:SESSION_SECRET='una-clave-larga-aleatoria'`. Ejecutar `uvicorn main:app --reload` y abrir http://localhost:8000. Sin DATABASE_URL usa SQLite local para pruebas.

## Render
Crear PostgreSQL y un Web Service desde el repositorio. Build: `pip install -r requirements.txt`. Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`. Configurar DATABASE_URL (URL interna PostgreSQL), ADMIN_PASSWORD, OPERATOR_PASSWORD, SESSION_SECRET. No subir claves ni bases locales.

## Alcance y limitaciones
Prototipo, no producción. Incluye acceso por rol, persistencia, generación CP-SAT, edición de borrador, publicación, historial, novedades y vistas iniciales. Pendientes: equidad anual optimizada, sugerencias concretas de reemplazo, traslados e inactivaciones con vigencia, auditoría individual, edición de novedades, resaltado de cambios, alertas y gráfica semanal, festivos, configuración versionada, validación completa de bordes de periodo y seguridad de producción. El optimizador tiene límite de búsqueda; no garantiza óptimo ni reproducibilidad entre versiones de dependencias. No se ha ejecutado el motor en este entorno. No usar para publicar turnos reales hasta completar pruebas.

Los 12 operadores iniciales son ficticios. Registrar nombres reales únicamente desde la administración de la aplicación protegida.
