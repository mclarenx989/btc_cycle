#!/usr/bin/env python3
"""BTC cycle tracker -> SQLite -> Discord.

Tracks the /biz/ post No.56956841 (2023-12-12) claim that Bitcoin runs a fixed
1064d ATL->ATH / 364d ATH->ATL cycle - the post that projected the 2025-10-06
top 664 days ahead, and hit the exact day.

Keeps an immutable ledger of projections and grades each one as it resolves,
so the claim stays falsifiable instead of becoming folklore.

    python3 btc_cycle.py --backfill   # replay history, seed pivots+projections
    python3 btc_cycle.py --verify     # realized legs vs the post's claims
    python3 btc_cycle.py --window     # ATL-window view, no post
    python3 btc_cycle.py --dry-run    # full run, print instead of posting
    python3 btc_cycle.py --healthcheck  # alert if the daily run is overdue
    python3 btc_cycle.py              # daily run, posts to Discord

Stdlib only. Python 3.9 compatible - no X | None unions.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

import anticipate as ant
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
CREATE TABLE IF NOT EXISTS pending_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, queued_at TEXT);
CREATE TABLE IF NOT EXISTS alerts_sent (
    key TEXT PRIMARY KEY, sent_at TEXT);
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

# A run is overdue after this many days. Two, not one: the job is daily, so a
# single miss is a transient network blip that the 7-day trailing refetch
# already heals. Two consecutive misses means something is actually wrong.
STALE_DAYS = 2


def commit(conn):
    if DRY_RUN:
        return
    conn.commit()


def staleness(last_run, today, max_age=STALE_DAYS):
    """Pure. How long since the last successful run, and is that a problem."""
    if last_run is None:
        return {"age_days": None, "stale": True,
                "reason": "no successful run has ever been recorded"}
    age = (today - last_run).days
    return {"age_days": age, "stale": age > max_age,
            "reason": "last successful run %s, %d day%s ago"
                      % (last_run.isoformat(), age, "" if age == 1 else "s")}


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(scores)")}
    if "provisional" not in cols:
        conn.execute("ALTER TABLE scores ADD COLUMN provisional INTEGER DEFAULT 0")
        conn.commit()
    return conn


def queue_post(conn, embeds):
    """Park embeds that could not be delivered.

    Discord is the only output, so a failed post used to mean the alert simply
    never existed - the watchdog correctly detected the 2026-08-25..29 outage
    on three separate days and every one of those alerts died on the same DNS
    failure that caused the outage. Queued embeds are flushed by the next run
    that gets through."""
    conn.execute("INSERT INTO pending_posts (payload,queued_at) VALUES (?,?)",
                 (json.dumps(embeds), now_iso()))
    commit(conn)


def flush_pending(conn, hook):
    """Deliver anything queued while the network was down. Oldest first.

    Stops at the first failure and leaves the rest queued - if delivery is
    still broken, draining the queue into the void would lose it for good.
    """
    rows = conn.execute(
        "SELECT id,payload,queued_at FROM pending_posts ORDER BY id").fetchall()
    sent = 0
    for r in rows:
        try:
            embeds = json.loads(r["payload"])
        except ValueError:
            conn.execute("DELETE FROM pending_posts WHERE id=?", (r["id"],))
            continue
        for e in embeds:
            e["title"] = "[delayed] " + e.get("title", "")
        try:
            report.post(hook, embeds)
        except report.PostFailed:
            break
        conn.execute("DELETE FROM pending_posts WHERE id=?", (r["id"],))
        sent += 1
    commit(conn)
    return sent


def deliver(conn, hook, embeds):
    """Post, or queue for the next run that can. Returns True if delivered."""
    try:
        report.post(hook, embeds)
        return True
    except report.PostFailed as exc:
        queue_post(conn, embeds)
        print("queued %d embed(s) for later: %s" % (len(embeds), exc), file=sys.stderr)
        return False


def alert_sent(conn, key):
    return conn.execute("SELECT 1 FROM alerts_sent WHERE key=?", (key,)).fetchone() is not None


def mark_alert_sent(conn, key):
    """One-shot bookkeeping. Without this the daily run re-fires whichever
    countdown tier it currently sits inside, every single day."""
    conn.execute("INSERT OR IGNORE INTO alerts_sent (key,sent_at) VALUES (?,?)",
                 (key, now_iso()))
    commit(conn)


def sent_milestones(conn, target):
    out = set()
    for r in conn.execute("SELECT key FROM alerts_sent WHERE key LIKE ?",
                          ("milestone:%s:%%" % target.isoformat(),)):
        try:
            out.add(int(r["key"].rsplit(":", 1)[1]))
        except (ValueError, IndexError):
            continue
    return out


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

    prov = pv.provisional_atl(series, ps[-1]["date"], today) if ps else None
    new_lows = pv.new_lows_since(series, ps[-1]["date"]) if ps else []
    band_lo = leg["target_date"] - timedelta(days=sch.TOLERANCE_DAYS)
    band_hi = leg["target_date"] + timedelta(days=sch.TOLERANCE_DAYS)

    ana = ant.analogue(series, ps, leg["elapsed"], price,
                       running_low=prov["price"] if prov else None)
    if low is not None:
        ana["current_above_running_low"] = (price / low["low"] - 1.0) * 100.0
    div = ant.divergence(series, ps, new_lows, today)
    must = ant.must_happen(band_lo, band_hi, prov, leg["remaining"],
                           ended=leg["ended"]) if prov else ""
    inval = ant.invalidations(leg["from"], price, prov, band_hi, today, new_lows)

    # Once the leg has ended, the next pivot is no longer of `to_kind` - the
    # old code kept hunting for the next ATL and surfaced one four years out,
    # skipping the 2029 ATH entirely. Take the earliest upcoming projection of
    # ANY kind instead.
    kind_filter = "" if leg["ended"] else " AND kind = ?"
    params = [today.isoformat()] if leg["ended"] else [today.isoformat(), leg["to_kind"]]
    upcoming = conn.execute(
        "SELECT schedule, kind, projected_date, basis FROM projections "
        "WHERE projected_date >= ?" + kind_filter +
        " ORDER BY projected_date", params).fetchall()

    next_proj = None
    if upcoming:
        # The ledger is write-once, so a schedule that re-anchors leaves its
        # older projection in place. Both stay registered and both get graded,
        # but the display should show each schedule's CURRENT call - otherwise
        # `derived` appears twice with two different dates.
        # Per schedule: keep only its most recent BASIS (a re-anchor creates a
        # fresh batch), then within that batch take the SOONEST upcoming date.
        # Taking the last row created instead picks the furthest-out step of
        # the chain - which surfaced 2033 dates as "next pivot".
        cand = {}
        for r in conn.execute(
                "SELECT schedule, kind, projected_date, basis, created_at "
                "FROM projections WHERE projected_date >= ?", [today.isoformat()]):
            if not (leg["ended"] or r["kind"] == leg["to_kind"]):
                continue
            cur = cand.get(r["schedule"])
            if (cur is None
                    or r["created_at"] > cur["created_at"]
                    or (r["created_at"] == cur["created_at"]
                        and r["projected_date"] < cur["projected_date"])):
                cand[r["schedule"]] = r
        rows = sorted(cand.values(), key=lambda r: r["projected_date"])
        if not rows:
            rows = [upcoming[0]]

        first = date.fromisoformat(rows[0]["projected_date"])
        near = [r for r in rows
                if (date.fromisoformat(r["projected_date"]) - first).days <= 180]
        dates = sorted({r["projected_date"] for r in near})
        names = sorted({r["schedule"] for r in near})
        if len(dates) == 1:
            agreement = "✅  %s\nagree on `%s`" % (" + ".join(names), dates[0])
        else:
            agreement = "⚡  split\n" + "\n".join(
                "`%s`  %s%s" % (r["projected_date"], r["schedule"],
                                "  \u26a0\ufe0f" if (leg["ended"]
                                                  and r["schedule"] == "oracle")
                                else "")
                for r in near)
            if leg["ended"] and any(r["schedule"] == "oracle" for r in near):
                agreement += ("\n\u26a0\ufe0f *oracle chains through the %s "
                              "that never printed*" % leg["to_kind"].upper())

        # Only the chain that actually runs through the pivot that never
        # printed gets the warning. `derived` re-anchored onto the realized
        # low, so flagging it here would be exactly the kind of misattributed
        # claim this project exists to avoid.
        lead = rows[0]
        next_proj = {"date": first, "kind": lead["kind"], "schedules": names,
                     "schedule": lead["schedule"],
                     "chains_unrealized": leg["ended"]
                                          and lead["schedule"] == "oracle"}
    else:
        agreement = "n/a"

    return {"leg": leg, "price": price, "pivots": ps, "running_low": low,
            "band": (band_lo, band_hi), "analogue": ana, "must_happen": must,
            "next_proj": next_proj,
            "divergence": div, "invalidations": inval,
            "scored": scored, "hits": hits, "agreement": agreement, "today": today,
            "provisional": prov, "new_low_count": len(new_lows),
            "new_lows": new_lows, "tolerance": sch.TOLERANCE_DAYS}


def resolve_projections(conn, series, ps, today):
    """Grade every projection whose window has closed, and re-grade any
    provisional verdict that can now be settled.

    Not one-shot any more. A provisional grade is a placeholder standing in for
    evidence that cannot exist yet - an ATL is only confirmed once the NEXT ATH
    confirms, years later - so it must be revisited. Only a CHANGE in verdict
    alerts; re-posting the same provisional result every morning would be noise.
    """
    alerts = []
    prov = pv.provisional_atl(series, ps[-1]["date"], today) if ps else None

    rows = conn.execute(
        "SELECT p.*, s.days_off AS old_days, s.within_tolerance AS old_ok, "
        "       s.provisional AS old_prov, s.realized_date AS old_realized "
        "FROM projections p LEFT JOIN scores s ON s.projection_id = p.id "
        "WHERE s.projection_id IS NULL OR s.provisional = 1").fetchall()

    for r in rows:
        proj = {"date": date.fromisoformat(r["projected_date"]), "kind": r["kind"]}
        g = sc.evaluate(proj, ps, prov, today, r["tolerance_days"])
        if g is None:
            continue

        had = r["old_realized"] is not None or r["old_days"] is not None
        unchanged = (had
                     and r["old_days"] == g["days_off"]
                     and bool(r["old_ok"]) == bool(g["within_tolerance"])
                     and bool(r["old_prov"]) == bool(g["provisional"]))
        if unchanged:
            continue

        conn.execute(
            "INSERT OR REPLACE INTO scores "
            "(projection_id, realized_date, days_off, within_tolerance, "
            " scored_at, provisional) VALUES (?,?,?,?,?,?)",
            (r["id"],
             g["realized_date"].isoformat() if g["realized_date"] else None,
             g["days_off"], 1 if g["within_tolerance"] else 0,
             now_iso(), 1 if g["provisional"] else 0))

        if g["days_off"] is None:
            verdict = ("**UNRESOLVED** — the band closed with no qualifying %s"
                       % r["kind"].upper())
            detail = ["Projected  ·  `%s`" % r["projected_date"],
                      "Realized   ·  *nothing within %dd*" % sc.MAX_MATCH_DAYS]
        else:
            verdict = "**%+d days**  →  **%s%s**" % (
                g["days_off"],
                "HIT, within ±%dd" % r["tolerance_days"]
                if g["within_tolerance"] else "MISS",
                "  *(provisional)*" if g["provisional"] else "")
            detail = ["Projected  ·  `%s`" % r["projected_date"],
                      "Realized   ·  `%s`%s" % (g["realized_date"].isoformat(),
                                                "  *(running leg low)*"
                                                if g["provisional"] else "")]

        title = "%s %s %s  ·  %s" % (
            r["kind"].upper(), r["projected_date"],
            "re-graded" if had else "resolved", r["schedule"])
        alerts.append(report.alert_embed(
            "hit" if g["within_tolerance"] else "miss",
            title, detail + ["", verdict]))

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


def healthcheck(conn, dry_run):
    """Is the daily job actually running? Returns a process exit code.

    This exists as a SEPARATE entry point with its own launchd job because the
    daily run cannot alert on its own non-execution - if the machine has no
    network at 09:10 the script never starts, and a check living inside it
    would never execute either. That is exactly how the 2026-08-15 and
    2026-08-19 failures went unnoticed. A second job later in the day, once
    network is likely back, asks whether the morning run actually landed a
    snapshot and shouts if it did not.
    """
    today = datetime.now(timezone.utc).date()
    row = conn.execute("SELECT MAX(date) AS m FROM snapshots").fetchone()
    last = date.fromisoformat(row["m"]) if row and row["m"] else None
    st = staleness(last, today)

    if not st["stale"]:
        print("ok - %s" % st["reason"])
        return 0

    embed = report.alert_embed(
        "stale", "Tracker has not run",
        ["**%s**" % st["reason"],
         "",
         "Expected a run every day at 09:10. The price series self-heals on the "
         "next successful run, but each missed day is a permanent hole in "
         "`snapshots`.",
         "",
         "Check: `launchctl list | grep btccycle` and `logs/launchd.err`."])

    if dry_run:
        print(report.render_text([embed]))
        print("\n[dry run - nothing posted]")
        return 1
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not hook:
        print("STALE and no webhook to report it: %s" % st["reason"], file=sys.stderr)
        return 1
    flush_pending(conn, hook)
    deliver(conn, hook, [embed])
    print("staleness alert - %s" % st["reason"])
    return 1


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

    # A gap is only visible to the run that ends it. Nothing on this machine
    # can alert while the machine is offline, so the first run back reports how
    # many days were lost - otherwise an outage looks exactly like a quiet
    # market, which is what happened across 2026-08-25..29.
    prev = conn.execute("SELECT MAX(date) AS m FROM snapshots").fetchone()
    gap_days = 0
    if prev and prev["m"]:
        gap_days = (today - date.fromisoformat(prev["m"])).days - 1

    found = pv.detect(series)
    fresh = save_pivots(conn, found)
    ps = load_pivots(conn)
    prov_now = pv.provisional_atl(series, ps[-1]['date'], today) if ps else None
    save_projections(conn, sch.all_projections(ps, provisional_low=prov_now))

    embeds = []
    if gap_days > 0:
        embeds.append(report.alert_embed(
            "gap", "%d day%s missed" % (gap_days, "" if gap_days == 1 else "s"),
            ["Last successful run **%s**, this one **%s**."
             % (prev["m"], today.isoformat()),
             "",
             "Price history self-heals (7-day trailing refetch); the `snapshots` "
             "rows for those days do not."]))

    for p in fresh:
        kind = "new_ath" if p["kind"] == "ath" else "new_low"
        embeds.append(report.alert_embed(
            kind, "NEW CONFIRMED %s - %s" % (p["kind"].upper(), p["date"]),
            ["**%s**" % report._money(p["price"]),
             "",
             "The current leg is now re-anchored on this pivot."]))

    embeds += resolve_projections(conn, series, ps, today)

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

    # Countdown milestones. One-shot per target date: without the alerts_sent
    # guard the daily run re-fires whichever tier it currently sits inside,
    # every day, and the callout stops meaning anything.
    tier = ant.due_milestone(leg["remaining"],
                             sent_milestones(conn, leg["target_date"]))
    if tier is not None and state["provisional"]:
        keys = ["milestone:%s:%d" % (leg["target_date"].isoformat(), m)
                for m in ant.consumed_tiers(tier)]
        embeds.append(report.alert_embed(
            "milestone",
            "T-%d  ·  %s %s" % (leg["remaining"], leg["to_kind"].upper(),
                                leg["target_date"].isoformat()),
            [("Crossed the **T-%d** mark." % tier) if leg["remaining"] == tier
             else ("Crossed the **T-%d** mark (firing late — first run since "
                   "the countdown passed it)." % tier),
             "",
             "For the pattern to hit, %s." % state["must_happen"]]))
        for k in keys:
            mark_alert_sent(conn, k)

    # Inside the band, say so once a day - that is the stretch where a new leg
    # low is the whole question.
    if band_lo <= today <= band_hi:
        key = "band:%s:%s" % (leg["target_date"].isoformat(), today.isoformat())
        if not alert_sent(conn, key):
            embeds.append(report.alert_embed(
                "in_band", "Inside the band  ·  day %d of %d"
                % ((today - band_lo).days + 1, (band_hi - band_lo).days + 1),
                ["Band `%s` → `%s`." % (band_lo.isoformat(), band_hi.isoformat()),
                 "",
                 "For the pattern to hit, %s." % state["must_happen"]]))
            mark_alert_sent(conn, key)

    if leg["to_kind"] == "atl" and leg["remaining"] <= WINDOW_LEAD_DAYS and state["provisional"]:
        embeds.append(report.anticipation_embed(state))
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
    flushed = flush_pending(conn, hook)
    if flushed:
        print("flushed %d queued post(s)" % flushed)
    if deliver(conn, hook, embeds):
        print("posted %d embed(s)" % len(embeds))
    else:
        raise SystemExit("delivery failed; %d embed(s) queued for the next run"
                         % len(embeds))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--window", action="store_true",
                    help="ATL-window view only, no post")
    ap.add_argument("--healthcheck", action="store_true",
                    help="alert if the daily run is overdue; exit 1 if stale")
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
    elif args.healthcheck:
        sys.exit(healthcheck(conn, args.dry_run))
    else:
        daily(conn, args.dry_run)


if __name__ == "__main__":
    main()
