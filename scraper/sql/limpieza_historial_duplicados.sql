-- Limpieza retroactiva de filas duplicadas en historial_precios, acumuladas ANTES del fix de
-- código en ofertas_writer.py (Regla 3 ya no inserta filas nuevas cuando precio/descuento no
-- cambiaron — ver commit que agrega DecisionPublicacion.puede_reusar_fila).
--
-- No se ejecuta automáticamente: correr manualmente desde la consola SQL de Railway, cuando
-- quieras, idealmente:
--   1. Después de deployar el fix de código (para no limpiar mientras se siguen generando
--      duplicados nuevos).
--   2. Después de un backup (Railway → Postgres → Backups, o `pg_dump "$DATABASE_URL"
--      -t historial_precios > backup_historial.sql`).
--
-- Criterio de seguridad (igual al del fix de código): para cada producto_id, colapsa corridas
-- consecutivas de filas con el mismo (precio, descuento_pct) en una sola fila, conservando SIEMPRE:
--   - la fila más reciente de cada corrida (preserva la fecha usada por el timer de Regla 3), y
--   - la fila de origen del precio mínimo histórico de ese producto (preserva la fecha real del
--     récord, mostrada en el caption como "Precio mínimo anterior: $X el DD/MM/YYYY").
-- Nunca borra un valor distinto — solo duplicados exactos del mismo (precio, descuento_pct)
-- consecutivos en el tiempo. Es idempotente: correrlo dos veces no borra nada la segunda vez.


-- ============================================================================================
-- PASO 1 — dry run (solo lectura, seguro de correr en cualquier momento): cuenta cuántas filas
-- se borrarían sin borrar nada todavía.
-- ============================================================================================
WITH ordenado AS (
    SELECT
        id, producto_id, precio, descuento_pct, fecha,
        LAG(precio) OVER w AS precio_prev,
        LAG(descuento_pct) OVER w AS descuento_prev
    FROM historial_precios
    WINDOW w AS (PARTITION BY producto_id ORDER BY fecha, id)
),
marcado AS (
    SELECT *,
        CASE WHEN precio_prev IS DISTINCT FROM precio
               OR descuento_prev IS DISTINCT FROM descuento_pct
             THEN 1 ELSE 0 END AS es_inicio_run
    FROM ordenado
),
runs AS (
    SELECT *, SUM(es_inicio_run) OVER (PARTITION BY producto_id ORDER BY fecha, id) AS run_id
    FROM marcado
),
origen_minimo AS (
    SELECT DISTINCT ON (producto_id) producto_id, id AS id_origen
    FROM historial_precios
    ORDER BY producto_id, precio ASC, fecha ASC, id ASC
),
a_conservar AS (
    SELECT DISTINCT ON (producto_id, run_id) id
    FROM runs
    ORDER BY producto_id, run_id, fecha DESC, id DESC
)
SELECT count(*) AS filas_a_borrar
FROM historial_precios hp
WHERE hp.id NOT IN (SELECT id FROM a_conservar)
  AND hp.id NOT IN (SELECT id_origen FROM origen_minimo);


-- ============================================================================================
-- PASO 2 — el DELETE real. Correr recién después de revisar el conteo del paso 1.
-- ============================================================================================
WITH ordenado AS (
    SELECT
        id, producto_id, precio, descuento_pct, fecha,
        LAG(precio) OVER w AS precio_prev,
        LAG(descuento_pct) OVER w AS descuento_prev
    FROM historial_precios
    WINDOW w AS (PARTITION BY producto_id ORDER BY fecha, id)
),
marcado AS (
    SELECT *,
        CASE WHEN precio_prev IS DISTINCT FROM precio
               OR descuento_prev IS DISTINCT FROM descuento_pct
             THEN 1 ELSE 0 END AS es_inicio_run
    FROM ordenado
),
runs AS (
    SELECT *, SUM(es_inicio_run) OVER (PARTITION BY producto_id ORDER BY fecha, id) AS run_id
    FROM marcado
),
origen_minimo AS (
    SELECT DISTINCT ON (producto_id) producto_id, id AS id_origen
    FROM historial_precios
    ORDER BY producto_id, precio ASC, fecha ASC, id ASC
),
a_conservar AS (
    SELECT DISTINCT ON (producto_id, run_id) id
    FROM runs
    ORDER BY producto_id, run_id, fecha DESC, id DESC
)
DELETE FROM historial_precios hp
WHERE hp.id NOT IN (SELECT id FROM a_conservar)
  AND hp.id NOT IN (SELECT id_origen FROM origen_minimo);


-- ============================================================================================
-- PASO 3 — reclamar el espacio en disco. Un DELETE por sí solo NO reduce el volumen facturado
-- por Railway (Postgres/MVCC no devuelve el espacio al SO al hacer DELETE). VACUUM FULL sí lo
-- hace, reescribiendo la tabla y sus índices. Toma un lock ACCESS EXCLUSIVE breve — a este
-- tamaño de tabla debería tomar bien menos de un segundo, pero conviene correrlo cuando el
-- scraper no esté a mitad de una corrida (cron horario).
-- ============================================================================================
VACUUM (FULL) historial_precios;
