# La Red RSS->Facebook — Blindaje del feed (COMPLETADO 13 jun 2026)

## Objetivo
Que las notas NUEVAS nunca se queden sin publicar en Facebook (scenario Make 4634525).

## Causa raiz resuelta
1. WP REST API bloqueada por WAF desde IP Render -> el feed caia a scraper que inventaba fechas sinteticas cambiantes (ahora-Ns) -> Make desincronizaba su puntero por GUID/pubDate.
2. Render corre 2 workers gunicorn; cada worker tenia su _real_dates en memoria -> fechas distintas por request.

## Fix aplicado (commits 0c97450 + f12c1e7)
- Vercel (lared1061.com/feed) es la fuente PRIMARIA (accesible desde cualquier IP, fechas reales, GUID estable).
- Orden de capas: 1.Vercel fresco  2.scraper Next.js  3.cache memoria  4.WP REST.
- _real_dates PERSISTIDO en /tmp/lared_real_dates.json, compartido entre workers con lock -> fechas 100% coherentes entre llamadas y workers.
- Notas reales (Vercel/WP) sobreescriben fechas sinteticas cuando aparecen.
- +/rss-health (monitoreo), -/rss-rescate (eliminada).

## Verificado en PROD
- /rss-proxy: 20 items, fecha "Nace un gigante" IDENTICA en 6 llamadas seguidas (ambos workers).
- Make 4634525: isActive=true, isinvalid=false, cada 5min, url=/rss-proxy, maxResults=3, ids [1,8,5,3] intactos.
- Run 15:27:55 -> status:1, ops:1, transfer:3363 = exito con datos reales.

## Pendiente opcional (decision usuario)
- Upgrade Render srv-d71d17nkijhs73cipvbg a plan "standard" (~$7/mes) para que no duerma (plan starter duerme 15min sin trafico; el primer hit tras dormir tarda ~30s).
