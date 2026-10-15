# Data sources

Every source is public and read-only. No API keys, logins or paid feeds are
used. Data is used for personal/research analysis only; see
[../DISCLAIMER.md](../DISCLAIMER.md).

## Sources in use

| Source | Endpoints | Auth | Cadence in this tool | Used for |
| --- | --- | --- | --- | --- |
| Kalshi market listing | `GET /trade-api/v2/markets?status=open&mve_filter=exclude` | none | full sweep every run (about 131k markets in 20–30s) | best asks and sizes for every screen; close times; event tickers |
| Kalshi orderbooks | `GET /markets/{ticker}/orderbook?depth=1` | none | candidates only, at most a few hundred per run | confirm prices and top-of-book depth; verify-before-alert |
| Kalshi events / series | `GET /events/{event}`, `GET /series/{series}` | none | candidates only; fee multipliers cached 24h | exhaustiveness proof for event books; series fee multiplier |
| Polymarket gamma | `gamma-api.polymarket.com/events` | none | every run, top 3,000 events by 24h volume | cross-venue matching (Type C); fee schedule per market |
| Polymarket CLOB | `clob.polymarket.com/book` | none | matched candidates only | confirm the Polymarket leg's ask and depth; the fair value of `xvenue_poly` picker legs |
| Kraken | public `Trades` and 1-minute `OHLC` | none | only pairs with a live, takeable 15-minute window; two calls per pair | Tier 3 spot and volatility; the fair value of `kraken_model` picker legs |

Notes:

- **Orderbook asks.** Kalshi orderbooks list bids only, so the YES ask is
  `100 − best NO bid`. An empty book is not fillable.
- **Kraken spot freshness.** Kraken's `Trades` endpoint returns at most 1000
  trades, so a busy pair's last trade can be minutes old. Spot is the newer
  of the last trade and the latest 1-minute candle close, and any spot older
  than 180s is refused.
- **Cross-venue matching.** Matching uses normalized titles with a word-set
  Jaccard score of at least 0.5, plus guards: numbers must match, the Kalshi
  outcome name must appear on the Polymarket side, there must be no one-sided
  qualifiers, and the two expiries must be within 2 days. The number guard
  treats ordinals ("4th", "3rd") as numbers so placement mismatches
  ("finish 4th" vs "3rd place") are rejected. The outcome-name guard
  compares accent-folded text (NFKD + strip combining marks) because the
  venues can spell the same name in different Unicode forms (precomposed
  vs combining acute); the Jaccard score itself is unchanged. Resolution
  rules can still differ across venues.

## Leg fair values used by the combo picker

Only two leg types have a fair value the picker accepts:

- **`kraken_model`:** live Kalshi 15-minute crypto windows, priced by a
  zero-drift Brownian model against Kraken spot. The model has a 3-minute
  warmup, and it only takes a side when the market already leans that way.
- **`xvenue_poly`:** a Kalshi market matched to a Polymarket market. The fair
  value of the Kalshi side is `100 − the Polymarket opposite-side ask`.

## NO-EDGE list (excluded from picks)

Legs with no defensible, independent fair value are excluded. They never
enter the picker's pool:

1. **Sports spreads, totals and player props.** No validated margin or
   totals distribution model exists.
2. **Sports line movement and opening-line edges.** No public, keyless
   historical odds feed exists.
3. **Multi-book consensus odds.** The aggregators are keyed or paid.
4. **Fed-decision legs.** There's no verified keyless fed-funds feed;
   cross-venue only.
5. **Macro prints** (payrolls, GDP, retail sales, jobless claims). There's
   no public keyless nowcast; cross-venue only.
6. **Treasury-yield threshold legs.** A data feed exists, but no probability
   model has been built.
7. **Non-election politics** (cabinet, legislation, nominations). There are
   no polls; cross-venue only.
8. **Culture and entertainment legs without a Polymarket match.** No fair
   value is available anywhere.
9. **Any source that needs a token or login.** These are excluded by rule.
10. **Any leg whose only "fair" is Kalshi's own price history.** That's
    circular, not independent.

"Cross-venue only" means the leg can enter the pool only through a
confirmed Polymarket match (`xvenue_poly`). Otherwise it is excluded.

Candidate future sources include sportsbook moneylines, polling averages,
commodity futures and inflation nowcasts. Each would need its own
probability model, validated out of sample against settlement, before its
legs could enter the pool. None is wired today.
