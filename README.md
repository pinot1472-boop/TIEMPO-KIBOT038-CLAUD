# IPDA BingX Bot v3

Escanea **todos los perps USDT de BingX** al cierre de cada vela y envía señales a Telegram (`MODE=SIGNAL`) o ejecuta en BingX (`MODE=LIVE`).

## Despliegue (Railway)
1. Sube el repo a GitHub → Railway → Deploy from GitHub (servicio **Worker**).
2. Volumen en `/data`.
3. Pega `railway_vars.txt` en Variables y rellena `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`.
4. Deploy. Primero `MODE=SIGNAL` varios días.
5. LIVE: `BINGX_API_KEY`, `BINGX_API_SECRET`, `MODE=LIVE`, `LIVE_CONFIRMED=true`. Requiere cuenta en modo **Hedge** (posición LONG/SHORT).

## Estrategia
Sweep de liquidez sobre caja 20/40 velas (medida ANTES del sweep) → Accept/Reject en 3 velas → score.
Filtros: RVOL percentil sobre todo el universo, régimen BTC por EMA50/200, tendencia propia, funding real por símbolo, absorción, ATR%/riesgo% mínimos (comisiones).

## Novedades v3.1
- **SL estructural**: detrás del extremo de la mecha del sweep (antes podía quedar dentro de la mecha). En CONT, detrás del nivel roto.
- **Tiers**: `A` = setup completo (RVOL p≥`XSEC_RVOL_PCT`). `B` = filtro de volumen relajado (p≥`XSEC_B_PCT`) y todas las CONT: solo señal, nunca LIVE. `SEND_TIER_B=false` los desactiva.
- **Embudo en el log**: cada ciclo muestra en qué filtro cae cada símbolo y los percentiles reales de RVOL del universo.
- **Seguimiento en papel** (`tracker.py`): resuelve cada señal (TP1/TP2/SL) con las velas que ya se descargan y publica win% y esperanza en R neta de comisiones en el heartbeat, desglosada por tier, origen, BTC, tendencia, absorción, percentil, tamaño de stop y distancia. Datos en `TRACK_PATH`.
- Última vela cerrada por reloj; precios del aviso redondeados a la precisión del contrato.

## Archivos
bot.py · strategy.py · bingx.py · notify.py · tracker.py · config.py · Procfile · railway.toml · railway_vars.txt · requirements.txt

`tradingview/ipda_v3.pine` → versión Pine v6 de la estrategia para backtest en TradingView (no se despliega en Railway).
