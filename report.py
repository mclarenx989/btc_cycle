#!/usr/bin/env python3
"""Discord embeds, plus a plain-text rendering of the same content so a
--dry-run shows exactly what would be posted.

Presentation only. Every value shown here is computed upstream in pivots /
schedule / score - this module never derives a number of its own.
"""

import json
import time
import urllib.error
import urllib.request
from datetime import timedelta

GREEN, AMBER, RED, BLUE, VIOLET = 0x2ECC71, 0xF1C40F, 0xE74C3C, 0x3498DB, 0x9B59B6
BAR_WIDTH = 20

FILLED, EMPTY = "█", "░"   # full block / light shade


def _money(v):
    return "$" + format(int(round(v)), ",")


def _pct(v):
    return "%+.1f%%" % v


def bar(pct, width=BAR_WIDTH):
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100.0 * width))
    return "`" + FILLED * filled + EMPTY * (width - filled) + "`"


def drawdown_history(pivots):
    """Peak-to-trough for each completed cycle."""
    out = []
    for a, b in zip(pivots, pivots[1:]):
        if a["kind"] == "ath" and b["kind"] == "atl":
            out.append({
                "ath": a, "atl": b,
                "pct": (b["price"] / a["price"] - 1.0) * 100.0,
                "days": (b["date"] - a["date"]).days,
            })
    return out


def _band(leg, tolerance):
    return (leg["target_date"] - timedelta(days=tolerance),
            leg["target_date"] + timedelta(days=tolerance))


# ---------------------------------------------------------------------------
# embeds
# ---------------------------------------------------------------------------

def _next_pivot_text(state, leg):
    """After a leg ends, the next pivot is the next REGISTERED projection, not
    the dead leg's target date sitting in the past."""
    nxt = state.get("next_proj")
    if not leg["ended"] or not nxt:
        return "**%s**\n%s" % (leg["target_date"].isoformat(), leg["to_kind"].upper())
    out = "**%s**\n%s" % (nxt["date"].isoformat(), nxt["kind"].upper())
    out += "\n`%s`" % nxt.get("schedule", "")
    if nxt.get("chains_unrealized"):
        out += ("\n\u26a0\ufe0f *chains through the %s that never printed*"
                % leg["to_kind"].upper())
    return out


def daily_embed(state):
    leg = state["leg"]
    ath = leg["from"]
    price = state["price"]
    dd = (price / ath["price"] - 1.0) * 100.0

    hist = drawdown_history(state["pivots"])
    if hist:
        hist_txt = "\n".join(
            "`%s → %s`  **%.0f%%**  in %dd"
            % (h["ath"]["date"].year, str(h["atl"]["date"].year)[2:], h["pct"], h["days"])
            for h in hist)
    else:
        hist_txt = "—"

    low = state.get("running_low")
    if low:
        low_txt = "**%s**  ·  %s  ·  %s off ATH" % (
            _money(low["low"]), low["date"].isoformat(),
            _pct((low["low"] / ath["price"] - 1.0) * 100.0))
    else:
        low_txt = "—"

    marks = "✅" * state["hits"] + "❌" * (state["scored"] - state["hits"])
    record = "%s  **%d / %d** within ±3d" % (marks or "—",
                                                  state["hits"], state["scored"])

    colour = VIOLET if leg["remaining"] < 0 else (BLUE if leg["remaining"] <= 14 else AMBER)

    fields = [
        {"name": "\U0001f4b5  Price",
         "value": "**%s**  ·  %s from ATH %s" % (_money(price), _pct(dd), _money(ath["price"])),
         "inline": False},
        {"name": ("\u2705  Leg resolved  —  %s → %s"
                  if leg["ended"] else "⏳  Cycle progress  —  %s → %s")
                 % (ath["kind"].upper(), leg["to_kind"].upper()),
         "value": ("%s  **overran by %d days**\nprojected %d-day leg ended `%s` "
                   "with no %s"
                   % (bar(100.0), leg["elapsed"] - leg["span"], leg["span"],
                      leg["target_date"].isoformat(), leg["to_kind"].upper()))
                  if leg["ended"] else
                  ("%s  **%.1f%%**\n`%d` of `%d` days  ·  `%d` remaining"
                   % (bar(leg["pct"]), leg["pct"], leg["elapsed"], leg["span"],
                      leg["remaining"])),
         "inline": False},
        {"name": "\U0001f3af  Next pivot",
         "value": _next_pivot_text(state, leg),
         "inline": True},
        {"name": "\U0001f9ed  Schedules",
         "value": state["agreement"], "inline": True},
        {"name": "\U0001fa78  Leg low so far", "value": low_txt, "inline": False},
        {"name": "\U0001f4c9  Prior drawdowns", "value": hist_txt, "inline": True},
        {"name": "\U0001f3c5  Pattern record", "value": record, "inline": True},
    ]
    return {
        "title": ("\u2705  BTC Cycle Tracker  ·  leg resolved, day %d of a "
                  "projected %d" if leg["ended"]
                  else "⏳  BTC Cycle Tracker  ·  Day %d / %d")
                 % (leg["elapsed"], leg["span"]),
        "description": "*The /biz/ 1064d ⁄ 364d cycle — No.56956841, "
                       "which called the %s top.*" % ath["date"].isoformat(),
        "color": colour,
        "fields": fields,
    }


def window_embed(state):
    """The ATL-window view. Foregrounds the one thing knowable in real time:
    whether a new leg low printed inside the tolerance band."""
    leg = state["leg"]
    prov = state["provisional"]
    band_lo, band_hi = _band(leg, state["tolerance"])
    in_band = prov and band_lo <= prov["date"] <= band_hi

    fields = [
        {"name": "\U0001f5d3️  Target band",
         "value": "**%s → %s**\n±%dd around `%s`"
                  % (band_lo.isoformat(), band_hi.isoformat(),
                     state["tolerance"], leg["target_date"].isoformat()),
         "inline": True},
        {"name": "\U0001f3c1  Outcome" if leg["ended"] else "⏱️  T-minus",
         "value": ("**window closed**\n%d days ago" % -leg["remaining"])
                  if leg["ended"] else "**%d days**" % leg["remaining"],
         "inline": True},
        {"name": "\U0001fa78  Leg low so far",
         "value": "**%s**  ·  `%s`\nheld **%d days**  ·  %d new lows this leg"
                  % (_money(prov["price"]), prov["date"].isoformat(),
                     prov["stood_days"], state["new_low_count"]),
         "inline": False},
        {"name": "\U0001f52e  Confidence it is THE low",
         "value": prov["confidence"], "inline": False},
        {"name": "\U0001f6a8  Status" if in_band else "\U0001f4cd  Status",
         "value": ("**A new leg low HAS printed inside the band.**" if in_band else
                   ("**No leg low printed inside the band.** The projection missed."
                    if leg["ended"] else "No leg low inside the band yet.")),
         "inline": False},
    ]
    return {
        "title": "%s  ATL WINDOW  ·  %s ±%dd"
                 % ("\U0001f3c1" if leg["ended"] else "\U0001f3af",
                    leg["target_date"].isoformat(), state["tolerance"]),
        "color": VIOLET if in_band else AMBER,
        "fields": fields,
    }


def anticipation_embed(state):
    """Forward-looking view: what to watch for, and what would settle it.

    Every number here is measured, and the sample size travels with it - three
    completed cycles is the whole evidence base and the reader has to see that
    in the same breath as the figure.
    """
    leg = state["leg"]
    band_lo, band_hi = state["band"]
    prov = state["provisional"]
    ana = state["analogue"]
    div = state["divergence"]

    fields = [
        {"name": "\U0001f3c1  What was required" if leg["ended"]
                 else "\U0001f3af  What must happen",
         "value": ("Required: %s.\n**Resolved %d days ago.**"
                   % (state["must_happen"], -leg["remaining"]))
                  if leg["ended"] else
                  ("For the pattern to hit, %s.\n**%d days** left."
                   % (state["must_happen"], leg["remaining"])),
         "inline": False},
    ]

    if ana["rows"]:
        lines = ["`%s`  **%+.0f%%** above its eventual low"
                 % (r["year"], r["above_pct"]) for r in ana["rows"]]
        lo, hi = ana["implied_low"]
        lines.append("")
        lines.append("Today: **%+.0f%%** above the running low."
                     % ana["current_above_running_low"])
        lines.append("Same distances imply a low of **%s – %s**."
                     % (_money(lo), _money(hi)))
        if ana.get("all_analogues_above_running_low"):
            lines.append("*Every analogue now implies a low at or above one that "
                         "has already printed — all of them are consistent with "
                         "the low already being in.*")
        if ana.get("n_total") and ana["n"] < ana["n_total"]:
            lines.append("*Sample is %d of %d cycles: shorter legs have no day %d.*"
                         % (ana["n"], ana["n_total"], leg["elapsed"]))
        lines.append("*n=%d completed cycles; the spread is %.0f points wide, "
                     "so this is context, not a forecast.*"
                     % (ana["n"],
                        max(r["above_pct"] for r in ana["rows"])
                        - min(r["above_pct"] for r in ana["rows"])))
        fields.append({"name": "\U0001f4d0  At day %d of prior down legs" % leg["elapsed"],
                       "value": "\n".join(lines), "inline": False})

    if div["prior"]:
        dl = ["`%s`  %d new lows, **%d** in its final %dd"
              % (p["year"], p["total"], p["late"], 60) for p in div["prior"]]
        dl.append("")
        dl.append("This leg: **%d days** since the last new low (%s)."
                  % (div["quiet_days"], prov["date"].isoformat()))
        if div["diverging"]:
            dl.append("\u26a0\ufe0f  **Diverging** — every prior leg was still "
                      "printing new lows this late.")
        fields.append({"name": "\U0001f4e1  Divergence", "value": "\n".join(dl),
                       "inline": False})

    iv = ["%s  %s\n\u2003*%s*"
          % ("\u2705" if i["triggered"] else "\u2b1c", i["condition"], i["detail"])
          for i in state["invalidations"]]
    fields.append({"name": "\u2620\ufe0f  Pre-registered invalidation",
                   "value": "\n".join(iv), "inline": False})

    return {
        "title": "\U0001f52d  Anticipation  \u00b7  %s %s"
                 % (leg["to_kind"].upper(), leg["target_date"].isoformat()),
        "color": VIOLET if div.get("diverging") else BLUE,
        "fields": fields,
    }


ALERT_STYLE = {
    "new_ath":  ("\U0001f680", GREEN),
    "new_low":  ("\U0001f53b", RED),
    "in_band":  ("\U0001f3af", VIOLET),
    "window":   ("⚠️", AMBER),
    "milestone": ("\u23f3", AMBER),
    "resolved": ("\U0001f4cb", BLUE),
    "stale":    ("\U0001f6d1", RED),
    "gap":      ("\U0001f4ed", RED),
    "hit":      ("✅", GREEN),
    "miss":     ("❌", RED),
}


def alert_embed(kind, title, lines):
    emoji, colour = ALERT_STYLE.get(kind, ("\U0001f4cc", BLUE))
    return {
        "title": "%s  %s" % (emoji, title),
        "description": "\n".join(lines),
        "color": colour,
    }


# ---------------------------------------------------------------------------
# rendering / transport
# ---------------------------------------------------------------------------

def render_text(embeds):
    """What --dry-run prints. Same content, terminal-shaped."""
    def plain(s):
        return s.replace("`", "").replace("**", "").replace("*", "")

    out = []
    for e in embeds:
        out.append("=" * 64)
        out.append(plain(e["title"]))
        if e.get("description"):
            out.append(plain(e["description"]))
        if e.get("fields"):
            out.append("-" * 64)
        for f in e.get("fields", []):
            out.append(plain(f["name"]))
            for line in plain(f["value"]).split("\n"):
                out.append("    " + line)
        if e.get("footer"):
            out.append("-" * 64)
            out.append(e["footer"]["text"])
    out.append("=" * 64)
    return "\n".join(out)


class PostFailed(Exception):
    """Delivery failed. Raised rather than exiting, so the caller can queue the
    embeds instead of losing them - which is exactly what happened on
    2026-08-27/28/29 and 2026-09-18: the alert was correct, and nobody saw it."""


def post(webhook_url, embeds, attempts=3, delay=20):
    """Deliver, retrying transient network failures.

    The observed failure is DNS at wake ("nodename nor servname provided"),
    which resolves within minutes. An HTTP error from Discord is NOT retried -
    a 400 payload or a revoked webhook will fail identically every time.
    """
    body = json.dumps({"embeds": embeds}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "btc-cycle/1.0"})
    last = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            raise PostFailed("Discord rejected the post (%s): %s"
                             % (exc.code, exc.read().decode("utf-8", "replace")[:300]))
        except urllib.error.URLError as exc:
            last = exc.reason
            if attempt < attempts:
                time.sleep(delay)
    raise PostFailed("Could not reach Discord after %d attempts: %s" % (attempts, last))
