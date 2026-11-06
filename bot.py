"""
IPDA BingX Bot v3 — escanea TODOS los perps USDT de BingX al cierre de cada vela.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from collections import Counter
from typing import Optional

import httpx
import numpy as np

import config
from bingx import BingX, BingXError
from notify import State, Telegram
from tracker import Tracker
from strategy import (
    Signal,
    analyze,
    apply_cross_sectional,
    btc_regime_from_rows,
    BtcRegime,
    minutes_to_funding,
)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ipda")
STOP = asyncio.Event()

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _handle_sig(*_):
    log.info("Parada")
    STOP.set()


def round_sig(bx: BingX, s: Signal) -> None:
    """Redondea entrada/SL/TP a la precisión del contrato (antes salían con 15 decimales)."""
    for f in ("entry", "sl", "tp1", "tp2"):
        setattr(s, f, bx.round_price(s.symbol, getattr(s, f)))


def fmt_sig(s: Signal, mode: str) -> str:
    arrow = "🟢 LONG" if s.side == "LONG" else "🔴 SHORT"
    live = "LIVE" if (mode == "LIVE" and s.tier == "A") else "SOLO SEÑAL"
    abs_t = " · ABSORCIÓN" if s.absorb else ""
    tier = " · 🟡 TIER B (filtro relajado)" if s.tier == "B" else ""
    btc = {"alineado": "alineado ✓", "contra": "EN CONTRA ✗", "líder": "es el líder"}.get(
        s.btc_state, "sin dato"
    )
    xs = f"p{s.xsec_pct:.0f}" if s.xsec_pct > 0 else "n/a"
    return (
        f"<b>IPDA v3 · {arrow}</b> · {live}{abs_t}{tier}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Par: <b>{s.symbol}</b>\n"
        f"Origen: {s.src} · Score <b>{s.score}/100</b>\n"
        f"RVOL universo: <b>{xs}</b> · Funding en {s.funding_min:.0f}m\n"
        f"BTC: {btc} · Tendencia: {s.trend}\n"
        f"\n"
        f"📍 Entrada: <code>{s.entry}</code>\n"
        f"🛑 SL: <code>{s.sl}</code> ({s.risk_pct:.2f}%)\n"
        f"🎯 TP1: <code>{s.tp1}</code> · TP2: <code>{s.tp2}</code>\n"
        f"\n"
        f"RVOL {s.rvol:.2f}x · Dist {s.dist_atr:.2f} ATR\n"
        f"{s.reason}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"TF {config.TIMEFRAME} · MODE={mode}"
    )


# ── universo ──────────────────────────────────────────────────────────
async def build_universe(bx: BingX) -> Optional[list[str]]:
    """Todos los contratos USDT activos (o SYMBOLS si se fija). None si falla."""
    try:
        all_syms = await bx.symbols()   # también carga precisiones
    except Exception as exc:  # noqa: BLE001
        log.error("simbolos: %s", exc)
        return None
    if not all_syms:
        return None

    if config.SYMBOLS:
        wanted = set(config.SYMBOLS)
        syms = [s for s in all_syms if s in wanted]
    else:
        try:
            vols = await bx.tickers_24h()
        except Exception as exc:  # noqa: BLE001
            log.warning("tickers: %s", exc)
            vols = {}
        syms = all_syms
        if vols and any(v > 0 for v in vols.values()):
            ranked = sorted(
                (s for s in all_syms if vols.get(s, 0) >= config.MIN_QUOTE_VOL_24H),
                key=lambda s: vols.get(s, 0),
                reverse=True,
            )
            if ranked:
                syms = ranked
        if config.MAX_SYMBOLS > 0:
            syms = syms[: config.MAX_SYMBOLS]

    btc = config.BTC_SYMBOL
    if config.USE_BTC_LEAD and btc in all_syms:
        syms = [btc] + [s for s in syms if s != btc]
    return syms


async def fetch_all(bx: BingX, symbols: list[str]) -> dict[str, list[dict]]:
    """Velas de todos los símbolos en paralelo (el limitador de bx marca el ritmo)."""
    sem = asyncio.Semaphore(max(1, config.CONCURRENCY))

    async def one(sym: str):
        async with sem:
            if STOP.is_set():
                return sym, None
            try:
                return sym, await bx.klines(sym, config.TIMEFRAME, config.KLINE_LIMIT)
            except Exception as exc:  # noqa: BLE001
                log.debug("klines %s: %s", sym, exc)
                return sym, None

    res = await asyncio.gather(*(one(s) for s in symbols))
    return {s: r for s, r in res if r}


# ── ejecución real ────────────────────────────────────────────────────
async def try_live_order(bx: BingX, sig: Signal, equity: float) -> Optional[str]:
    risk_px = abs(sig.entry - sig.sl)
    if risk_px <= 0 or equity <= 0:
        return None
    qty = (equity * config.RISK_PCT / 100.0) / risk_px
    qty = min(qty, (equity * config.MAX_LEVERAGE) / sig.entry)
    qty = bx.round_qty(sig.symbol, qty)
    if qty <= 0 or qty < bx.min_qty(sig.symbol):
        log.warning("%s qty baja %.8f", sig.symbol, qty)
        return None

    side = "BUY" if sig.side == "LONG" else "SELL"
    pos_side = "LONG" if sig.side == "LONG" else "SHORT"
    sl = bx.round_price(sig.symbol, sig.sl)
    tp = bx.round_price(sig.symbol, sig.tp2 if config.LIVE_TP == "TP2" else sig.tp1)
    cid = f"ipda{int(time.time() * 1000)}"

    await bx.set_margin_mode(sig.symbol, "ISOLATED")
    try:
        await bx.set_leverage(sig.symbol, pos_side, config.LEVERAGE)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s leverage: %s", sig.symbol, exc)

    try:
        # SL y TP viajan en la MISMA orden: sin ventana de posición desprotegida.
        await bx.market_order(sig.symbol, side, qty, sl, tp, cid)
        return cid
    except (BingXError, httpx.HTTPError) as exc:
        log.error("LIVE %s: %s", sig.symbol, exc)
        # la respuesta pudo perderse con la orden ya ejecutada
        try:
            if await bx.order_exists(sig.symbol, cid):
                return cid
        except Exception:  # noqa: BLE001
            pass
        return None


async def reconcile(bx: BingX, tg: Telegram, state: State) -> None:
    """Quita de state['open'] lo que ya no está abierto en BingX y cuenta W/L."""
    opens = state.data.get("open", {})
    if not opens:
        return
    try:
        pos = await bx.open_positions()
    except Exception as exc:  # noqa: BLE001
        log.warning("posiciones: %s", exc)
        return
    live = {
        str(p.get("symbol"))
        for p in pos
        if float(p.get("positionAmt", 0) or 0) != 0
    }
    for sym, info in list(opens.items()):
        if sym in live or time.time() - float(info.get("ts", 0)) < 90:
            continue
        won = False
        try:
            rows = await bx.klines(sym, config.TIMEFRAME, 3)
            px = rows[-1]["close"]
            won = px > info["entry"] if info["side"] == "LONG" else px < info["entry"]
        except Exception:  # noqa: BLE001
            pass
        state.close_trade(sym, won)
        await tg.send(
            f"{'✅' if won else '❌'} <b>{sym}</b> {info.get('side')} cerrada "
            f"(resultado aprox.) · W/L {state.data['wins']}/{state.data['losses']}"
        )


# ── ciclo de escaneo ──────────────────────────────────────────────────
async def scan_once(
    bx: BingX, tg: Telegram, state: State, trk: Tracker, symbols: list[str], mode: str
) -> dict:
    t0 = time.time()
    state.roll_day()
    live = mode == "LIVE" and config.LIVE_CONFIRMED

    equity = 0.0
    if live:
        await reconcile(bx, tg, state)
        try:
            equity = (await bx.cuenta()).get("equity", 0.0) or await bx.balance_usdt()
        except Exception as exc:  # noqa: BLE001
            log.warning("balance: %s", exc)

    fmap = await bx.funding_info()
    data = await fetch_all(bx, symbols)
    trk.resolve(data, time.time())

    regime = BtcRegime()
    if config.USE_BTC_LEAD and config.BTC_SYMBOL in data:
        regime = btc_regime_from_rows(data[config.BTC_SYMBOL])

    now = time.time()
    universe: list[float] = []
    candidates: list[Signal] = []
    funnel: Counter = Counter()
    stale = 0

    for sym, rows in data.items():
        if STOP.is_set():
            break
        # par parado/deslistado: la última vela es vieja
        if now * 1000 - rows[-1]["time"] > 2 * config.TF_SEC * 1000:
            stale += 1
            continue
        rate, nxt = fmap.get(sym, (0.0, 0.0))
        mins = (nxt - now) / 60.0 if nxt > now else minutes_to_funding(now)
        reg = None if sym == config.BTC_SYMBOL else regime
        sig, metric = analyze(sym, rows, rate, mins, reg, funnel=funnel)
        if metric is not None:
            universe.append(metric)
        if sig:
            if state.can_signal(sym):
                candidates.append(sig)
            else:
                funnel["en_cooldown"] += 1

    ranked = apply_cross_sectional(candidates, universe)
    final_a = [s for s in ranked if s.tier == "A"][: config.MAX_SIGNALS_CYCLE]
    final_b = [s for s in ranked if s.tier == "B"][: config.MAX_B_CYCLE]
    final = final_a + final_b

    p50, p75, p90 = np.percentile(universe, [50, 75, 90]) if universe else (0.0, 0.0, 0.0)
    log.info(
        "Embudo %s | RVOL universo p50/p75/p90 = %.2f/%.2f/%.2f | "
        "tras filtro universo %d/%d (A=%d B=%d)",
        dict(funnel), p50, p75, p90, len(ranked), len(candidates), len(final_a), len(final_b),
    )

    for sig in final:
        log.info(
            "SENAL[%s] %s %s score=%s xsec=p%.0f %s",
            sig.tier, sig.side, sig.symbol, sig.score, sig.xsec_pct, sig.src,
        )
        round_sig(bx, sig)
        await tg.send(fmt_sig(sig, mode))
        state.mark_signal(sig.symbol)
        will_trade = live and equity > 0 and sig.tier == "A"
        trk.add(sig, live=will_trade)

        if will_trade:
            if not state.can_trade_live(sig.symbol):
                await tg.send(f"⏸ {sig.symbol}: límite diario/posiciones, solo señal")
            else:
                oid = await try_live_order(bx, sig, equity)
                if oid:
                    state.mark_trade(
                        sig.symbol,
                        {
                            "side": sig.side,
                            "entry": sig.entry,
                            "sl": sig.sl,
                            "tp": sig.tp2 if config.LIVE_TP == "TP2" else sig.tp1,
                            "ts": time.time(),
                            "oid": oid,
                        },
                    )
                    await tg.send(f"OK Orden <b>{sig.symbol}</b> {sig.side}")
                else:
                    await tg.send(f"WARN {sig.symbol} señal OK, orden rechazada")
        await asyncio.sleep(0.4)  # respeta el límite de Telegram

    elapsed = time.time() - t0
    log.info(
        "Ciclo: %d/%d pares con datos (%d parados) | %d candidatos → %d señales | "
        "BTC=%s | %.0fs",
        len(data), len(symbols), stale, len(candidates), len(final),
        regime.side or "neutral", elapsed,
    )
    if candidates and not final:
        for s in sorted(candidates, key=lambda x: x.score, reverse=True)[:3]:
            log.info("  cerca: %s %s score=%s rvol=%.2f %s", s.side, s.symbol, s.score, s.rvol, s.src)
    return {"pairs": len(data), "cands": len(candidates), "sigs": len(final), "sec": elapsed}


def seconds_to_next_bar() -> float:
    now = time.time()
    return config.TF_SEC - (now % config.TF_SEC) + config.BAR_DELAY_SEC


async def main() -> None:
    mode = config.MODE
    if mode == "LIVE" and not config.LIVE_CONFIRMED:
        log.warning("LIVE sin LIVE_CONFIRMED -> SIGNAL")
        mode = "SIGNAL"
    if mode == "LIVE" and not (config.BINGX_API_KEY and config.BINGX_API_SECRET):
        log.warning("LIVE sin claves API -> SIGNAL")
        mode = "SIGNAL"

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    state = State(config.STATE_PATH)
    trk = Tracker(config.TRACK_PATH)
    async with httpx.AsyncClient() as client:
        bx = BingX(client)
        tg = Telegram(client)

        symbols = await build_universe(bx) or ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
        await tg.send(
            f"IPDA v3 arrancado\n"
            f"Modo: <b>{mode}</b> · TF {config.TIMEFRAME}\n"
            f"Pares: <b>{len(symbols)}</b> "
            f"(vol24h ≥ {config.MIN_QUOTE_VOL_24H:,.0f} USDT)\n"
            f"Escaneo al cierre de cada vela de {config.TIMEFRAME}\n"
            f"Edges: RVOL universo A p≥{config.XSEC_RVOL_PCT:.0f} / "
            f"B p≥{config.XSEC_B_PCT:.0f} ({'ON' if config.SEND_TIER_B else 'off'}) · "
            f"BTC-lead={'ON' if config.USE_BTC_LEAD else 'off'} · "
            f"Funding={'ON' if config.USE_FUNDING_EDGE else 'off'}\n"
            f"Máx {config.MAX_SIGNALS_CYCLE} señales/ciclo · score≥{config.MIN_SCORE}"
        )

        last_refresh = last_hb = time.time()
        stats: dict = {}
        first = True
        while not STOP.is_set():
            if not first:
                try:
                    await asyncio.wait_for(STOP.wait(), timeout=seconds_to_next_bar())
                except asyncio.TimeoutError:
                    pass
                if STOP.is_set():
                    break
            first = False

            if time.time() - last_refresh >= config.SYMBOL_REFRESH_MIN * 60:
                new = await build_universe(bx)
                if new:
                    symbols = new
                last_refresh = time.time()

            try:
                stats = await scan_once(bx, tg, state, trk, symbols, mode)
            except Exception as exc:  # noqa: BLE001
                log.exception("scan: %s", exc)

            if time.time() - last_hb >= config.HEARTBEAT_MIN * 60:
                last_hb = time.time()
                d = state.data
                await tg.send(
                    f"Heartbeat · {len(symbols)} pares · último ciclo {stats.get('sec', 0):.0f}s\n"
                    f"Señales hoy {d.get('day_signals', 0)} · "
                    f"trades hoy {d.get('day_trades', 0)} · W/L {d.get('wins', 0)}/{d.get('losses', 0)}\n\n"
                    f"{trk.summary()}"
                )
        await tg.send("IPDA v3 detenido")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
