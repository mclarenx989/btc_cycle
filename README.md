# btc_cycle

A falsification harness for a dated market claim.

In December 2023 an anonymous post asserted that Bitcoin runs a fixed cycle —
1064 days from cycle low to all-time high, 364 days back down — and projected
the next ATH on **2025-10-06**. Bitcoin topped that exact day at ~$126,272.

Claims like that are usually argued about after the fact. This tracks it
instead: it registers each dated projection in a **write-once ledger before the
date arrives**, pulls daily exchange data, detects cycle pivots from raw price,
and grades every projection as it resolves. The point is not to find out
whether the pattern is real. The point is that it can no longer be
rationalised either way.

Its first finding was that the claim does not reproduce. Applying the post's
own stated rule to the real 2022 low gives 2025-10-20, two weeks late. The
famous hit was chained through a projected 2022-11-07 low **that never
happened**. Both facts are in the ledger: `ATL 2022-11-07 → +14d MISS`,
`ATH 2025-10-06 → +0d HIT`.

## What it does

- Pulls daily OHLC from two keyless public exchange APIs (Bitstamp, Coinbase)
- Detects cycle highs and lows from raw candles, with no lookahead
- Registers projections from three competing schedules, write-once
- Grades each as it resolves — including *provisional* and *unresolved* as
  outcomes distinct from "missed"
- Posts a daily digest and event alerts to Discord
- Watches two price thresholds continuously via GitHub Actions

## Design notes

Four decisions that carried most of the weight.

**Pivot detection is calibrated, not guessed.** A cycle high is a running-max
high left un-exceeded for `CONFIRM_DAYS`. Measured across 4,234 real candles,
the longest any *non*-pivot high stood was 237 days (2024-03-13); the shortest
true cycle top has stood 302. Only 238–302 separates them, so the constant is
bounded on **both** sides and both edges are pinned by tests — below 238 a
consolidation peak is wrongly promoted, above 302 the most recent top cannot be
confirmed at all.

**Every timestamp is UTC.** Exchange candles are stamped at UTC midnight, and
converting with a local-time call shifts every candle back one day in US
Eastern. That is invisible on a price chart and fatal to a tool whose entire
output is day counts. One helper does the conversion; a test pins three known
pivot epochs.

**A projection cannot be moved after the fact.** `projections` is write-once;
grades live in a separate table keyed to it. That separation is the whole
integrity guarantee — it is precisely the failure mode the source material got
away with.

**No real-time bottom detector, deliberately.** Two candidate signals were
measured against the three completed cycles and both failed: a low held **176
days** and still broke, and the 2013-12-18 *false* low recovered **160%** while
the real 2022 bottom managed **18.7%**. With n=3 nothing separates, so the tool
reports a calibrated confidence tier instead of a call it cannot support.

## Layout

```
btc_cycle.py     CLI, SQLite storage, orchestration
sources.py       exchange fetch, UTC normalisation
pivots.py        pivot detection, confidence tiers      [pure]
schedule.py      the three projection schedules         [pure]
score.py         grading: definitive / provisional / unresolved  [pure]
anticipate.py    countdown, historical analogue, divergence      [pure]
report.py        Discord embeds
ops/             launchd wrapper + templates, installer, cloud watcher
docs/            handoff doc — decisions, falsified theories, gotchas
```

The modules marked `[pure]` are functions over plain data: no network, no
database, no clock. The pattern logic is the part that must be correct, so it
is testable without touching an API.

## Running it

Python 3.9+, standard library only. Nothing to install.

```bash
python3 btc_cycle.py --backfill    # replay history, seed pivots + projections
python3 btc_cycle.py --verify      # score the claim's own arithmetic
python3 btc_cycle.py --window      # the pivot-window view
python3 btc_cycle.py --dry-run     # full run, prints instead of posting
python3 -m unittest test_btc_cycle # 43 tests
```

The backfill doubles as the test: it replays 5,330 candles from 2012 and must
independently rediscover all seven known cycle pivots on exact dates, or it
aborts rather than write a bad baseline.

To schedule it, put a Discord webhook in the keychain and run the installer:

```bash
security add-generic-password -a btc-cycle -s btc-cycle-discord -w 'https://discord.com/api/webhooks/...'
./ops/install.sh
```

## Honest limits

- **n=3.** Three completed cycles is the entire evidence base. Every historical
  comparison in the output carries its sample size for that reason.
- **Timing only.** The model says nothing about price.
- **Not investment advice**, and not built to be acted on.
- GitHub's scheduled workflows are best-effort — measured cadence for a
  `*/15` cron was roughly **6 hours**, not 15 minutes. The watcher reads the
  *day's* low rather than spot specifically so that delay costs latency and
  never a missed event.

## License

MIT — see [LICENSE](LICENSE).
