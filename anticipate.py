#!/usr/bin/env python3
"""Forward-looking callouts. Pure functions - no network, no database, no clock.

Everything else in this project reports what HAS happened. This module answers
"what should I be watching for, and what would settle it" - computed ahead of
the event, so the tracker stops being purely reactive.

Nothing here predicts. Each function returns measured history plus the
arithmetic that turns it into a thing to watch; the caller displays the sample
size alongside, because n=3 completed cycles is the binding constraint on all
of it.
"""

# T-minus tiers. Each fires once, ever, per target date - the daily run would
# otherwise re-fire whichever tier it currently sits inside, every day.
MILESTONES = [60, 30, 14, 7, 3, 1, 0]

# Prior legs are compared over their final stretch. 60 days because that is
# roughly the tolerance band plus a cycle's worth of noise either side, and
# because every completed leg has enough candles in it to count.
FINAL_STRETCH_DAYS = 60


def due_milestone(remaining, already_sent):
    """The most urgent un-fired tier we are now inside, or None.

    `already_sent` is a set of ints. Returns the SMALLEST applicable tier, so
    a countdown that skips ahead (missed runs, or starting mid-window) reports
    the tier it actually reached rather than one it passed days ago.

    The caller must then retire every wider tier via `consumed_tiers`. Without
    that, a run at T-2 fires "T-3" and the next run fires "T-7" — a countdown
    going backwards, which is worse than a missed notification.
    """
    candidates = sorted(m for m in MILESTONES if remaining <= m and m not in already_sent)
    return candidates[0] if candidates else None


def consumed_tiers(tier):
    """Every tier retired by firing `tier` - itself and all wider ones.

    Firing T-3 means T-7, T-14, T-30 and T-60 are moot; they describe a
    countdown position already behind us.
    """
    return [m for m in MILESTONES if m >= tier]


def completed_down_legs(pivots):
    """(ath, atl) pairs for every finished ATH->ATL leg."""
    return [(a, b) for a, b in zip(pivots, pivots[1:])
            if a["kind"] == "ath" and b["kind"] == "atl"]


def analogue(series, pivots, elapsed, price_now, running_low=None):
    """At the same day-index of each prior down leg, how far was price above
    the low that leg eventually made - and what that implies for this one.

    The "was the low still ahead" reading is near-tautological, since an ATL is
    by definition the leg's endpoint. The number with actual content is the
    DISTANCE above it, which is what this returns.
    """
    rows = []
    for a, b in completed_down_legs(pivots):
        span = (b["date"] - a["date"]).days
        at = [c for c in series if (c["date"] - a["date"]).days == elapsed
              and a["date"] < c["date"] <= b["date"]]
        if not at:
            continue
        above = (at[0]["close"] / b["price"] - 1.0) * 100.0
        rows.append({"year": a["date"].year, "span": span, "above_pct": above,
                     "eventual_low": b["price"]})
    if not rows:
        return {"rows": [], "implied_low": None, "n": 0}
    highest = max(r["above_pct"] for r in rows)
    lowest = min(r["above_pct"] for r in rows)
    lo = price_now / (1.0 + highest / 100.0)
    hi = price_now / (1.0 + lowest / 100.0)

    # An implied low ABOVE a low that has already printed is impossible - that
    # level was already traded through. Clamp it, and report whether every
    # analogue now sits above the running low, which is itself the finding:
    # all of them would be consistent with the low already being in.
    all_above = running_low is not None and lo >= running_low
    if running_low is not None:
        hi = min(hi, running_low)
        lo = min(lo, running_low)

    return {
        "rows": rows,
        "n": len(rows),
        # The sample shrinks past the shortest completed leg - a leg of 363
        # days simply has no "day 370" - so the caller must be able to say WHY
        # n moved rather than silently comparing against a different set.
        "n_total": len(completed_down_legs(pivots)),
        "implied_low": (lo, hi),
        "all_analogues_above_running_low": all_above,
        "current_above_running_low": None,   # filled by caller, needs the low
    }


def divergence(series, pivots, new_lows, today):
    """Is this leg behaving like the prior ones did at this stage?

    The signal that matters: every completed leg was still printing new lows
    into its final stretch. A leg that has gone quiet well before its projected
    pivot is diverging from the pattern in the direction of "the low already
    happened, early."
    """
    prior = []
    for a, b in completed_down_legs(pivots):
        leg = [c for c in series if a["date"] < c["date"] <= b["date"]]
        best, marks = float("inf"), []
        for c in leg:
            if c["low"] < best:
                best = c["low"]
                marks.append(c)
        late = [m for m in marks if (b["date"] - m["date"]).days <= FINAL_STRETCH_DAYS]
        prior.append({"year": a["date"].year, "total": len(marks), "late": len(late)})

    quiet = (today - new_lows[-1]["date"]).days if new_lows else None
    return {
        "prior": prior,
        "quiet_days": quiet,
        "all_prior_printed_late": all(p["late"] > 0 for p in prior) if prior else False,
        "diverging": bool(prior) and quiet is not None
                     and quiet > FINAL_STRETCH_DAYS
                     and all(p["late"] > 0 for p in prior),
    }


def must_happen(band_lo, band_hi, running_low, remaining, ended=False):
    """The single crisp falsifiable sentence - future tense while the window is
    open, past tense once it has closed. Saying what "must happen" inside a
    window that shut days ago is how a tracker starts lying quietly."""
    price = "$" + format(int(round(running_low["price"])), ",")
    if ended:
        return ("no leg low printed below **%s** between `%s` and `%s` — "
                "the window closed" % (price, band_lo.isoformat(), band_hi.isoformat()))
    return ("a new leg low must print below **%s** between `%s` and `%s`"
            % (price, band_lo.isoformat(), band_hi.isoformat()))


def invalidations(last_ath, price_now, running_low, band_hi, today, new_lows):
    """Pre-registered kill conditions, each with whether it has triggered.

    Registered ahead of the event on purpose: a condition written down only
    after the fact is not a test.
    """
    low_in_band = any(m for m in new_lows if m["date"] > band_hi)
    return [
        {"condition": "price makes a new ATH before any cycle low",
         "detail": "would need > %s" % ("$" + format(int(round(last_ath["price"])), ",")),
         "triggered": price_now > last_ath["price"]},
        {"condition": "the band closes with no new leg low inside it",
         "detail": "band closes `%s`" % band_hi.isoformat(),
         "triggered": today > band_hi and not low_in_band},
    ]
