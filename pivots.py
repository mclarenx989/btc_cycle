#!/usr/bin/env python3
"""Cycle pivot detection. Pure functions over a candle series - no network,
no database, no clock.

A cycle ATH is NOT simply "a high above every prior high" - that marks every
day of a run-up. It is a running-max high that then stands un-exceeded for at
least CONFIRM_DAYS.

CONFIRM_DAYS is calibrated, not guessed. Measured over 4,234 real daily
candles (2015-01-01 to 2026-08-03), the longest any non-pivot running-max high
stood un-exceeded was 237 days (2024-03-13, $73,794), while the shortest true
cycle ATH has stood 302 days (2025-10-06). Anything in 238..302 separates them.
Both edges are asserted in the tests: go lower and the 2024 consolidation peak
is wrongly promoted; go higher and the 2025 ATH cannot be confirmed yet.
"""

CONFIRM_DAYS = 270
CONFIRM_MIN, CONFIRM_MAX = 238, 302


def running_max_highs(series):
    """Every candle that set a new all-time high, in order."""
    best = 0.0
    peaks = []
    for c in series:
        if c["high"] > best:
            best = c["high"]
            peaks.append(c)
    return peaks


def confirmed_aths(series, confirm_days=CONFIRM_DAYS):
    """Running-max highs left un-exceeded for >= confirm_days.

    Where a cluster of consecutive new highs all qualify (a peak set over
    several days), only the highest is kept - that is the cycle top.
    """
    peaks = running_max_highs(series)
    if not peaks:
        return []
    last_date = series[-1]["date"]
    out = []
    for i, p in enumerate(peaks):
        gap = None
        for later in peaks[i + 1:]:
            if later["high"] > p["high"]:
                gap = (later["date"] - p["date"]).days
                break
        if gap is None:
            gap = (last_date - p["date"]).days
        if gap >= confirm_days:
            out.append(dict(p, unexceeded_days=gap))
    # Collapse clusters: consecutive qualifying peaks within confirm_days of
    # each other are one event; keep the highest.
    collapsed = []
    for p in out:
        if collapsed and (p["date"] - collapsed[-1]["date"]).days < confirm_days:
            if p["high"] > collapsed[-1]["high"]:
                collapsed[-1] = p
        else:
            collapsed.append(p)
    return collapsed


def cycle_lows(series, aths):
    """The minimum low between each pair of consecutive confirmed ATHs."""
    lows = []
    for a, b in zip(aths, aths[1:]):
        window = [c for c in series if a["date"] < c["date"] < b["date"]]
        if not window:
            continue
        m = min(window, key=lambda c: c["low"])
        lows.append(m)
    return lows


def running_low_since(series, since_date):
    """Lowest low since a date - the *unconfirmed* low of the current leg."""
    window = [c for c in series if c["date"] > since_date]
    if not window:
        return None
    return min(window, key=lambda c: c["low"])


def detect(series, confirm_days=CONFIRM_DAYS):
    """Full pivot table, oldest first. Each entry: date, kind, price."""
    aths = confirmed_aths(series, confirm_days)
    lows = cycle_lows(series, aths)
    out = [{"date": a["date"], "kind": "ath", "price": a["high"],
            "source": a["source"]} for a in aths]
    out += [{"date": l["date"], "kind": "atl", "price": l["low"],
             "source": l["source"]} for l in lows]
    out.sort(key=lambda p: p["date"])
    return out


def legs(pivots):
    """Day counts between consecutive pivots."""
    out = []
    for a, b in zip(pivots, pivots[1:]):
        out.append({
            "from": a, "to": b,
            "days": (b["date"] - a["date"]).days,
            "label": "%s->%s" % (a["kind"].upper(), b["kind"].upper()),
        })
    return out
