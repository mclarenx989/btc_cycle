# BTC Cycle Tracker — Design

Date: 2026-08-04
Status: approved

## Background

On 2023-12-12 an anonymous /biz/ poster (ID `SBC7H7La`, post No.56956841,
archived at <https://warosu.org/biz/thread/56956841>) claimed Bitcoin runs a
fixed cycle:

    ATL 2015 to ATH 2017 = 1064d
    ATH 2017 to ATL 2018 = 364d
    ATL 2018 to ATH 2021 = 1064d
    ATH 2021 to ATL 2022 = 364d

and projected the next ATH on **2025-10-06**. Bitcoin topped that exact day at
$126,296 (Coinbase). The call was made 664 days ahead.

### What the pattern actually is

The two constants are whole weeks: 1064d = 152.0 weeks, 364d = 52.0 weeks.
The full cycle is 204.0 weeks (1428d, 3.91 years) — slightly shorter than the
four-year halving interval.

### Where it is fudged

Measured against widely-cited pivot dates:

| Leg | Claimed | Actual | Delta |
|---|---|---|---|
| ATL 2015-01-14 -> ATH 2017-12-17 | 1064d | 1068d | +4 |
| ATH 2017-12-17 -> ATL 2018-12-15 | 364d | 363d | -1 |
| ATL 2018-12-15 -> ATH 2021-11-10 | 1064d | 1061d | -3 |
| ATH 2021-11-10 -> ATL 2022-11-21 | 364d | 376d | +12 |

Each leg is rounded to the nearest whole week. Three legs tolerate that; the
fourth does not.

Critically: applying the OP's own stated rule to the *real* 2022 low
(2022-11-21 + 1064d) yields **2025-10-20**, two weeks late. The 2025-10-06
figure comes from chaining projection to projection — anchoring on the
2021 daily-close ATH (2021-11-08), adding 364 to get a hypothetical
2022-11-07 low that never occurred, then adding 1064. The exact-day hit
rests on a pivot that was never realized.

### Why it is still worth tracking

ATH-to-ATH spacing is independently tight: 2017-12-17 -> 2021-11-10 = 1424d,
2021-11-10 -> 2025-10-06 = 1426d. Two days apart across eight years. That is a
stronger empirical formulation than the OP's own, and the tracker records it
alongside.

The pattern models timing only. It says nothing about price. Prior cycle
drawdowns ran 77-84%. This tool is pattern forensics, not trading guidance.

### Live state at time of writing

Day 302 of the projected 364-day down leg. Projected cycle low 2026-10-05,
62 days out. BTC last ~$63,526, roughly 50% off the high.

## Goals

1. Log BTC daily and show where price sits inside the current projected leg.
2. Hold an immutable ledger of projections and grade each one as it resolves,
   so the claim is falsifiable rather than a vibe.
3. Alert loudly when the pattern is confirmed or broken.

## Non-goals

- Price prediction. Timing only.
- Trading, signals, or position sizing.
- Any paid data source or API key.

## Architecture

New project at `~/btc_cycle`, mirroring the existing `~/kalshi` tracker:
stdlib-only Python (host runs 3.9.6 — no `X | None` union syntax), SQLite,
Discord webhook read from the macOS keychain by an `ops/run_*.sh` wrapper,
scheduled by launchd. Nothing to pip install.

### Modules

| File | Responsibility | Depends on |
|---|---|---|
| `sources.py` | Fetch daily OHLC candles from either source, normalized to one shape | stdlib only |
| `pivots.py` | Detect realized ATH/ATL pivots from a candle series | nothing |
| `schedule.py` | Emit projected pivots per schedule, with tolerance band | nothing |
| `score.py` | Grade a projection once its window closes | nothing |
| `report.py` | Build and post Discord embeds | stdlib only |
| `btc_cycle.py` | CLI and orchestration | all of the above |

`pivots.py`, `schedule.py` and `score.py` are pure functions over plain data:
no network, no database, no clock. They take a series or a list of records and
return records. This keeps the pattern logic — the part that must be correct —
testable without touching an API or a DB.

### Data sources

Both keyless and public.

- **Bitstamp** — `GET /api/v2/ohlc/btcusd/?step=86400&limit=1000&start=<unix>`,
  paginated. Deep history to 2011. Used for backfill. Verified: returns
  2015-01-14 low of $152.40.
- **Coinbase Exchange** — `GET /products/BTC-USD/candles?granularity=86400`,
  max 300 candles per request. Used for the daily tick and as a cross-check,
  because it is the exchange the $126k headlines quote. Verified: returns
  2025-10-06 high of $126,296.

Coinbase is primary for recent data, Bitstamp is the fallback.

### Pivot detection

"A candle whose high exceeds every prior high" is not sufficient — that marks
every day of a run-up as an ATH. A cycle ATH is a running-max high that then
stands un-exceeded for a long time.

- A **cycle ATH** is a running-max high not exceeded for at least
  `CONFIRM_DAYS`.
- A **cycle ATL** is the minimum low between two consecutive *confirmed*
  cycle ATHs.

`CONFIRM_DAYS = 270`, calibrated against the real series rather than guessed.
Measuring how long each running-max high stood un-exceeded (Bitstamp daily,
2015-01-01 to 2026-08-03, 4,234 candles):

| Running-max high | Un-exceeded for | Is it a cycle ATH? |
|---|---|---|
| 2017-12-17 $19,666 | 1079d | yes |
| 2021-11-10 $69,000 | 846d | yes |
| 2025-10-06 $126,272 | 302d and counting | yes |
| 2024-03-13 $73,794 | 237d | **no — nearest false positive** |
| 2015-11-03 $502 | 206d | no |
| 2021-04-14 $64,895 | 189d | no |

Any `CONFIRM_DAYS` in 238-302 separates the three true pivots from every false
positive. 270 sits in the middle of that band. Note the constraint is
two-sided: below 238 the March 2024 consolidation peak is wrongly promoted;
above 302 the 2025 ATH is not yet confirmable today. The test suite must
assert both edges so a future tweak cannot silently break either.

The current, unconfirmed low is tracked separately as a running minimum since
the last confirmed ATH, so the tracker can report "lowest so far" without
claiming a pivot that history has not yet sealed.

### Time zone

**All timestamp-to-date conversion is UTC.** Exchange candle timestamps are
UTC midnight; converting with a local-time call shifts every candle back one
day in US Eastern, which silently moves every pivot. This was observed during
design: `date.fromtimestamp` produced 2017-12-16 / 2021-11-09 / 2025-10-05 —
each exactly one day early — while `utcfromtimestamp` on the same payload
produced the correct 2015-01-14 $152.40 and 2025-10-06 $126,296.

A one-day systematic offset is invisible in a price chart and fatal to a tool
whose entire output is day counts. The test suite must pin at least one known
pivot date to catch a regression here.

### Schedules

Three, stored as rows in the same table, differing only in `basis`.

| Schedule | Rule | Next projections |
|---|---|---|
| `oracle` | The OP's literal chain from the 2021-11-08 daily-close anchor, +364/+1064 forever | 2026-10-05 (ATL), 2029-09-03 (ATH) |
| `derived` | Same 152w/52w rule applied to *realized* pivots | 2026-10-05 (ATL) |
| `ath_to_ath` | 1425d ATH-to-ATH spacing | 2029-08-31 (ATH) |

`oracle` and `derived` re-converged after 2025, because the oracle's projected
ATH became the realized one. They diverge again only if the low misses
2026-10-05.

Tolerance band is ±3 days. `days_off` is always recorded exactly, so the band
is only a convenience boolean, not a truth claim — a wider or narrower band can
be applied to stored scores at any time without a re-run.

Under ±3, the OP's own four historical legs score 2 pass / 2 fail (-1 and -3
pass; +4 and +12 fail). That is the intended, honest result: the band is set
from what a whole-week rounding rule should tolerate, not fitted backwards to
make the source material look good. `--verify` prints this scoreline.

### Schema

```sql
candles(date TEXT PRIMARY KEY, open REAL, high REAL, low REAL, close REAL,
        source TEXT, fetched_at TEXT)

pivots(date TEXT, kind TEXT,            -- 'ath' | 'atl'
       price REAL, source TEXT, detected_at TEXT,
       PRIMARY KEY (date, kind))

projections(id INTEGER PRIMARY KEY, schedule TEXT, kind TEXT,
            projected_date TEXT, tolerance_days INTEGER,
            basis TEXT, created_at TEXT)

scores(projection_id INTEGER PRIMARY KEY REFERENCES projections(id),
       realized_date TEXT, days_off INTEGER,
       within_tolerance INTEGER, scored_at TEXT)

snapshots(date TEXT PRIMARY KEY, price REAL, leg_kind TEXT,
          days_into_leg INTEGER, days_remaining INTEGER,
          drawdown_pct REAL)
```

`projections` rows are written once and never updated. Grades live in a
separate table keyed to them. A projection cannot be silently moved after the
fact — that constraint is the whole point of the ledger.

### Daily flow

1. Fetch yesterday's candle (Coinbase, falling back to Bitstamp).
2. Upsert into `candles`. Idempotent — a same-day re-run cannot double-count.
3. Re-run pivot detection over the tail of the series; insert any new pivot.
4. Compute and store today's `snapshots` row.
5. Grade any projection whose tolerance window has closed; insert into `scores`.
6. Post to Discord.

### Output

One routine daily embed: current leg, days into and remaining, price, distance
from the $126,296 ATH, drawdown against the 77-84% prior-cycle range, and the
countdown to the next projected pivot.

Separate loud alerts, posted as their own embed:

- New ATH printed (the down leg is invalidated)
- New cycle low printed below the prior running minimum
- Projection window opening (T-7 days)
- Projection resolved, with its grade

### Error handling

- Missing keychain webhook is a hard failure. Discord is the only output, so a
  silent no-op would be indistinguishable from a healthy quiet day. Matches the
  Kalshi wrapper's behaviour.
- API failure raises `SystemExit` with a readable message including status and
  a truncated body, matching `kalshi_snapshot.py`.
- Coinbase failure falls back to Bitstamp before giving up.
- A gap in the candle series is logged and skipped, not interpolated. Inventing
  a price could invent a pivot.

### Testing

The backfill is the primary test. On first run the tracker replays 2015 to
today and must independently rediscover, from raw candles alone:

| Date | Kind | Expected |
|---|---|---|
| 2015-01-14 | ATL | ~$152 (Bitstamp $152.40) |
| 2017-12-17 | ATH | ~$19,700 (Bitstamp $19,666) |
| 2018-12-15 | ATL | ~$3,122 |
| 2021-11-10 | ATH | ~$69,000 |
| 2022-11-21 | ATL | ~$15,476 |
| 2025-10-06 | ATH | ~$126,300 (Bitstamp $126,272, Coinbase $126,296) |

and reproduce leg lengths 1068 / 363 / 1061 / 376. If `pivots.py` cannot
recover those, it is wrong and the run aborts rather than writing a bad
baseline.

Prices differ by a few hundred dollars between exchanges and are asserted
loosely. **Dates are asserted exactly** — the whole tool is day counts, and
the design-time time-zone bug above proves a date assertion is what catches
that class of error. The run records which source produced each pivot.

`--verify` prints the realized leg table beside the OP's claimed
1064/364/1064/364, keeping the fudge visible inside the tool itself.

Unit tests cover the pure modules with synthetic series: a known ATH/ATL
sequence through `pivots.py`, a known anchor through `schedule.py`, and
on-time / late / outside-tolerance cases through `score.py`.

### CLI

```
python3 btc_cycle.py --backfill    # replay history, seed pivots and projections
python3 btc_cycle.py --verify      # print realized legs vs claimed
python3 btc_cycle.py --dry-run     # full run, no Discord post
python3 btc_cycle.py               # daily run, posts to Discord
```

### Scheduling

`ops/run_btc_cycle.sh` pulls the webhook from keychain service
`btc-cycle-discord` (its own entry, separate from `kalshi-snapshot-discord`, so
it can point at a different channel), then execs the script.
`ops/com.btccycle.snapshot.plist` runs it daily. No jitter — this hits public
exchange APIs where a predictable timestamp makes the series cleaner, same
reasoning as the Kalshi plist.

One-time manual setup, run by the user:

```
security add-generic-password -a btc-cycle -s btc-cycle-discord -w 'https://discord.com/api/webhooks/...'
```

## Open questions

None blocking. The `ath_to_ath` schedule is optional scope and can be dropped
without touching any code path — it is one row in `projections`.
