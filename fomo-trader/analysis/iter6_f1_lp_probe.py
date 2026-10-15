"""Iter-6 F1 feasibility spike (lane-direct): can public BSC RPC answer
LP-burn/lock questions for historical one-tick wipe mints?

For each BSC wipe mint (pinned -100% or realized_usd <= -3.5):
  pool address <- entry.buy_sig (GeckoTerminal /bsc/pools/<pool>)
  totalSupply(pool) via eth_call 0x18160ddd
  balanceOf(0x...dead), balanceOf(0x0...0) via 0x70a08231

Classification (current-state, descriptive only):
  - BURNED: dead+zero >= 95% of totalSupply
  - WITHDRAWN: totalSupply tiny relative to entry-time liquidity (LP pulled)
  - LIVE: supply materially present and neither burned nor zero
  - RPC_FAIL: contract/call failure

Read-only, public RPC, polite pacing. Not a strategy candidate.
"""
import json
import time
import urllib.request

RPC_URL = "https://bsc-dataseed.binance.org/"
DEAD = "0x000000000000000000000000000000000000dead"
ZERO = "0x0000000000000000000000000000000000000000"
MAX_MINTS = 12


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def call(to, data):
    return rpc("eth_call", [{"to": to, "data": data}, "latest"]).get("result")


def bal_of(pool, addr):
    return call(pool, "0x70a08231" + "0" * 24 + addr[2:])


def main():
    entries = {}
    for line in open("runs/paper-1h/trades.jsonl"):
        r = json.loads(line)
        if r.get("type") == "entry":
            entries[r.get("mint")] = r
    wipes = []
    for line in open("runs/paper-1h/trades.jsonl"):
        c = json.loads(line)
        if (c.get("type") == "close" and c.get("chain") == "bsc"
                and (c.get("realized_usd") or 0) <= -3.5):
            e = entries.get(c["mint"])
            if e and e.get("buy_sig") and "/bsc/pools/" in e["buy_sig"]:
                wipes.append((c, e))
                if len(wipes) >= MAX_MINTS:
                    break
    print(f"probing {len(wipes)} wipe mints")
    results = []
    for c, e in wipes:
        pool = e["buy_sig"].rstrip("/").split("/")[-1]
        try:
            ts = call(pool, "0x18160ddd")
            time.sleep(0.4)
            db = bal_of(pool, DEAD)
            time.sleep(0.4)
            zb = bal_of(pool, ZERO)
            time.sleep(0.4)
            tot = int(ts, 16) if ts else None
            dead = int(db, 16) if db else None
            zero = int(zb, 16) if zb else None
            if tot is None:
                verdict = "RPC_FAIL"
            elif tot == 0:
                verdict = "SUPPLY_ZERO"
            elif (dead or 0) + (zero or 0) >= 0.95 * tot:
                verdict = "BURNED"
            elif tot < 10**15:
                verdict = "WITHDRAWN_TINY_SUPPLY"
            else:
                verdict = "LIVE_SUPPLY_PRESENT"
            results.append({
                "name": e.get("name"), "mint": e["mint"], "pool": pool,
                "realized_usd": c.get("realized_usd"),
                "lp_total_supply_wei": tot,
                "dead_wei": dead, "zero_wei": zero, "verdict": verdict,
            })
        except Exception as ex:  # noqa: BLE001 - probe must not die
            results.append({"name": e.get("name"), "mint": e["mint"],
                            "pool": pool, "verdict": "RPC_FAIL",
                            "error": type(ex).__name__})
        print(json.dumps(results[-1]))
    with open("hidden_files/deep-improvement-loop/iter-6/f1_lp_probe_results.json",
              "w") as f:
        json.dump(results, f, indent=1)
    tally = {}
    for r in results:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    print("tally:", tally)


if __name__ == "__main__":
    main()
