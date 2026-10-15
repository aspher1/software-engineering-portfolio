"""Deterministic entry allocator for FOMO Trader (paper bot).

Runs on the entry-commit hot path, under the trader lock, right after the
commit guardrails pass. Pure arithmetic over a feature dict: no network,
no AI/LLM, no subprocess, no randomness. Identical features always give an
identical (take, score, multiplier).

`balance_factor(bankroll_usd, equity_peak_usd, max_drawdown_pct)` applies a
neutral-to-half-size linear drawdown factor. `score_signal_with_balance()`
combines it with the unchanged score multiplier; both use local math only.

    score      = logistic(intercept + sum(weight_f * norm_f(feature_f)))
    take       = score >= take_threshold
    multiplier = piecewise-linear, monotone in score:
                 score 0 -> 0.5x, 0.5 -> 1.0x, 0.7 -> 2.5x, 1 -> 3.0x

Each feature is normalized to [-1, 1] around a "typical signal" reference,
so a typical signal contributes ~0. Missing, null, non-finite or
out-of-domain features contribute exactly 0 (neutral). Any internal error
returns FALLBACK = (True, 0.5, 1.0): take at 1.0x, so entries never stall
on the allocator. The allocator is an additional filter only; the rug
guard, trade caps, kill switches, cooldowns and the money.py risk ceiling
are evaluated independently and always win.

Commit-time feature availability. Every model feature is known before the
position is written: signal fields come from the scanner, holder
concentration from the Solana rug check (BSC: null), slippage from the
entry quote and the in-lock native/USD rate already fetched for bankroll
sizing, and latency from the commit clock. Excluded as post-commit only:
the journal's `commit_price_usd` / `slip_from_signal_pct` (computed from a
second, post-commit rate fetch; the allocator recomputes slippage from the
in-lock rate instead), research tags, and anything from the close record.
LP burn/lock evidence is available but not modeled.

Journal units differ from feature units: `slip_from_signal_pct` is a
fraction (0.14 = +14%) and latency is in ms; features_from_record converts.
`signal_gain_pct` is the scanner window's gain (15m for GeckoTerminal, 5m
for DexScreener signals).

Config (`allocator` section of the bot config):
    mode            "off" | "shadow" | "live". Section absent -> off;
                    section present without mode (or unknown) -> shadow.
    take_threshold  default 0.35.
    weights         per-feature overrides of DEFAULT_WEIGHTS; keys starting
                    with "_" and unknown keys are ignored.
"""
import math

MULT_MIN = 0.5
MULT_MAX = 3.0
DEFAULT_TAKE_THRESHOLD = 0.35
FALLBACK = (True, 0.5, 1.0)
MODES = ("off", "shadow", "live")

FEATURES = (
    "signal_gain_pct", "liquidity_usd", "buy_count_15m", "sell_count_15m",
    "buy_sell_ratio", "volume_15m_usd", "mcap_usd", "holder_top1_pct",
    "holder_top5_pct", "signal_to_fill_slippage_pct", "entry_latency_sec",
    "chain_bsc",
)

# Mild, uncalibrated priors. Sum of |w| is 0.9, so the score stays within
# logistic(+-0.9) = [0.29, 0.71] -> multiplier [0.79, 2.52]; typical signals
# land near 0.5 / 1.0x and only a signal adverse on nearly every feature
# falls below the 0.35 take threshold. The offline analyst proposes
# replacements; nothing here is fitted.
DEFAULT_WEIGHTS = {
    "intercept": 0.0,
    "signal_gain_pct": -0.10,
    "liquidity_usd": 0.15,
    "buy_count_15m": 0.05,
    "sell_count_15m": 0.0,
    "buy_sell_ratio": 0.10,
    "volume_15m_usd": 0.05,
    "mcap_usd": 0.0,
    "holder_top1_pct": -0.15,
    "holder_top5_pct": -0.10,
    "signal_to_fill_slippage_pct": -0.15,
    "entry_latency_sec": -0.05,
    "chain_bsc": 0.0,
}


def _num(x):
    """Finite float or None. Booleans are not numbers here."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def _clamp(v, lo=-1.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def _log_ratio(x, ref, span):
    """log(x/ref)/log(span), clamped: ref -> 0, ref*span -> +1, ref/span -> -1.
    Non-positive x is treated as missing."""
    v = _num(x)
    if v is None or v <= 0:
        return None
    return _clamp(math.log(v / ref) / math.log(span))


def _linear(x, center, scale, lo=None, hi=None):
    v = _num(x)
    if v is None or (lo is not None and v < lo) or (hi is not None and v > hi):
        return None
    return _clamp((v - center) / scale)


def _norm_gain(x):
    v = _num(x)
    if v is None or v <= -100:
        return None
    return _log_ratio(1.0 + v / 100.0, 2.0, 4.0)  # +100% -> 0, +700% -> +1


def _norm_latency(x):
    v = _num(x)
    if v is None or v < 0:
        return None
    return _log_ratio(max(v, 0.1), 2.0, 30.0)  # 2s -> 0, 60s -> +1


def _norm_chain(x):
    if x == "bsc":
        return 1.0
    if x == "solana":
        return 0.0
    return None


NORMALIZERS = {
    "signal_gain_pct": _norm_gain,
    "liquidity_usd": lambda x: _log_ratio(x, 30000.0, 10.0),
    "buy_count_15m": lambda x: _log_ratio(x, 50.0, 10.0),
    "sell_count_15m": lambda x: _log_ratio(x, 20.0, 10.0),
    "buy_sell_ratio": lambda x: _log_ratio(x, 3.0, 5.0),
    "volume_15m_usd": lambda x: _log_ratio(x, 20000.0, 10.0),
    "mcap_usd": lambda x: _log_ratio(x, 500000.0, 100.0),
    "holder_top1_pct": lambda x: _linear(x, 20.0, 20.0, 0.0, 100.0),
    "holder_top5_pct": lambda x: _linear(x, 40.0, 35.0, 0.0, 100.0),
    "signal_to_fill_slippage_pct": lambda x: _linear(x, 0.0, 20.0),
    "entry_latency_sec": _norm_latency,
    "chain_bsc": _norm_chain,
}


def normalize(features):
    """Feature dict -> {feature: value in [-1, 1]}; missing -> 0.0."""
    features = features or {}
    out = {}
    for name in FEATURES:
        src = "chain" if name == "chain_bsc" else name
        v = NORMALIZERS[name](features.get(src))
        out[name] = 0.0 if v is None else v
    return out


def _logistic(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def multiplier_from_score(score):
    s = _clamp(float(score), 0.0, 1.0)
    if s <= 0.5:
        m = 0.5 + s
    elif s <= 0.7:
        m = 1.0 + 7.5 * (s - 0.5)
    else:
        m = 2.5 + (MULT_MAX - 2.5) * (s - 0.7) / 0.3
    return _clamp(m, MULT_MIN, MULT_MAX)


def mode_of(cfg):
    """Allocator mode from the `allocator` config section (or None)."""
    if not isinstance(cfg, dict):
        return "off"
    mode = cfg.get("mode", "shadow")
    return mode if mode in MODES else "shadow"


def weights_of(cfg):
    w = dict(DEFAULT_WEIGHTS)
    over = (cfg or {}).get("weights") if isinstance(cfg, dict) else None
    if isinstance(over, dict):
        for k, v in over.items():
            if k in w:
                w[k] = float(v)
    return w


def threshold_of(cfg):
    t = (cfg or {}).get("take_threshold", DEFAULT_TAKE_THRESHOLD) \
        if isinstance(cfg, dict) else DEFAULT_TAKE_THRESHOLD
    t = float(t)
    if not math.isfinite(t):
        raise ValueError("non-finite take_threshold")
    return t


def score_signal(features, cfg=None):
    """(take, score in [0,1], multiplier in [0.5, 3.0]). Never raises."""
    try:
        w = weights_of(cfg)
        thr = threshold_of(cfg)
        z = w["intercept"]
        for name, v in normalize(features).items():
            z += w[name] * v
        if not math.isfinite(z):
            return FALLBACK
        score = _logistic(z)
        return (score >= thr, score, multiplier_from_score(score))
    except Exception:
        return FALLBACK


def balance_factor(bankroll_usd, equity_peak_usd, max_drawdown_pct):
    """Linear drawdown sizing factor in [0.5, 1.0]; invalid data is neutral."""
    try:
        bankroll = _num(bankroll_usd)
        peak = _num(equity_peak_usd)
        cap_pct = _num(max_drawdown_pct)
        if (bankroll is None or peak is None or peak <= 0 or
                cap_pct is None or cap_pct <= 0):
            return 1.0
        if bankroll >= peak:
            return 1.0
        cap = cap_pct / 100.0
        drawdown = 1.0 - bankroll / peak
        if cap == 0.0:  # positive percentage underflowed during conversion
            return 0.5
        return _clamp(1.0 - 0.5 * min(drawdown, cap) / cap, 0.5, 1.0)
    except Exception:
        return 1.0


def score_signal_with_balance(features, balance, cfg=None):
    """Score with drawdown sizing: (take, score, multiplier, balance factor)."""
    try:
        take, score, score_mult = score_signal(features, cfg)
        balance = balance or {}
        bf = balance_factor(balance.get("bankroll_usd"),
                            balance.get("equity_peak_usd"),
                            balance.get("max_drawdown_pct"))
        return take, score, _clamp(score_mult * bf, MULT_MIN, MULT_MAX), bf
    except Exception:
        return FALLBACK[0], FALLBACK[1], FALLBACK[2], 1.0


def size_native(base_native, multiplier, ceiling_native):
    """Allocator ticket: min(base * multiplier, risk ceiling).

    The money.py ceiling always wins. With no usable ceiling the allocator
    may shrink the ticket but never grow it above the configured base."""
    m = _clamp(float(multiplier), MULT_MIN, MULT_MAX)
    want = base_native * m
    if ceiling_native is None:
        return min(want, base_native)
    return min(want, ceiling_native)


def features_from_signal(signal, chain, entry_native=None, native_usd=None,
                         holder=None, now=None):
    """Commit-time feature dict from the signal plus in-lock observations."""
    signal = signal or {}
    holder = holder or {}
    slip = None
    sig_usd = _num(signal.get("signal_price_usd"))
    ent = _num(entry_native)
    rate = _num(native_usd)
    if sig_usd and sig_usd > 0 and ent is not None and rate is not None:
        slip = (ent * rate / sig_usd - 1.0) * 100.0
    latency = None
    ts = _num(signal.get("ts"))
    if ts is not None and now is not None:
        latency = max(0.0, now - ts)
    return {
        "signal_gain_pct": signal.get("m15_gain_pct"),
        "liquidity_usd": signal.get("liquidity_usd"),
        "buy_count_15m": signal.get("m15_buys"),
        "sell_count_15m": signal.get("m15_sells"),
        "buy_sell_ratio": signal.get("buy_sell_ratio"),
        "volume_15m_usd": signal.get("m15_volume_usd"),
        "mcap_usd": signal.get("mcap_usd"),
        "holder_top1_pct": holder.get("holder_top1_pct"),
        "holder_top5_pct": holder.get("holder_top5_pct"),
        "signal_to_fill_slippage_pct": slip,
        "entry_latency_sec": latency,
        "chain": chain,
    }


def features_from_record(rec):
    """Feature dict from a journal entry record (offline analyst)."""
    rec = rec or {}
    slip = _num(rec.get("slip_from_signal_pct"))
    lat = _num(rec.get("entry_latency_ms"))
    return {
        "signal_gain_pct": rec.get("signal_gain_pct"),
        "liquidity_usd": rec.get("liquidity_usd"),
        "buy_count_15m": rec.get("m15_buys"),
        "sell_count_15m": rec.get("m15_sells"),
        "buy_sell_ratio": rec.get("signal_ratio"),
        "volume_15m_usd": rec.get("m15_volume_usd"),
        "mcap_usd": rec.get("mcap_usd"),
        "holder_top1_pct": rec.get("holder_top1_pct"),
        "holder_top5_pct": rec.get("holder_top5_pct"),
        "signal_to_fill_slippage_pct": slip * 100.0 if slip is not None else None,
        "entry_latency_sec": lat / 1000.0 if lat is not None else None,
        "chain": rec.get("chain") or "solana",
    }
