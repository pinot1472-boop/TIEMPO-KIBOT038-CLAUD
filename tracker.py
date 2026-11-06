"""
Seguimiento en papel de TODAS las señales emitidas (tier A y B, SIGNAL y LIVE).

Sin esto no hay forma de saber si el bot es fiable: en MODE=SIGNAL el bot no
mide qué pasó después de cada aviso, y en LIVE `reconcile` solo compara el
precio actual con la entrada. Aquí cada señal se resuelve contra las velas que
el bot YA descarga en cada ciclo (cero peticiones extra a BingX).

Reglas de resolución (conservadoras):
  · entrada = precio de cierre de la vela de la señal (como en el aviso)
  · se recorren las velas CERRADAS posteriores, en orden
  · si SL y TP caen en la misma vela, cuenta SL
  · dos resultados independientes por señal: «TP1 vs SL» y «TP2 vs SL»
  · caducidad a TRACK_TIMEOUT_BARS velas: se valora a precio de mercado (EXP)
  · R neto = R bruto − FEE_RT_PCT / riesgo%  (las comisiones pesan más cuanto
    más pequeño es el stop)
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import config

log = logging.getLogger("tracker")

MAX_DONE = 3000          # histórico de señales resueltas que se conserva


class Tracker:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {"pending": [], "done": []}
        self._load()

    # ── persistencia ──────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text())
                self.data["pending"] = list(d.get("pending", []))
                self.data["done"] = list(d.get("done", []))
        except Exception as exc:  # noqa: BLE001
            log.warning("Tracker load: %s", exc)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data))
            tmp.replace(self.path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Tracker save: %s", exc)

    # ── alta ──────────────────────────────────────────────────────────
    def add(self, s, live: bool = False) -> None:
        if not config.TRACK_ENABLED:
            return
        if any(p["sym"] == s.symbol and p["bar"] == s.bar_time for p in self.data["pending"]):
            return
        self.data["pending"].append(
            {
                "sym": s.symbol, "side": s.side, "tier": s.tier, "src": s.src,
                "entry": s.entry, "sl": s.sl, "tp1": s.tp1, "tp2": s.tp2,
                "bar": s.bar_time, "ts": time.time(),
                "score": s.score, "xsec": round(s.xsec_pct, 1), "rvol": round(s.rvol, 2),
                "btc": s.btc_state, "trend": s.trend, "abs": bool(s.absorb),
                "risk": round(s.risk_pct, 3), "dist": round(s.dist_atr, 2),
                "fmin": round(s.funding_min), "live": bool(live),
                "o1": None, "o2": None,
            }
        )
        self.save()

    # ── resolución ────────────────────────────────────────────────────
    @staticmethod
    def _walk(p: dict, bars: list[dict]) -> None:
        """Recorre velas cerradas en orden y fija p['o1'] / p['o2'] (TP | SL)."""
        long = p["side"] == "LONG"
        for r in bars:
            hi, lo = r["high"], r["low"]
            sl_hit = lo <= p["sl"] if long else hi >= p["sl"]
            t1 = hi >= p["tp1"] if long else lo <= p["tp1"]
            t2 = hi >= p["tp2"] if long else lo <= p["tp2"]
            if p["o1"] is None:
                if sl_hit:
                    p["o1"] = "SL"
                elif t1:
                    p["o1"] = "TP"
            if p["o2"] is None:
                if sl_hit:
                    p["o2"] = "SL"
                elif t2:
                    p["o2"] = "TP"
            if p["o1"] and p["o2"]:
                return

    @staticmethod
    def _r(p: dict, outcome: str, tp_price: float, px: float) -> float:
        """R neto de comisiones para un resultado."""
        long = p["side"] == "LONG"
        risk_px = abs(p["entry"] - p["sl"]) or 1e-12
        if outcome == "TP":
            gross = abs(tp_price - p["entry"]) / risk_px
        elif outcome == "SL":
            gross = -1.0
        else:  # EXP: precio de mercado, acotado entre -1R y el objetivo
            mtm = ((px - p["entry"]) if long else (p["entry"] - px)) / risk_px
            gross = max(-1.0, min(abs(tp_price - p["entry"]) / risk_px, mtm))
        cost = config.FEE_RT_PCT / p["risk"] if p.get("risk", 0) > 0 else 0.0
        return gross - cost

    def resolve(self, data: dict[str, list[dict]], now: float | None = None) -> int:
        """Resuelve pendientes con las velas del ciclo. Devuelve cuántas cerró."""
        if not self.data["pending"]:
            return 0
        now = now if now is not None else time.time()
        now_ms = now * 1000.0
        tf_ms = config.TF_SEC * 1000
        limit = max(1, config.TRACK_TIMEOUT_BARS)

        still: list[dict] = []
        closed = 0
        for p in self.data["pending"]:
            rows = data.get(p["sym"])
            if not rows:
                # sin datos (par fuera del universo, fallo de red): se espera, pero no eternamente
                if now - p["ts"] > (limit + 12) * config.TF_SEC:
                    closed += 1
                    log.info("track %s: sin datos, descartada", p["sym"])
                else:
                    still.append(p)
                continue

            bars = [r for r in rows if r["time"] > p["bar"] and r["time"] + tf_ms <= now_ms]
            bars.sort(key=lambda r: r["time"])
            bars = bars[:limit]
            self._walk(p, bars)
            expired = len(bars) >= limit
            done1, done2 = p["o1"] is not None, p["o2"] is not None

            if (done1 and done2) or expired:
                px = bars[-1]["close"] if bars else p["entry"]
                o1 = p["o1"] or "EXP"
                o2 = p["o2"] or "EXP"
                rec = {k: p[k] for k in (
                    "sym", "side", "tier", "src", "score", "xsec", "rvol", "btc",
                    "trend", "abs", "risk", "dist", "fmin", "live", "ts")}
                rec.update(
                    o1=o1, o2=o2,
                    r1=round(self._r(p, o1, p["tp1"], px), 3),
                    r2=round(self._r(p, o2, p["tp2"], px), 3),
                )
                self.data["done"].append(rec)
                closed += 1
                log.info("track %s %s [%s/%s]: TP1→%s TP2→%s", p["sym"], p["side"],
                         p["tier"], p["src"], o1, o2)
            else:
                still.append(p)

        self.data["pending"] = still
        self.data["done"] = self.data["done"][-MAX_DONE:]
        if closed:
            self.save()
        return closed

    # ── informe ───────────────────────────────────────────────────────
    @staticmethod
    def _line(label: str, items: list[dict]) -> str:
        n = len(items)

        def part(o_key: str, r_key: str) -> str:
            dec = [d for d in items if d[o_key] in ("TP", "SL")]
            win = sum(1 for d in dec if d[o_key] == "TP")
            wr = f"{win / len(dec) * 100:.0f}%" if dec else "n/a"
            ex = sum(d[r_key] for d in items) / n
            return f"{wr} E{ex:+.2f}R"

        return f"{label}: n={n} · TP1 {part('o1', 'r1')} · TP2 {part('o2', 'r2')}"

    def _group(self, key, min_n: int = 5) -> list[str]:
        g: dict[str, list[dict]] = defaultdict(list)
        for d in self.data["done"]:
            g[key(d)].append(d)
        return [self._line(k, v) for k, v in sorted(g.items()) if len(v) >= min_n]

    def summary(self) -> str:
        done = self.data["done"]
        pend = len(self.data["pending"])
        if not done:
            return f"Seguimiento: sin señales resueltas aún ({pend} en curso)"
        lines = [f"Seguimiento en papel · {len(done)} resueltas · {pend} en curso",
                 "(E = esperanza en R, neta de comisiones; win% sin contar caducadas)"]
        lines += self._group(lambda d: f"{d['tier']}·{d['src']}", min_n=1)
        if len(done) >= 30:
            lines.append("— desglose —")
            lines += self._group(lambda d: f"BTC {d['btc']}")
            lines += self._group(lambda d: f"tend {d['trend']}")
            lines += self._group(lambda d: "absorción" if d["abs"] else "sin absorción")
            lines += self._group(
                lambda d: "p<50" if d["xsec"] < 50 else "p50-75" if d["xsec"] < 75
                else "p75-90" if d["xsec"] < 90 else "p90+")
            lines += self._group(lambda d: "stop corto (riesgo menor de 0.5%)" if d["risk"] < 0.5
                                 else "stop normal (riesgo 0.5% o más)")
            lines += self._group(lambda d: "entrada cerca del nivel" if d["dist"] <= config.NEAR_ATR
                                 else "entrada lejos del nivel")
        return "\n".join(lines).replace("<", "‹").replace(">", "›").replace("&", "y")  # HTML de Telegram
