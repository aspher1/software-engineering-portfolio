# FOMO Trader — end-to-end automated memecoin system (Solana)

> **Paper/dry-run mode.** This system was designed, built, and validated as a
> paper-trading system. It never traded real funds: `dry_run` is the
> operating mode, and all performance figures are paper results.

Buys **only** FOMO setups: violent buy-pressure pumps (100%+ in 15 min with
buys crushing sells 10-to-1 or better — the NPC/POOPFUN pattern). Then it
manages each position on a tight 5-second loop: take-profit ladder, trailing
stop, hard stop. First trigger wins, sells immediately with high priority fees.

## The loop

1. **HUNTER** — every 30s, scans 5 trending + 10 new-pool pages on
   GeckoTerminal (~450 pools) in parallel. Applies the FOMO filter:
   `m15 gain >= 100%`, `buys/sells >= 10`, `>= 50 buys/15m`,
   `liquidity >= $15k`, `15m volume >= $5k`. Nothing else qualifies.
2. **BUY** — on a fresh signal (guardrails pass), swaps SOL for the token
   via Jupiter with your wallet.
3. **MANAGE** — per-position thread checks price every 5s:
   - Take-profit ladder: sell 50% at +100%, 25% more at +200%
   - Trailing stop: sell all on -30% from peak
   - Hard stop: sell all on -40% from entry
4. **GUARDRAILS** — max 3 open positions, max 10 trades/day, 2h cooldown per
   token, daily loss kill-switch (default 0.5 SOL).

## Setup (5 minutes, your machine, your wallet)

```bash
cd fomo-trader
bash setup.sh                      # creates venv, installs deps, makes config.json
```

Then:
1. Put your Solana keypair JSON (the `[1,2,3,...]` array format from the
   Solana CLI or a Phantom export) at `~/.config/solana/id.json`
   — or point `keypair_path` in `config.json` at it.
   **Never send this file to anyone, including me.**
2. Fund that wallet with SOL (trade size + a little extra for fees).
3. Edit `config.json`: trade size (`buy_sol_per_trade`), stops, take-profits.
4. Dry-run first (default is ON — it paper-trades with live prices and logs
   every decision without sending anything):
   ```bash
   .venv/bin/python fomo_trader.py --config config.json --hunt   # read-only scan test
   .venv/bin/python fomo_trader.py --config config.json          # paper-trade loop
   ```
5. When the paper trading looks right, set `dry_run: false` and run it again.
   Start with a tiny `buy_sol_per_trade` (0.02–0.05).

Run it in the background: `nohup .venv/bin/python fomo_trader.py --config config.json > bot.log 2>&1 &`
State (positions, peaks, cooldowns) persists in `state.json`, so restarts
resume managing open positions.

## Honest limits — read this

- **"Zero latency" doesn't exist.** Realistic path: price check every ~5s,
  then quote → build → sign → send → confirm takes ~3–8s with priority fees.
  In a -90% candle you will get filled below your stop. Size positions so a
  bad fill doesn't hurt.
- **Most FOMO pumps still dump.** The filter finds buy pressure, not winners.
  The edge is mechanical exits + small size + the kill switch, not picking.
- **Dry-run first, tiny size second.** Bugs in money-moving code cost money.
  Read `fomo_trader.py` before going live.
- Jupiter's free API has rate limits; the defaults (30s scans, 5s price
  checks, ≤3 positions) stay well under them.

## Files

- `fomo_trader.py` — the whole system (hunter + trader + risk)
- `config.example.json` / `config.json` — all tunables
- `state.json` — runtime state (auto-created)
- `setup.sh` — one-command install

## Phone control (VPS)

The bot can't run on an iPhone (iOS suspends background apps), so the
supported setup is: bot runs 24/7 on a cheap VPS, your phone is the remote.

- `dashboard.py` — mobile web UI: live status, open positions with P&L,
  recent activity, Start / Stop / Kill-switch buttons.
  Auth is `dashboard_token` in config.json; the URL is the only login.
- `deploy.sh` — one-command VPS setup (Ubuntu): installs deps, generates the
  dashboard token, creates systemd services. The bot does NOT auto-start;
  the dashboard does.

Deploy:

    scp -r fomo-trader root@<vps-ip>:/root/
    ssh root@<vps-ip> "bash /root/fomo-trader/deploy.sh"

Open the dashboard URL it prints on your phone, then:
1. put your trading keypair at ~/.config/solana/id.json (or set keypair_path)
2. fund the wallet, 3. press Start in the dashboard (dry_run=true first).

Kill switch: stops the bot immediately and blocks restarts (even systemd
won't revive it) until you explicitly force-start from the dashboard.
