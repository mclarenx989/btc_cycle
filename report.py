#!/usr/bin/env python3
"""Discord embeds, plus a plain-text rendering of the same content so a
--dry-run shows exactly what would be posted.

Presentation only. Every value shown here is computed upstream in pivots /
schedule / score - this module never derives a number of its own.
"""

import json
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
        {"name": "⏳  Cycle progress  —  %s → %s"
                 % (ath["kind"].upper(), leg["to_kind"].upper()),
         "value": "%s  **%.1f%%**\n`%d` of `%d` days  ·  `%d` remaining"
                  % (bar(leg["pct"]), leg["pct"], leg["elapsed"], leg["span"], leg["remaining"]),
         "inline": False},
        {"name": "\U0001f3af  Next pivot",
         "value": "**%s**\n%s" % (leg["target_date"].isoformat(), leg["to_kind"].upper()),
         "inline": True},
        {"name": "\U0001f9ed  Schedules",
         "value": state["agreement"], "inline": True},
        {"name": "\U0001fa78  Leg low so far", "value": low_txt, "inline": False},
        {"name": "\U0001f4c9  Prior drawdowns", "value": hist_txt, "inline": True},
        {"name": "\U0001f3c5  Pattern record", "value": record, "inline": True},
    ]
    return {
        "title": "⏳  BTC Cycle Tracker  ·  Day %d / %d" % (leg["elapsed"], leg["span"]),
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
        {"name": "⏱️  T-minus",
         "value": "**%d days**" % leg["remaining"], "inline": True},
        {"name": "\U0001fa78  Leg low so far",
         "value": "**%s**  ·  `%s`\nheld **%d days**  ·  %d new lows this leg"
                  % (_money(prov["price"]), prov["date"].isoformat(),
                     prov["stood_days"], state["new_low_count"]),
         "inline": False},
        {"name": "\U0001f52e  Confidence it is THE low",
         "value": prov["confidence"], "inline": False},
        {"name": "\U0001f6a8  Status" if in_band else "\U0001f4cd  Status",
         "value": ("**A new leg low HAS printed inside the band.**"
                   if in_band else "No leg low inside the band yet."),
         "inline": False},
    ]
    return {
        "title": "\U0001f3af  ATL WINDOW  ·  %s ±%dd"
                 % (leg["target_date"].isoformat(), state["tolerance"]),
        "color": VIOLET if in_band else AMBER,
        "fields": fields,
    }


ALERT_STYLE = {
    "new_ath":  ("\U0001f680", GREEN),
    "new_low":  ("\U0001f53b", RED),
    "in_band":  ("\U0001f3af", VIOLET),
    "window":   ("⚠️", AMBER),
    "resolved": ("\U0001f4cb", BLUE),
    "stale":    ("\U0001f6d1", RED),
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


def post(webhook_url, embeds):
    body = json.dumps({"embeds": embeds}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "btc-cycle/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        raise SystemExit("Discord rejected the post (%s): %s"
                         % (exc.code, exc.read().decode("utf-8", "replace")[:300]))
    except urllib.error.URLError as exc:
        raise SystemExit("Could not reach Discord: %s" % exc.reason)
