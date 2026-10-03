#!/usr/bin/env python3
"""Intraday threshold watcher. Runs in GitHub Actions; also runs locally.

The daily tracker already catches every event, just up to 24h late. This closes
that gap for the only two switches that are price-driven rather than calendar-
driven: a new leg low, and a new ATH. Everything else the tracker reports
(confidence tiers, divergence, band entry, projection resolution) moves at most
once a day and does not need polling.

Reads the DAY'S low/high, never spot. A flash crash that recovers before the
next poll still shows up in the day's low, so a 15-minute cadence cannot miss a
wick - which polling spot absolutely would.

Self-contained by design: it recomputes the leg low from exchange data each run
rather than reading the Mac's SQLite, so the two halves never disagree and the
cloud side keeps working when the laptop is shut.

Stdlib only. Python 3.9 compatible.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "watch_config.json")
STATE = os.path.join(HERE, "watch_state.json")

TICKER = "https://www.bitstamp.net/api/v2/ticker/btcusd/"
OHLC = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
UA = {"User-Agent": "btc-cycle-watch/1.0"}

GREEN, RED, VIOLET, AMBER = 0x2ECC71, 0xE74C3C, 0x9B59B6, 0xF1C40F


def _get(url, params=None):
    if params:
        url += "?" + "&".join("%s=%s" % kv for kv in params.items())
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _money(v):
    return "$" + format(int(round(v)), ",")


def load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (IOError, ValueError):
        return default


def leg_low_before_today(anchor_date, today):
    """Lowest low since the anchor ATH, EXCLUDING today.

    Excluding today is what makes 'a new low printed' a real crossing: compared
    against a figure that already included today, today could never beat it.
    """
    start = int(datetime.strptime(anchor_date, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc).timestamp())
    rows = _get(OHLC, {"step": "86400", "limit": "1000", "start": str(start)}) \
        .get("data", {}).get("ohlc", [])
    lows = []
    for c in rows:
        d = datetime.fromtimestamp(int(c["timestamp"]), tz=timezone.utc).date()
        if d > datetime.strptime(anchor_date, "%Y-%m-%d").date() and d < today:
            lows.append((float(c["low"]), d))
    if not lows:
        return None, None
    lo, d = min(lows)
    return lo, d


def post(webhook, embeds):
    body = json.dumps({"embeds": embeds}).encode()
    req = urllib.request.Request(
        webhook, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "btc-cycle-watch/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def main():
    cfg = load(CONFIG, {})
    st = load(STATE, {})
    today = datetime.now(timezone.utc).date()

    t = _get(TICKER)
    last, day_low, day_high = float(t["last"]), float(t["low"]), float(t["high"])

    prior_low, prior_low_date = leg_low_before_today(cfg["anchor_ath_date"], today)
    ath = float(cfg["anchor_ath_price"])
    in_band = cfg["band"][0] <= today.isoformat() <= cfg["band"][1]

    alerts = []
    changed = False

    # 1. New leg low. The whole ballgame, and louder inside the band.
    if prior_low is not None and day_low < prior_low:
        if st.get("alerted_leg_low") is None or day_low < float(st["alerted_leg_low"]):
            alerts.append({
                "title": ("%s NEW LEG LOW  ·  %s"
                          % ("\U0001f3af IN BAND —" if in_band else "\U0001f53b",
                             today.isoformat())),
                "description": "\n".join([
                    "**%s**  (prior leg low %s, set %s)"
                    % (_money(day_low), _money(prior_low), prior_low_date),
                    "",
                    ("**Inside the %s → %s band.** This is the event the tracker "
                     "exists to catch." % (cfg["band"][0], cfg["band"][1]))
                    if in_band else
                    "Outside the band (%s → %s)." % (cfg["band"][0], cfg["band"][1]),
                ]),
                "color": VIOLET if in_band else RED,
            })
            st["alerted_leg_low"] = day_low
            changed = True

    # 2. New ATH - invalidation condition #1.
    if day_high > ath and not st.get("alerted_ath"):
        alerts.append({
            "title": "\U0001f680  NEW ALL-TIME HIGH  ·  %s" % today.isoformat(),
            "description": "**%s**  (prior ATH %s)\n\nInvalidation #1 has "
                           "triggered: a new ATH before any cycle low."
                           % (_money(day_high), _money(ath)),
            "color": GREEN,
        })
        st["alerted_ath"] = True
        changed = True

    # 3. Significant move, measured from the last alerted reference rather than
    # a fixed window - so a grinding 5% and a fast 5% both fire once, and chop
    # around one level does not fire repeatedly.
    move_pct = float(cfg.get("move_pct", 5.0))
    ref = st.get("ref_price")
    if ref is None:
        st["ref_price"] = last
        changed = True
    else:
        delta = (last / float(ref) - 1.0) * 100.0
        if abs(delta) >= move_pct:
            to_trigger = (prior_low / last - 1.0) * 100.0 if prior_low else None
            alerts.append({
                "title": "%s  %+.1f%% move  ·  %s"
                         % ("\U0001f4c8" if delta > 0 else "\U0001f4c9",
                            delta, _money(last)),
                "description": "\n".join([
                    "From %s to **%s** since the last reference."
                    % (_money(float(ref)), _money(last)),
                    "",
                    ("New leg low needs **%.1f%%** from here (%s)."
                     % (to_trigger, _money(prior_low))) if to_trigger is not None else "",
                ]).strip(),
                "color": GREEN if delta > 0 else AMBER,
            })
            st["ref_price"] = last
            changed = True

    if changed:
        st["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        st["last_price"] = last

    if alerts:
        hook = os.environ.get("DISCORD_WEBHOOK_URL")
        if not hook:
            print("ALERTS but no DISCORD_WEBHOOK_URL set", file=sys.stderr)
            return 1
        post(hook, alerts)
        print("posted %d alert(s)" % len(alerts))
    else:
        print("no alert — last %s, day low %s, leg low %s (%.1f%% away)"
              % (_money(last), _money(day_low),
                 _money(prior_low) if prior_low else "n/a",
                 ((prior_low / last - 1.0) * 100.0) if prior_low else 0.0))

    # Only touch the file when something meaningful moved. Stamping last_run
    # every pass made the file differ on every run, and the workflow commits on
    # any diff - 96 commits a day saying nothing. The run log already records
    # the current price each pass; this file is for dedup state only.
    if changed:
        with open(STATE, "w") as fh:
            json.dump(st, fh, indent=2, sort_keys=True)
            fh.write("\n")
    print("STATE_CHANGED=%s" % ("1" if changed else "0"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
