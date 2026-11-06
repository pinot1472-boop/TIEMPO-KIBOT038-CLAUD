"""
IPDA v3 — sweep de liquidez + accept/reject, con filtros de universo completo.

Edges:
1) Accept/Reject post-sweep (IPDA base)
2) RVOL cross-sectional medido contra TODO el universo escaneado
   (percentil real, no solo entre los candidatos)
3) Régimen BTC calculado SIEMPRE por tendencia (EMA50/EMA200), no solo
   cuando BTC da señal
4) Funding con la hora de liquidación REAL de cada símbolo
5) Absorción: esfuerzo (RVOL) vs resultado (cuerpo/ATR)
6) Filtros de coste: ATR% y distancia al SL mínimos (comisiones se comen
   los stops diminutos de las monedas de baja volatilidad)
7) Tendencia propia: EMA50 / EMA200 del par

v3.1:
- SL estructural: detrás del EXTREMO del sweep (antes: detrás de la vela i, que
  con sweeps de >0.35 ATR dejaba el stop DENTRO de la mecha).
- CONT: SL detrás del nivel roto (antes: en el fondo de toda la caja).
- Última vela cerrada por reloj, no por posición en la lista.
- Embudo (Counter) para ver en qué filtro muere cada símbolo.
- Tiers: A = setup completo; B = menos filtro de volumen / CONT (solo señal).
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from typing import Optional

import numpy as np

import config


@dataclass
class Signal:
    symbol: str
    side: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    score: int
    src: str
    rvol: float
    atr: float
    dist_atr: float
    reason: str
    risk_pct: float = 0.0
    xsec_pct: float = 0.0
    funding_min: float = 999.0
    absorb: bool = False
    btc_state: str = "sin dato"      # alineado | contra | sin dato | líder
    trend: str = "mixta"             # a favor | mixta | en contra
    tier: str = "A"                  # A = completo | B = relajado (solo señal)
    bar_time: int = 0                # apertura (ms) de la vela de confirmación
    ext: float = 0.0                 # extremo estructural usado para el SL


@dataclass
class BtcRegime:
    side: Optional[str] = None       # LONG / SHORT / None (neutral)
    ts: float = 0.0


# ── utilidades ────────────────────────────────────────────────────────
def last_closed_idx(rows: list[dict], now: float | None = None) -> int:
    """
    Índice de la última vela CERRADA según el reloj. Con `len-2` fijo, si la
    vela nueva aún no existe en el exchange (par ilíquido, pocos segundos tras
    el cierre) se analizaba una vela de retraso.
    """
    now_ms = (now if now is not None else time.time()) * 1000.0
    i = len(rows) - 1
    if i >= 0 and rows[i]["time"] + config.TF_SEC * 1000 > now_ms:
        i -= 1
    return i


# ── indicadores ───────────────────────────────────────────────────────
def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    prev = np.roll(c, 1)
    prev[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    out = np.full_like(tr, np.nan, dtype=float)
    if len(tr) < n:
        return out
    out[n - 1] = np.mean(tr[:n])
    for i in range(n, len(tr)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def _rvol(v: np.ndarray, n: int) -> np.ndarray:
    """Volumen / media de las n velas anteriores (vectorizado)."""
    out = np.ones_like(v, dtype=float)
    if len(v) <= n:
        return out
    cs = np.concatenate(([0.0], np.cumsum(v)))
    idx = np.arange(n, len(v))
    m = (cs[idx] - cs[idx - n]) / n
    safe = np.where(m > 0, m, 1.0)
    out[n:] = np.where(m > 0, v[n:] / safe, 1.0)
    return out


def _ema(x: np.ndarray, n: int) -> np.ndarray:
    a = 2.0 / (n + 1.0)
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    for k in range(1, len(x)):
        out[k] = a * x[k] + (1.0 - a) * out[k - 1]
    return out


# ── funding ───────────────────────────────────────────────────────────
def minutes_to_funding(now_ts: float | None = None) -> float:
    """Respaldo: liquidación estándar 00/08/16 UTC si BingX no da la hora."""
    t = now_ts if now_ts is not None else time.time()
    day = int(t) % 86400
    for s in (0, 8 * 3600, 16 * 3600, 24 * 3600):
        if day < s:
            return (s - day) / 60.0
    return (24 * 3600 - day) / 60.0


def funding_edge(side: str, funding_rate_pct: float, mins: float) -> int:
    """
    Funding muy positivo = longs saturados: favorece SHORT, penaliza LONG.
    Inverso con funding negativo. Solo en los ≤45 min previos a la
    liquidación; más peso en los últimos 15.
    """
    if mins > 45:
        return 0
    pts = 0
    if funding_rate_pct >= 0.03:
        pts = 8 if side == "SHORT" else -6
    elif funding_rate_pct <= -0.03:
        pts = 8 if side == "LONG" else -6
    if mins <= 15:
        pts = int(pts * 1.5)
    return pts


# ── régimen BTC ───────────────────────────────────────────────────────
def btc_regime_from_rows(rows: list[dict]) -> BtcRegime:
    """Régimen por tendencia en cada ciclo: independiente de si BTC da señal."""
    if not rows or len(rows) < config.TREND_EMA_FAST + 10:
        return BtcRegime()
    c = np.array([r["close"] for r in rows], dtype=float)
    i = last_closed_idx(rows)
    fast = _ema(c, config.TREND_EMA_FAST)
    slow = _ema(c, config.TREND_EMA_SLOW) if len(c) >= config.TREND_EMA_SLOW else None
    up = c[i] > fast[i] and (slow is None or fast[i] > slow[i])
    dn = c[i] < fast[i] and (slow is None or fast[i] < slow[i])
    side = "LONG" if up else "SHORT" if dn else None
    return BtcRegime(side=side, ts=time.time())


# ── análisis ───────────────────────────────────────────────────────────
def analyze(
    symbol: str,
    rows: list[dict],
    funding_rate_pct: float = 0.0,
    funding_mins: float | None = None,
    btc_regime: Optional[BtcRegime] = None,
    funnel: Optional[Counter] = None,
    now: float | None = None,
) -> tuple[Optional[Signal], Optional[float]]:
    """
    Devuelve (señal | None, rvol_métrica).
    rvol_métrica = RVOL máximo en la ventana de sweep de ESTE símbolo; se usa
    para construir el percentil del universo completo.
    `funnel` (opcional) cuenta en qué filtro cae cada símbolo.
    """

    def fail(key: str) -> None:
        if funnel is not None:
            funnel[key] += 1

    if len(rows) < max(config.LOOKBACK_B, 50) + 5:
        fail("pocos_datos")
        return None, None

    o = np.array([r["open"] for r in rows], dtype=float)
    h = np.array([r["high"] for r in rows], dtype=float)
    l = np.array([r["low"] for r in rows], dtype=float)
    c = np.array([r["close"] for r in rows], dtype=float)
    v = np.array([r["volume"] for r in rows], dtype=float)

    i = last_closed_idx(rows, now)  # última vela cerrada (por reloj)
    acc = config.ACC_BARS
    win = config.SWEEP_WIN
    if i < config.LOOKBACK_B + acc + win + 2:
        fail("pocos_datos")
        return None, None

    atr = _atr(h, l, c, config.ATR_LEN)
    rvol = _rvol(v, config.VOL_LEN)
    a = atr[i]
    if not np.isfinite(a) or a <= 0:
        fail("atr_invalido")
        return None, None

    sweep_end = i - acc
    if sweep_end < win + 5:
        fail("pocos_datos")
        return None, None
    metric = float(np.max(rvol[sweep_end - win + 1 : sweep_end + 1]))

    # filtro de coste: ATR% mínimo
    if a / c[i] * 100.0 < config.MIN_ATR_PCT:
        fail("atr_bajo")
        return None, metric

    # La caja se mide ANTES de la ventana de sweep (ver nota histórica en v2).
    ref = sweep_end - win + 1

    def box_up(n: int) -> float:
        return float(np.max(h[ref - n : ref]))

    def box_dn(n: int) -> float:
        return float(np.min(l[ref - n : ref]))

    up_a, dn_a = box_up(config.LOOKBACK_A), box_dn(config.LOOKBACK_A)
    up_b, dn_b = box_up(config.LOOKBACK_B), box_dn(config.LOOKBACK_B)
    cand_up = [x for x in (up_a, up_b) if x > c[i]]
    cand_dn = [x for x in (dn_a, dn_b) if x < c[i]]
    p_up = min(cand_up) if cand_up else up_a
    p_dn = max(cand_dn) if cand_dn else dn_a

    sw_up = sw_dn = False
    first_up = first_dn = -1          # primera vela de sweep de cada lado
    dep_up = dep_dn = 0.0
    rvol_sw = 1.0
    abs_bar = False

    for j in range(sweep_end - win + 1, sweep_end + 1):
        body = abs(c[j] - o[j]) / a
        if h[j] > p_up and c[j] <= p_up:
            d = (h[j] - p_up) / a
            if config.MIN_DEP_ATR <= d <= config.MAX_DEP_ATR:
                sw_up = True
                if first_up < 0:
                    first_up = j
                dep_up = max(dep_up, d)
                rvol_sw = max(rvol_sw, float(rvol[j]))
                if rvol[j] >= config.ABSORB_RVOL and body <= config.ABSORB_BODY:
                    abs_bar = True
        if l[j] < p_dn and c[j] >= p_dn:
            d = (p_dn - l[j]) / a
            if config.MIN_DEP_ATR <= d <= config.MAX_DEP_ATR:
                sw_dn = True
                if first_dn < 0:
                    first_dn = j
                dep_dn = max(dep_dn, d)
                rvol_sw = max(rvol_sw, float(rvol[j]))
                if rvol[j] >= config.ABSORB_RVOL and body <= config.ABSORB_BODY:
                    abs_bar = True

    if not (sw_up or sw_dn):
        fail("sin_sweep")
        return None, metric
    if rvol_sw < config.SWEEP_RVOL:
        fail("rvol_sweep_bajo")
        return None, metric

    # lvl = nivel de referencia del setup; ext = extremo estructural para el SL
    #   GRAB: ext = extremo de la mecha del sweep (o más allá, si hubo otro empujón)
    #   CONT: ext = mínimo/máximo de las velas de confirmación (fallo de la rotura)
    need = max(1, acc // 2 + 1)
    side = src = None
    lvl = ext = 0.0
    if sw_up:
        below = sum(1 for k in range(sweep_end + 1, i + 1) if c[k] < p_up)
        above = sum(1 for k in range(sweep_end + 1, i + 1) if c[k] > p_up)
        if below >= need:
            side, src, lvl = "SHORT", "GRAB", p_up
            ext = float(np.max(h[first_up : i + 1]))
        elif above >= need and dep_up >= 0.25 and config.ALLOW_CONT:
            side, src, lvl = "LONG", "CONT", p_up
            ext = float(np.min(l[sweep_end + 1 : i + 1]))
    if side is None and sw_dn:
        above = sum(1 for k in range(sweep_end + 1, i + 1) if c[k] > p_dn)
        below = sum(1 for k in range(sweep_end + 1, i + 1) if c[k] < p_dn)
        if above >= need:
            side, src, lvl = "LONG", "GRAB", p_dn
            ext = float(np.min(l[first_dn : i + 1]))
        elif below >= need and dep_dn >= 0.25 and config.ALLOW_CONT:
            side, src, lvl = "SHORT", "CONT", p_dn
            ext = float(np.max(h[sweep_end + 1 : i + 1]))

    if side is None:
        fail("sin_confirmacion")
        return None, metric
    if side == "LONG" and not config.ALLOW_LONG:
        fail("lado_no_permitido")
        return None, metric
    if side == "SHORT" and not config.ALLOW_SHORT:
        fail("lado_no_permitido")
        return None, metric

    # ── BTC-lead ──
    btc_state = "sin dato"
    if symbol == config.BTC_SYMBOL:
        btc_state = "líder"
    elif config.USE_BTC_LEAD and btc_regime is not None and btc_regime.side:
        if btc_regime.side == side:
            btc_state = "alineado"
        else:
            btc_state = "contra"
            if config.BTC_LEAD_STRICT:
                fail("btc_estricto")
                return None, metric

    # ── SL / TP ──
    entry = float(c[i])
    if side == "LONG":
        sl = min(lvl, ext) - config.SL_ATR * a
        risk = entry - sl
        if risk <= 0:
            fail("riesgo_invalido")
            return None, metric
        tp1 = entry + config.TP1_R * risk
        tp2 = entry + config.TP2_R * risk
        dist = abs(entry - lvl) / a
    else:
        sl = max(lvl, ext) + config.SL_ATR * a
        risk = sl - entry
        if risk <= 0:
            fail("riesgo_invalido")
            return None, metric
        tp1 = entry - config.TP1_R * risk
        tp2 = entry - config.TP2_R * risk
        dist = abs(lvl - entry) / a

    risk_pct = risk / entry * 100.0
    if risk_pct < config.MIN_RISK_PCT or risk_pct > config.MAX_RISK_PCT:
        fail("riesgo_fuera_rango")
        return None, metric

    # ── tendencia propia ──
    fast = _ema(c, config.TREND_EMA_FAST)[i]
    has_slow = len(c) >= config.TREND_EMA_SLOW
    slow = _ema(c, config.TREND_EMA_SLOW)[i] if has_slow else None
    if side == "LONG":
        t_full = entry > fast and (slow is None or fast > slow)
        t_part = entry > fast
        t_against = entry < fast and slow is not None and fast < slow
    else:
        t_full = entry < fast and (slow is None or fast < slow)
        t_part = entry < fast
        t_against = entry > fast and slow is not None and fast > slow
    trend = "a favor" if t_full else "en contra" if t_against else "mixta"

    # ── funding ──
    mins_f = funding_mins if funding_mins is not None else minutes_to_funding()
    f_pts = funding_edge(side, funding_rate_pct, mins_f) if config.USE_FUNDING_EDGE else 0

    # ── score ──
    score = 40
    score += 10                                   # rvol_sw ya ≥ SWEEP_RVOL
    if dist <= config.NEAR_ATR:
        score += 12
    score += 10 if src == "GRAB" else 8
    if abs_bar:
        score += 10
    if btc_state == "alineado":
        score += 6
    elif btc_state == "contra":
        score -= config.BTC_AGAINST_PENALTY
    score += f_pts
    if t_full:
        score += 8
    elif t_part:
        score += 4
    elif t_against:
        score -= 6
    if rvol_sw >= 1.8:
        score += 5
    score = int(max(0, min(100, score)))

    if score < config.MIN_SCORE - 15:
        fail("score_bajo")
        return None, metric

    fail("candidato")
    reason = (
        f"{src} {'ABS ' if abs_bar else ''}RVOL {rvol_sw:.2f}x · "
        f"dist {dist:.2f}ATR · riesgo {risk_pct:.2f}% · tend {trend}"
    )
    return (
        Signal(
            symbol=symbol,
            side=side,
            entry=entry,
            sl=float(sl),
            tp1=float(tp1),
            tp2=float(tp2),
            score=score,
            src=src or "—",
            rvol=float(rvol_sw),
            atr=float(a),
            dist_atr=float(dist),
            reason=reason,
            risk_pct=float(risk_pct),
            funding_min=float(mins_f),
            absorb=abs_bar,
            btc_state=btc_state,
            trend=trend,
            bar_time=int(rows[i]["time"]),
            ext=float(ext),
        ),
        metric,
    )


def apply_cross_sectional(
    candidates: list[Signal],
    universe_rvols: list[float],
    min_pct: float | None = None,
) -> list[Signal]:
    """
    Percentil del RVOL de cada candidato contra TODO el universo escaneado en
    este ciclo, y reparto en tiers:

      Tier A  GRAB con percentil ≥ XSEC_RVOL_PCT          (setup completo)
      Tier B  GRAB con percentil entre XSEC_B_PCT y A      (menos volumen)
              CONT con percentil ≥ XSEC_B_PCT              (aún sin validar)

    Tier B es solo señal (nunca LIVE) y existe para medir con el tracker si
    relajar el filtro de volumen compensa. SEND_TIER_B=false lo desactiva.
    Sin universo suficiente (o USE_XSEC_RVOL=false) no hay filtro de
    percentil: GRAB → A, CONT → B, y manda el score.
    """
    if not candidates:
        return []
    a_need = min_pct if min_pct is not None else config.XSEC_RVOL_PCT
    b_need = min(config.XSEC_B_PCT, a_need)
    use_x = config.USE_XSEC_RVOL and len(universe_rvols) >= config.XSEC_MIN_UNIVERSE
    arr = np.sort(np.array(universe_rvols, dtype=float)) if use_x else None

    out: list[Signal] = []
    for s in candidates:
        if use_x:
            rank = float(np.searchsorted(arr, s.rvol, side="right")) / len(arr) * 100.0
            s.xsec_pct = rank
        else:
            rank = 100.0
            s.xsec_pct = 0.0

        if s.src == "CONT":
            tier = "B" if rank >= b_need else None
        else:
            tier = "A" if rank >= a_need else "B" if rank >= b_need else None
        if tier is None or (tier == "B" and not config.SEND_TIER_B):
            continue

        if use_x and tier == "A":
            s.score = int(min(100, s.score + int((rank - a_need) / 5)))
        if s.score < config.MIN_SCORE:
            continue
        s.tier = tier
        out.append(s)

    out.sort(key=lambda x: (x.tier == "A", x.score, x.xsec_pct), reverse=True)
    return out
