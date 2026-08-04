#!/usr/bin/env python3
"""Grade projections against what actually happened. Pure functions.

A projection row is written once and never updated; grades live here, keyed to
it. That separation is the point of the ledger - a projection cannot be quietly
moved after the fact to make the pattern look better.
"""

from schedule import TOLERANCE_DAYS


def resolvable(projection, pivots, today, tolerance_days=TOLERANCE_DAYS):
    """Has this projection's window closed yet?"""
    return (today - projection["date"]).days > tolerance_days


def grade(projection, pivots, tolerance_days=TOLERANCE_DAYS):
    """Match a projection to the nearest realized pivot of the same kind.

    Returns None when no pivot of that kind has been confirmed anywhere near
    it - an unresolved miss, not a pass.
    """
    same = [p for p in pivots if p["kind"] == projection["kind"]]
    if not same:
        return None
    nearest = min(same, key=lambda p: abs((p["date"] - projection["date"]).days))
    days_off = (nearest["date"] - projection["date"]).days
    return {
        "realized_date": nearest["date"],
        "realized_price": nearest["price"],
        "days_off": days_off,
        "within_tolerance": abs(days_off) <= tolerance_days,
    }


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
