"""Near-miss diagnostic: what the DexScreener fallback sees and why each
token fails the production filter. Run with the project venv python.
Usage: .venv/bin/python diag_nearmiss.py --config runs/paper-1h/config.json
"""
import argparse
import json
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__file__))
from fomo_trader import DexScreenerSource, ds_get  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    with open(args.config) as f:
        cfg = json.load(f)
    src = DexScreenerSource(cfg)
    d = src.d
    try:
        boosts = ds_get("/token-boosts/top/v1", cfg, timeout=15)
    except Exception as e:
        print(json.dumps({"ts": time.time(), "error": "boosts failed: %s" % e}))
        return
    addrs = [b.get("tokenAddress") for b in (boosts or [])
             if b.get("chainId") == "solana" and b.get("tokenAddress")]
    addrs = addrs[:d.get("max_tokens", 30)]
    try:
        resp = ds_get("/latest/dex/tokens/" + ",".join(addrs), cfg, timeout=20)
    except Exception as e:
        print(json.dumps({"ts": time.time(), "error": "stats failed: %s" % e}))
        return
    rows = []
    for p in (resp or {}).get("pairs") or []:
        if p.get("chainId") != "solana":
            continue
        bt = p.get("baseToken") or {}
        pc = float((p.get("priceChange") or {}).get("m5") or 0)
        vol = float((p.get("volume") or {}).get("m5") or 0)
        tx = (p.get("txns") or {}).get("m5") or {}
        buys, sells = int(tx.get("buys") or 0), int(tx.get("sells") or 0)
        fails = []
        if pc < d.get("min_m5_gain_pct", 5):
            fails.append("m5 %.1f%%" % pc)
        if vol < d.get("min_m5_volume_usd", 2000):
            fails.append("vol $%.0f" % vol)
        if buys < d.get("min_m5_buys", 10):
            fails.append("buys %d" % buys)
        if sells < d.get("min_m5_sells", 5):
            fails.append("sells %d" % sells)
        if buys / max(sells, 1) < d.get("min_buy_sell_ratio", 1.5):
            fails.append("ratio %.1f" % (buys / max(sells, 1)))
        rows.append({"sym": bt.get("symbol") or bt.get("name") or "?",
                     "m5": round(pc, 1), "vol": round(vol),
                     "buys": buys, "sells": sells,
                     "pass": not fails, "fails": fails})
    rows.sort(key=lambda r: r["m5"], reverse=True)
    print(json.dumps({"ts": time.time(), "scanned": len(addrs),
                      "passed": sum(1 for r in rows if r["pass"]),
                      "tokens": rows[:15]}))


if __name__ == "__main__":
    main()
