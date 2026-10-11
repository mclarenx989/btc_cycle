#!/usr/bin/env python3
"""Projected pivot dates. Pure functions - no network, no database, no clock.

Three schedules, differing only in what they anchor on.

  oracle      The /biz/ post's literal chain. Anchors on the 2021-11-08
              daily-close ATH and adds 364/1064 forever. This is the claim
              that actually went viral, and it is what produced 2025-10-06.
  derived     The same 152w/52w rule applied to REALIZED pivots.
  ath_to_ath  The tighter empirical formulation: ATH->ATH has run 1424d then
              1426d across eight years.

oracle and derived re-converged after 2025, because the oracle's projected ATH
became the realized one. They split again only if the low misses 2026-10-05.
"""

from datetime import timedelta

UP_LEG = 1064    # 152.0 weeks, ATL -> ATH
DOWN_LEG = 364   # 52.0 weeks, ATH -> ATL
ATH_TO_ATH = 1425  # mean of the realized 1424 / 1426
TOLERANCE_DAYS = 3

# The post's own arithmetic, for scoring it against reality. Keyed by the
# leg's starting pivot date rather than by position - the realized leg list
# grows at the front as history is extended backwards, and a positional zip
# silently compares the wrong pairs.
def _d(s):
    from datetime import date as _date
    y, m, dd = (int(x) for x in s.split("-"))
    return _date(y, m, dd)


CLAIMED_LEGS = [
    (_d("2015-01-14"), "ATL 2015 -> ATH 2017", UP_LEG),
    (_d("2017-12-17"), "ATH 2017 -> ATL 2018", DOWN_LEG),
    (_d("2018-12-15"), "ATL 2018 -> ATH 2021", UP_LEG),
    (_d("2021-11-10"), "ATH 2021 -> ATL 2022", DOWN_LEG),
]


def chain(anchor_date, anchor_kind, steps):
    """Alternate 364/1064 forward from an anchor pivot."""
    out = []
    d, kind = anchor_date, anchor_kind
    for _ in range(steps):
        if kind == "ath":
            d, kind = d + timedelta(days=DOWN_LEG), "atl"
        else:
            d, kind = d + timedelta(days=UP_LEG), "ath"
        out.append({"date": d, "kind": kind})
    return out


def oracle(steps=6):
    """The post's chain, from its own anchor. Reproduces 2025-10-06 exactly."""
    from datetime import date
    rows = chain(date(2021, 11, 8), "ath", steps)
    for r in rows:
        r["schedule"] = "oracle"
        r["basis"] = "biz No.56956841 chain from 2021-11-08 daily-close ATH"
    return rows


def derived(pivots, steps=3, provisional_low=None):
    """152w/52w applied to the latest realized pivot.

    A CONFIRMED low cannot exist until the next ATH confirms, years out, so
    for its whole life this chained from the last confirmed ATH and was
    therefore identical to `oracle` - which is why it never once diverged and
    was never independently tested. When a provisional low exists and is more
    recent than the last confirmed pivot, anchor there instead: that low is
    the best available estimate of what actually happened, which is the entire
    point of calling this schedule "derived".
    """
    if not pivots:
        return []
    last = pivots[-1]
    anchor_date, anchor_kind, note = last["date"], last["kind"], "realized"
    if provisional_low is not None and provisional_low["date"] > last["date"]:
        anchor_date, anchor_kind, note = provisional_low["date"], "atl", "provisional"
    rows = chain(anchor_date, anchor_kind, steps)
    for r in rows:
        r["schedule"] = "derived"
        r["basis"] = "364/1064 from %s %s %s" % (
            note, anchor_kind.upper(), anchor_date.isoformat())
    return rows


def ath_to_ath(pivots, steps=2):
    """1425d ATH->ATH spacing from the latest realized ATH."""
    aths = [p for p in pivots if p["kind"] == "ath"]
    if not aths:
        return []
    d = aths[-1]["date"]
    rows = []
    for _ in range(steps):
        d = d + timedelta(days=ATH_TO_ATH)
        rows.append({"date": d, "kind": "ath", "schedule": "ath_to_ath",
                     "basis": "1425d ATH->ATH from realized %s" % aths[-1]["date"].isoformat()})
    return rows


def all_projections(pivots, provisional_low=None):
    return oracle() + derived(pivots, provisional_low=provisional_low) \
        + ath_to_ath(pivots)


def current_leg(pivots, today):
    """Where we are between the last realized pivot and the next projection."""
    if not pivots:
        return None
    last = pivots[-1]
    nxt_kind = "atl" if last["kind"] == "ath" else "ath"
    span = DOWN_LEG if last["kind"] == "ath" else UP_LEG
    target = last["date"] + timedelta(days=span)
    elapsed = (today - last["date"]).days
    remaining = (target - today).days
    return {
        "from": last,
        "to_kind": nxt_kind,
        "target_date": target,
        "span": span,
        "elapsed": elapsed,
        "remaining": remaining,
        # Clamp for display. The raw percentage is kept because the overshoot
        # itself is information - the leg ran past its projected length.
        "pct": min(100.0, (elapsed / float(span)) * 100.0),
        "pct_raw": (elapsed / float(span)) * 100.0,
        # The projected pivot date has passed and its tolerance band closed.
        # Everything downstream that assumes a live countdown must branch here:
        # a negative "T-minus" and a 101.6% progress bar are the symptoms of
        # not doing so.
        "ended": remaining < -TOLERANCE_DAYS,
    }
