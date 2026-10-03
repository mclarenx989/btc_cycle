#!/usr/bin/env python3
"""Grade projections against what actually happened. Pure functions.

A projection row is written once and never updated; grades live here, keyed to
it. That separation is the point of the ledger - a projection cannot be quietly
moved after the fact to make the pattern look better.
"""

from schedule import TOLERANCE_DAYS, DOWN_LEG

# No pivot further than a leg length from a projection can plausibly BE that
# projection's pivot. Without this bound, grade() matched the 2026-10-05 ATL
# projection to the 2022-11-21 pivot - 1,414 days away - and would have written
# that into the write-once ledger as a permanent, meaningless miss.
MAX_MATCH_DAYS = DOWN_LEG


def window_closed(projection, today, tolerance_days=TOLERANCE_DAYS):
    """Has the tolerance band passed? Calendar only - says nothing about
    whether the evidence needed to grade the projection can exist yet."""
    return (today - projection["date"]).days > tolerance_days


# Backwards-compatible alias; the old name claimed more than it checked.
def resolvable(projection, pivots, today, tolerance_days=TOLERANCE_DAYS):
    return window_closed(projection, today, tolerance_days)


def grade(projection, pivots, tolerance_days=TOLERANCE_DAYS,
          max_match_days=MAX_MATCH_DAYS):
    """Match a projection to the nearest CONFIRMED pivot of the same kind,
    within a leg length. Returns None when no candidate is close enough -
    which means "cannot grade this yet", not "missed"."""
    same = [p for p in pivots if p["kind"] == projection["kind"]]
    near = [p for p in same
            if abs((p["date"] - projection["date"]).days) <= max_match_days]
    if not near:
        return None
    nearest = min(near, key=lambda p: abs((p["date"] - projection["date"]).days))
    days_off = (nearest["date"] - projection["date"]).days
    return {
        "realized_date": nearest["date"],
        "realized_price": nearest["price"],
        "days_off": days_off,
        "within_tolerance": abs(days_off) <= tolerance_days,
        "provisional": False,
    }


def evaluate(projection, pivots, provisional_low, today,
             tolerance_days=TOLERANCE_DAYS, max_match_days=MAX_MATCH_DAYS):
    """The full verdict for one projection, or None if it is too early.

    Three outcomes, and keeping them distinct is the whole point:

      definitive  a confirmed pivot sits within a leg length -> final grade
      provisional an ATL cannot be CONFIRMED until the next ATH confirms,
                  years out, so the running leg low stands in. Marked
                  provisional and re-graded later.
      unresolved  the window closed and there is no candidate at all. Recorded
                  with days_off = None rather than a fabricated number.
    """
    if not window_closed(projection, today, tolerance_days):
        return None

    definitive = grade(projection, pivots, tolerance_days, max_match_days)
    if definitive is not None:
        return definitive

    if projection["kind"] == "atl" and provisional_low is not None:
        days_off = (provisional_low["date"] - projection["date"]).days
        if abs(days_off) <= max_match_days:
            return {
                "realized_date": provisional_low["date"],
                "realized_price": provisional_low["price"],
                "days_off": days_off,
                "within_tolerance": abs(days_off) <= tolerance_days,
                "provisional": True,
            }

    return {"realized_date": None, "realized_price": None, "days_off": None,
            "within_tolerance": False, "provisional": True}


def score_claimed_legs(realized_legs, claimed):
    """The post's own four legs vs reality. Feeds --verify.

    Under a +/-3 band this scores 2 pass / 2 fail. That is the honest result:
    the band comes from what a whole-week rounding rule should tolerate, not
    from what makes the source material look good.
    """
    by_start = {leg["from"]["date"]: leg for leg in realized_legs}
    out = []
    for start, label, claimed_days in claimed:
        leg = by_start.get(start)
        if leg is None:
            out.append({"label": label, "claimed": claimed_days, "actual": None,
                        "delta": None, "within_tolerance": False})
            continue
        delta = leg["days"] - claimed_days
        out.append({
            "label": label,
            "claimed": claimed_days,
            "actual": leg["days"],
            "delta": delta,
            "within_tolerance": abs(delta) <= TOLERANCE_DAYS,
        })
    return out
