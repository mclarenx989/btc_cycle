#!/usr/bin/env python3
"""BTC cycle tracker -> SQLite -> Discord.

Tracks the /biz/ post No.56956841 (2023-12-12) claim that Bitcoin runs a fixed
1064d ATL->ATH / 364d ATH->ATL cycle - the post that projected the 2025-10-06
top 664 days ahead, and hit the exact day.

Keeps an immutable ledger of projections and grades each one as it resolves,
so the claim stays falsifiable instead of becoming folklore.

    python3 btc_cycle.py --backfill   # replay history, seed pivots+projections
    python3 btc_cycle.py --verify     # realized legs vs the post's claims
    python3 btc_cycle.py --dry-run    # full run, print instead of posting
    python3 btc_cycle.py              # daily run, posts to Discord

Stdlib only. Python 3.9 compatible - no X | None unions.
"""

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

import pivots as pv
import report
import schedule as sch
import score as sc
import sources

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "btc_cycle.db")
# Start before the 2013 top. Beginning the series at 2015 made its first
# candle look like a cycle ATH - it had no prior high to be measured against,
# so it trivially "stood un-exceeded". Anchoring on the real 2013-11-30 peak
# removes that artifact and adds a fourth completed cycle to the record.
HISTORY_START = date(2012, 1, 1)

# The backfill must independently rediscover these from raw candles. Prices
# vary by a few hundred dollars between exchanges and are checked loosely;
# DATES are checked exactly, because the whole tool is day counts and a
# one-day timezone slip is otherwise invisible.
EXPECTED_PIVOTS = [
    (date(2013, 11, 30), "ath", 1_000, 1_300),
    (date(2015, 1, 14), "atl", 140, 200),
    (date(2017, 12, 17), "ath", 19_000, 21_000),
    (date(2018, 12, 15), "atl", 2_800, 3_500),
    (date(2021, 11, 10), "ath", 66_000, 70_000),
    (date(2022, 11, 21), "atl", 15_000, 16_500),
    (date(2025, 10, 6), "ath", 124_000, 128_000),
]


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    date TEXT PRIMARY KEY, open REAL, high REAL, low REAL, close REAL,
    source TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS pivots (
    date TEXT, kind TEXT, price REAL, source TEXT, detected_at TEXT,
    PRIMARY KEY (date, kind));
CREATE TABLE IF NOT EXISTS projections (
    id INTEGER PRIMARY KEY AUTOINCREMENT, schedule TEXT, kind TEXT,
    projected_date TEXT, tolerance_days INTEGER, basis TEXT, created_at TEXT,
    UNIQUE (schedule, kind, projected_date));
CREATE TABLE IF NOT EXISTS scores (
    projection_id INTEGER PRIMARY KEY REFERENCES projections(id),
    realized_date TEXT, days_off INTEGER, within_tolerance INTEGER,
    scored_at TEXT);
CREATE TABLE IF NOT EXISTS snapshots (
    date TEXT PRIMARY KEY, price REAL, leg_kind TEXT, days_into_leg INTEGER,
    days_remaining INTEGER, drawdown_pct REAL);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# How far ahead of a projected pivot the dedicated window view switches on.
WINDOW_LEAD_DAYS = 90

# A --dry-run must not mutate the ledger. Grading is a one-shot event - once a
# projection is scored it never alerts again - so a preview that quietly
# committed its grades would burn the real notification.
DRY_RUN = False


def commit(conn):
    if DRY_RUN:
        return
    conn.commit()


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(scores)")}
    if "provisional" not in cols:
        conn.execute("ALTER TABLE scores ADD COLUMN provisional INTEGER DEFAULT 0")
        conn.commit()
    return conn


def save_candles(conn, rows):
    conn.executemany(
        "INSERT INTO candles (date,open,high,low,close,source,fetched_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
        "high=excluded.high, low=excluded.low, close=excluded.close",
        [(c["date"].isoformat(), c["open"], c["high"], c["low"], c["close"],
          c["source"], now_iso()) for c in rows])
    commit(conn)


def load_candles(conn):
    return [{"date": date.fromisoformat(r["date"]), "open": r["open"],
             "high": r["high"], "low": r["low"], "close": r["close"],
             "source": r["source"]}
            for r in conn.execute("SELECT * FROM candles ORDER BY date")]


def save_pivots(conn, found):
    """Returns pivots that are new to the database."""
    known = {(r["date"], r["kind"]) for r in conn.execute("SELECT date,kind FROM pivots")}
    fresh = [p for p in found if (p["date"].isoformat(), p["kind"]) not in known]
    conn.executemany(
        "INSERT OR IGNORE INTO pivots (date,kind,price,source,detected_at) VALUES (?,?,?,?,?)",
        [(p["date"].isoformat(), p["kind"], p["price"], p["source"], now_iso())
         for p in found])
    commit(conn)
    return fresh


def load_pivots(conn):
    return [{"date": date.fromisoformat(r["date"]), "kind": r["kind"],
             "price": r["price"], "source": r["source"]}
            for r in conn.execute("SELECT * FROM pivots ORDER BY date")]


def save_projections(conn, rows):
    """Write-once. An existing row is never updated - that is the ledger."""
    conn.executemany(
        "INSERT OR IGNORE INTO projections "
        "(schedule,kind,projected_date,tolerance_days,basis,created_at) VALUES (?,?,?,?,?,?)",
        [(r["schedule"], r["kind"], r["date"].isoformat(), sch.TOLERANCE_DAYS,
          r["basis"], now_iso()) for r in rows])
    commit(conn)


# ---------------------------------------------------------------------------
# run modes
# ---------------------------------------------------------------------------

def backfill(conn):
    today = datetime.now(timezone.utc).date()
    print("Backfilling %s -> %s from Bitstamp ..." % (HISTORY_START, today))
    rows = sources.fetch_bitstamp(HISTORY_START, today)
    print("  %d candles  (%s -> %s)" % (len(rows), rows[0]["date"], rows[-1]["date"]))
    save_candles(conn, rows)

    found = pv.detect(rows)
    print("\nDetected %d cycle pivots (CONFIRM_DAYS=%d):" % (len(found), pv.CONFIRM_DAYS))
    for p in found:
        print("   %s  %s  %s" % (p["date"], p["kind"].upper(), report._money(p["price"])))

    verify_pivots(found)
    save_pivots(conn, found)
    save_projections(conn, sch.all_projections(found))
    print("\nSeeded projections:")
    for r in conn.execute("SELECT schedule,kind,projected_date FROM projections "
                          "WHERE projected_date >= ? ORDER BY projected_date",
                          (today.isoformat(),)):
        print("   %-11s %s  %s" % (r["schedule"], r["kind"].upper(), r["projected_date"]))
    return found


def verify_pivots(found):
    """The backfill IS the test. Abort rather than write a bad baseline."""
    got = {(p["date"], p["kind"]): p["price"] for p in found}
    problems = []
    for d, kind, lo, hi in EXPECTED_PIVOTS:
        if (d, kind) not in got:
            near = [k for k in got if k[1] == kind and abs((k[0] - d).days) <= 3]
            problems.append("missing %s %s%s" % (
                kind.upper(), d, (" (nearest: %s)" % near[0][0]) if near else ""))
        elif not (lo <= got[(d, kind)] <= hi):
            problems.append("%s %s price %s outside %s-%s"
                            % (kind.upper(), d, got[(d, kind)], lo, hi))
    if problems:
        raise SystemExit("Pivot detection failed its baseline:\n  " + "\n  ".join(problems))
    print("\n  baseline OK - all %d known pivots rediscovered from raw candles"
          % len(EXPECTED_PIVOTS))


def verify(conn):
    ps = load_pivots(conn) or backfill(conn)
    realized = pv.legs(ps)
    print("\nRealized legs vs the post's claimed numbers")
    print("-" * 68)
    print("%-24s %8s %8s %8s   %s" % ("leg", "claimed", "actual", "delta", "within +/-3d"))
    rows = sc.score_claimed_legs(realized, sch.CLAIMED_LEGS)
    for r in rows:
        if r["actual"] is None:
            print("%-24s %8d %8s %8s   NO DATA" % (r["label"], r["claimed"], "-", "-"))
            continue
        print("%-24s %8d %8d %+8d   %s"
              % (r["label"], r["claimed"], r["actual"], r["delta"],
                 "PASS" if r["within_tolerance"] else "FAIL"))
    hits = sum(1 for r in rows if r["within_tolerance"])
    print("-" * 68)
    print("post's own arithmetic: %d/%d legs within +/-3 days" % (hits, len(rows)))
    print("\nAll realized legs:")
    for leg in realized:
        print("   %s %s -> %s %s = %4dd (%.1f wk)"
              % (leg["from"]["kind"].upper(), leg["from"]["date"],
                 leg["to"]["kind"].upper(), leg["to"]["date"],
                 leg["days"], leg["days"] / 7.0))
    aths = [p for p in ps if p["kind"] == "ath"]
    print("\nATH -> ATH spacing (the tighter formulation):")
    for a, b in zip(aths, aths[1:]):
        print("   %s -> %s = %dd" % (a["date"], b["date"], (b["date"] - a["date"]).days))


def build_state(conn, series, ps, today):
    leg = sch.current_leg(ps, today)
    price = series[-1]["close"]
    low = pv.running_low_since(series, ps[-1]["date"]) if ps else None

    scored = conn.execute("SELECT COUNT(*) c FROM scores").fetchone()["c"]
    hits = conn.execute("SELECT COUNT(*) c FROM scores WHERE within_tolerance=1").fetchone()["c"]

    # Only compare schedules on the SAME upcoming pivot. Widening this to
    # "the next N projections of this kind" pulls in the pivot after next,
    # which is a whole cycle away and always reads as a disagreement.
    upcoming = conn.execute(
        "SELECT schedule, projected_date FROM projections WHERE kind=? AND projected_date>=? "
        "ORDER BY projected_date", (leg["to_kind"], today.isoformat())).fetchall()
    if upcoming:
        first = date.fromisoformat(upcoming[0]["projected_date"])
        near = [r for r in upcoming
                if (date.fromisoformat(r["projected_date"]) - first).days <= 180]
        dates = sorted({r["projected_date"] for r in near})
        names = sorted({r["schedule"] for r in near})
        if len(dates) == 1:
            agreement = "✅  %s\nagree on `%s`" % (" + ".join(names), dates[0])
        else:
            agreement = "⚡  split\n" + "\n".join(
                "`%s`  %s" % (r["projected_date"], r["schedule"]) for r in near)
    else:
        agreement = "n/a"

    prov = pv.provisional_atl(series, ps[-1]["date"], today) if ps else None
    new_lows = pv.new_lows_since(series, ps[-1]["date"]) if ps else []

    return {"leg": leg, "price": price, "pivots": ps, "running_low": low,
            "scored": scored, "hits": hits, "agreement": agreement, "today": today,
            "provisional": prov, "new_low_count": len(new_lows),
            "new_lows": new_lows, "tolerance": sch.TOLERANCE_DAYS}


def resolve_projections(conn, ps, today):
    """Grade every projection whose window has closed and is not yet scored."""
    alerts = []
    rows = conn.execute(
        "SELECT p.* FROM projections p LEFT JOIN scores s ON s.projection_id=p.id "
        "WHERE s.projection_id IS NULL").fetchall()
    for r in rows:
        proj = {"date": date.fromisoformat(r["projected_date"]), "kind": r["kind"]}
        if not sc.resolvable(proj, ps, today, r["tolerance_days"]):
            continue
        g = sc.grade(proj, ps, r["tolerance_days"])
        if g is None:
            continue
        # Name the columns. A positional VALUES list silently breaks the first
        # time a migration adds one - and the next projection to resolve is the
        # 2026-10-05 ATL, so that break would have landed in the window this
        # tool exists to watch.
        conn.execute(
            "INSERT OR REPLACE INTO scores "
            "(projection_id, realized_date, days_off, within_tolerance, scored_at, provisional) "
            "VALUES (?,?,?,?,?,?)",
            (r["id"], g["realized_date"].isoformat(), g["days_off"],
             1 if g["within_tolerance"] else 0, now_iso(), 0))
        alerts.append(report.alert_embed(
            "hit" if g["within_tolerance"] else "miss",
            "%s %s resolved  ·  %s"
            % (r["kind"].upper(), r["projected_date"], r["schedule"]),
            ["Projected  ·  `%s`" % r["projected_date"],
             "Realized   ·  `%s`" % g["realized_date"].isoformat(),
             "",
             "**%+d days**  →  **%s**"
             % (g["days_off"], "HIT, within ±%dd" % r["tolerance_days"]
                if g["within_tolerance"] else "MISS")]))
    commit(conn)
    return alerts


def window(conn):
    """The ATL-window view on demand, plus the historical context that says
    how much a standing low is actually worth."""
    today = datetime.now(timezone.utc).date()
    series = load_candles(conn)
    ps = load_pivots(conn)
    if not series or not ps:
        raise SystemExit("No data. Run --backfill first.")
    state = build_state(conn, series, ps, today)
    print(report.render_text([report.window_embed(state)]))

    print("\nHow long lows have stood before breaking, by completed down leg:")
    for a, b in zip(ps, ps[1:]):
        if not (a["kind"] == "ath" and b["kind"] == "atl"):
            continue
        leg_rows = [c for c in series if a["date"] < c["date"] <= b["date"]]
        marks = pv.new_lows_since(leg_rows, a["date"] - timedelta(days=1))
        stands = [(marks[i + 1]["date"] - marks[i]["date"]).days
                  for i in range(len(marks) - 1)]
        print("   %s -> %s: %2d new lows, longest false stand %3dd"
              % (a["date"], b["date"], len(marks), max(stands) if stands else 0))
    print("\n   worst on record: a low held %dd and still broke (2014-04-11)."
          % pv.FALSE_LOW_MAX_STAND)


def daily(conn, dry_run):
    today = datetime.now(timezone.utc).date()
    series = load_candles(conn)
    if not series:
        backfill(conn)
        series = load_candles(conn)

    recent = sources.fetch_daily(today - timedelta(days=7), today)
    if recent:
        save_candles(conn, recent)
        series = load_candles(conn)

    found = pv.detect(series)
    fresh = save_pivots(conn, found)
    ps = load_pivots(conn)
    save_projections(conn, sch.all_projections(ps))

    embeds = []
    for p in fresh:
        kind = "new_ath" if p["kind"] == "ath" else "new_low"
        embeds.append(report.alert_embed(
            kind, "NEW CONFIRMED %s - %s" % (p["kind"].upper(), p["date"]),
            ["**%s**" % report._money(p["price"]),
             "",
             "The current leg is now re-anchored on this pivot."]))

    embeds += resolve_projections(conn, ps, today)

    state = build_state(conn, series, ps, today)
    leg = state["leg"]

    # A new leg low is the one bottom-related event that is knowable the day it
    # happens. Inside the tolerance band it is the whole ballgame, so it gets
    # its own loud alert rather than a line in the daily embed.
    band_lo = leg["target_date"] - timedelta(days=sch.TOLERANCE_DAYS)
    band_hi = leg["target_date"] + timedelta(days=sch.TOLERANCE_DAYS)
    for c in state["new_lows"]:
        if c["date"] != today:
            continue
        inside = band_lo <= c["date"] <= band_hi
        embeds.append(report.alert_embed(
            "in_band" if inside else "new_low",
            "%sNew leg low  ·  %s" % ("IN BAND  —  " if inside else "", c["date"]),
            ["**%s**  ·  %s off the %s ATH"
             % (report._money(c["low"]),
                report._pct((c["low"] / leg["from"]["price"] - 1.0) * 100.0),
                leg["from"]["date"].isoformat()),
             "",
             ("Leg low **#%d**  ·  **inside the ±%dd band** around `%s`"
              % (state["new_low_count"], sch.TOLERANCE_DAYS,
                 leg["target_date"].isoformat())) if inside
             else "Leg low **#%d**  ·  T-minus **%d days** to `%s`"
             % (state["new_low_count"], leg["remaining"],
                leg["target_date"].isoformat())]))

    if leg["to_kind"] == "atl" and leg["remaining"] <= WINDOW_LEAD_DAYS and state["provisional"]:
        embeds.append(report.window_embed(state))

    embeds.append(report.daily_embed(state))

    conn.execute(
        "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?)",
        (today.isoformat(), state["price"], leg["to_kind"], leg["elapsed"],
         leg["remaining"], (state["price"] / leg["from"]["price"] - 1.0) * 100.0))
    commit(conn)

    if dry_run:
        conn.rollback()
        print(report.render_text(embeds))
        print("\n[dry run - nothing posted, no rows committed]")
        return
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not hook:
        raise SystemExit("No DISCORD_WEBHOOK_URL. Discord is the only output, so "
                         "this is a hard failure rather than a silent no-op.")
    report.post(hook, embeds)
    print("posted %d embed(s)" % len(embeds))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--window", action="store_true",
                    help="ATL-window view only, no post")
    args = ap.parse_args()
    global DRY_RUN
    DRY_RUN = args.dry_run
    conn = connect()
    if args.backfill:
        backfill(conn)
    elif args.verify:
        verify(conn)
    elif args.window:
        window(conn)
    else:
        daily(conn, args.dry_run)


if __name__ == "__main__":
    main()
