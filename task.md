# La Red - Fix publicacion Facebook (13 jun 2026)

## PROBLEMA
La Red dejo de publicar a Facebook desde ~12:33 UTC (6:33 AM GT).
Todas las ejecuciones de Make daban 1 op (no detectaba notas nuevas).

## CAUSA RAIZ
1. Vercel RSS (www.lared1061.com/feed) cayo con HTTP 500
2. Proxy cayo a Capa 1.5 (scraper Next.js)
3. Scraper ponia pubDate = "ahora" en TODAS las notas (linea 763 app.py)
4. Make rss:TriggerNewArticle usa pubDate para detectar novedad
   - maxResults=1, depende 100% de pubDate
   - con todas las fechas iguales no puede ordenar ni detectar nuevo -> 1 op

## FIX APLICADO (commit 587c3c0)
- pubDate decreciente por posicion: nota0=ahora, nota1=ahora-60s, etc.
- Mantiene orden cronologico correcto aunque Vercel este caido
- Deploy dep-d8mmcgvlk1mc738sb72g LIVE

## ESTADO ACTUAL
- Vercel RSS YA volvio (HTTP 200) -> sirve fechas REALES otra vez
- Fix es proteccion permanente para cuando Vercel vuelva a caer
- Ejecucion forzada 8145e1f9 = SUCCESS
- Pendiente: confirmar que publica cuando salga nota NUEVA

## NOTA
- Errores OAuthException de FB (12 jun 14:03-14:18) fueron caida temporal de Facebook, NO del sistema
- mia_brands.py tiene merge externo b8181c0 (links /articulo/) - no afecta
