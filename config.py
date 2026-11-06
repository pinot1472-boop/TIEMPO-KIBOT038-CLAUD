"""
Configuración IPDA BingX Bot v3 — escaneo de TODO el universo BingX.
Todo se controla por variables de entorno (Railway).
"""
from __future__ import annotations

import os


def _bool(n: str, d: bool = False) -> bool:
    return os.getenv(n, str(d)).strip().lower() in ("1", "true", "yes", "si", "sí")


def _float(n: str, d: float) -> float:
    try:
        return float(os.getenv(n, str(d)))
    except (TypeError, ValueError):
        return d


def _int(n: str, d: int) -> int:
    try:
        return int(float(os.getenv(n, str(d))))
    except (TypeError, ValueError):
        return d


def _list(n: str, d: str) -> tuple[str, ...]:
    return tuple(x.strip().upper() for x in os.getenv(n, d).split(",") if x.strip())


# ── modo ──────────────────────────────────────────────────────────────
MODE = os.getenv("MODE", "SIGNAL").strip().upper()
LIVE_CONFIRMED = _bool("LIVE_CONFIRMED", False)

# ── BingX ─────────────────────────────────────────────────────────────
BINGX_API_KEY = os.getenv("BINGX_API_KEY", "").strip()
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET", "").strip()
BINGX_BASE_URL = os.getenv("BINGX_BASE_URL", "https://open-api.bingx.com").strip()
RECV_WINDOW = _int("RECV_WINDOW", 5000)
POST_ONLY = _bool("POST_ONLY", True)

# ── Telegram ──────────────────────────────────────────────────────────
TELEGRAM_TOKEN = (
    os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or ""
).strip()
TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or ""
).strip()

# ── universo y ritmo de escaneo ───────────────────────────────────────
TIMEFRAME = os.getenv("TIMEFRAME", "15m").strip()
_TF_SEC = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400,
}
TF_SEC = _TF_SEC.get(TIMEFRAME.lower(), 900)

KLINE_LIMIT = _int("KLINE_LIMIT", 300)          # 300 velas: EMA200 estable
BAR_DELAY_SEC = _int("BAR_DELAY_SEC", 5)        # espera tras el cierre de vela
MAX_SYMBOLS = _int("MAX_SYMBOLS", 0)            # 0 = TODOS los contratos
MIN_QUOTE_VOL_24H = _float("MIN_QUOTE_VOL_24H", 1_000_000)  # filtro iliquidez
SYMBOL_REFRESH_MIN = _int("SYMBOL_REFRESH_MIN", 60)
CONCURRENCY = _int("CONCURRENCY", 8)            # peticiones simultáneas
RATE_PER_SEC = _float("RATE_PER_SEC", 8.0)      # techo de peticiones/seg a BingX
EXCLUDE_PREFIXES = _list(
    "EXCLUDE_PREFIXES", "USDC,BUSD,TUSD,FDUSD,NCCO,NCSI,NCSK,NCFX,NCS"
)
EXCLUDE_BASES = _list(
    "EXCLUDE_BASES",
    "USDC,EUR,GBP,XAU,XAG,GOLD,SILVER,OIL,WTI,BRENT,NASDAQ,SPX",
)
SYMBOLS = [
    s.strip().upper().replace("USDT", "-USDT") if "-" not in s.strip().upper() else s.strip().upper()
    for s in os.getenv("SYMBOLS", "").split(",")
    if s.strip()
]

# ── estrategia IPDA ───────────────────────────────────────────────────
LOOKBACK_A = _int("LOOKBACK_A", 20)
LOOKBACK_B = _int("LOOKBACK_B", 40)
SWEEP_WIN = _int("SWEEP_WIN", 3)
MIN_DEP_ATR = _float("MIN_DEP_ATR", 0.03)
MAX_DEP_ATR = _float("MAX_DEP_ATR", 0.55)
SWEEP_RVOL = _float("SWEEP_RVOL", 1.20)
ACC_BARS = _int("ACC_BARS", 3)
NEAR_ATR = _float("NEAR_ATR", 0.45)
MIN_SCORE = _int("MIN_SCORE", 60)
ALLOW_LONG = _bool("ALLOW_LONG", True)
ALLOW_SHORT = _bool("ALLOW_SHORT", True)
ALLOW_CONT = _bool("ALLOW_CONT", True)          # setups CONT (siempre tier B)
ATR_LEN = _int("ATR_LEN", 14)
VOL_LEN = _int("VOL_LEN", 20)

# ── filtros de calidad (nuevos) ───────────────────────────────────────
MIN_ATR_PCT = _float("MIN_ATR_PCT", 0.15)       # ATR/precio mínimo en %
MIN_RISK_PCT = _float("MIN_RISK_PCT", 0.30)     # distancia al SL mínima en %
MAX_RISK_PCT = _float("MAX_RISK_PCT", 5.0)      # distancia al SL máxima en %
TREND_EMA_FAST = _int("TREND_EMA_FAST", 50)
TREND_EMA_SLOW = _int("TREND_EMA_SLOW", 200)

# ── edges diferenciales ───────────────────────────────────────────────
USE_XSEC_RVOL = _bool("USE_XSEC_RVOL", True)
XSEC_RVOL_PCT = _float("XSEC_RVOL_PCT", 75.0)   # percentil sobre TODO el universo
XSEC_MIN_UNIVERSE = _int("XSEC_MIN_UNIVERSE", 30)
XSEC_B_PCT = _float("XSEC_B_PCT", 50.0)         # tier B: percentil mínimo (< XSEC_RVOL_PCT)
SEND_TIER_B = _bool("SEND_TIER_B", True)        # false = solo tier A
MAX_B_CYCLE = _int("MAX_B_CYCLE", 5)            # tope de señales B por ciclo
USE_BTC_LEAD = _bool("USE_BTC_LEAD", True)
BTC_LEAD_STRICT = _bool("BTC_LEAD_STRICT", False)
BTC_AGAINST_PENALTY = _int("BTC_AGAINST_PENALTY", 8)
BTC_SYMBOL = os.getenv("BTC_SYMBOL", "BTC-USDT").strip()
USE_FUNDING_EDGE = _bool("USE_FUNDING_EDGE", True)
ABSORB_RVOL = _float("ABSORB_RVOL", 1.50)
ABSORB_BODY = _float("ABSORB_BODY", 0.35)
MAX_SIGNALS_CYCLE = _int("MAX_SIGNALS_CYCLE", 5)

# ── riesgo / ejecución (solo MODE=LIVE) ───────────────────────────────
RISK_PCT = _float("RISK_PCT", 0.85)
LEVERAGE = _int("LEVERAGE", 5)                  # apalancamiento fijado en el exchange
MAX_LEVERAGE = _float("MAX_LEVERAGE", 5.0)      # tope de nocional/equity
SL_ATR = _float("SL_ATR", 0.35)
TP1_R = _float("TP1_R", 1.0)
TP2_R = _float("TP2_R", 2.0)
LIVE_TP = os.getenv("LIVE_TP", "TP1").strip().upper()   # TP1 | TP2
MAX_TRADES_DAY = _int("MAX_TRADES_DAY", 6)      # solo cuenta órdenes LIVE
MAX_LOSSES_DAY = _int("MAX_LOSSES_DAY", 3)
COOLDOWN_BARS = _int("COOLDOWN_BARS", 6)        # velas de espera por símbolo
MAX_OPEN = _int("MAX_OPEN", 3)

# ── seguimiento en papel (¿qué habría pasado con cada señal?) ─────────
TRACK_ENABLED = _bool("TRACK_ENABLED", True)
TRACK_PATH = os.getenv("TRACK_PATH", "/data/ipda_track.json")
TRACK_TIMEOUT_BARS = _int("TRACK_TIMEOUT_BARS", 48)   # velas hasta dar la señal por caducada
FEE_RT_PCT = _float("FEE_RT_PCT", 0.10)         # comisión ida+vuelta en % (taker 2x0.05)

# ── infraestructura ───────────────────────────────────────────────────
HEARTBEAT_MIN = _int("HEARTBEAT_MIN", 240)
STATE_PATH = os.getenv("STATE_PATH", "/data/ipda_state.json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
LOG_HTTP = _bool("LOG_HTTP", False)
