#!/usr/bin/env python3
"""Discord embeds, plus a plain-text rendering of the same content so a
--dry-run shows exactly what would be posted.
"""

import json
import urllib.error
import urllib.request

GREEN, AMBER, RED, BLUE = 0x2ECC71, 0xF1C40F, 0xE74C3C, 0x3498DB
BAR_WIDTH = 24


def _money(v):
    return "$" + format(int(round(v)), ",")


def bar(pct, width=BAR_WIDTH):
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100.0 * width))
    return "`" + "#" * filled + "-" * (width - filled) + "`"


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


def daily_embed(state):
    leg = state["leg"]
    ath = leg["from"]
    price = state["price"]
    dd = (price / ath["price"] - 1.0) * 100.0

    hist = drawdown_history(state["pivots"])
    hist_txt = " / ".join("%.0f%%" % h["pct"] for h in hist) or "n/a"

    low = state.get("running_low")
    if low:
        low_txt = "%s on %s (%.1f%% off ATH)" % (
            _money(low["low"]), low["date"].isoformat(),
            (low["low"] / ath["price"] - 1.0) * 100.0)
    else:
        low_txt = "n/a"

    agree = state["agreement"]
    colour = AMBER if leg["remaining"] > 14 else BLUE
    if leg["remaining"] < 0:
        colour = RED

    fields = [
        {"name": "Price",
         "value": "%s  (%.1f%% from ATH %s)" % (_money(price), dd, _money(ath["price"])),
         "inline": False},
        {"name": "Current leg  %s -> %s" % (ath["kind"].upper(), leg["to_kind"].upper()),
         "value": "%s  %d / %d days  (%.1f%%)" % (
             bar(leg["pct"]), leg["elapsed"], leg["span"], leg["pct"]),
         "inline": False},
        {"name": "Next projected pivot",
         "value": "**%s %s** in **%d days**" % (
             leg["to_kind"].upper(), leg["target_date"].isoformat(), leg["remaining"]),
         "inline": True},
        {"name": "Schedules", "value": agree, "inline": True},
        {"name": "Lowest so far this leg", "value": low_txt, "inline": False},
        {"name": "Prior cycle drawdowns", "value": hist_txt, "inline": True},
        {"name": "Pattern record",
         "value": "%d/%d projections within +/-3d" % (state["hits"], state["scored"]),
         "inline": True},
    ]
    return {
        "title": "BTC Cycle Tracker - day %d of %d" % (leg["elapsed"], leg["span"]),
        "description": ("Tracking the /biz/ 1064d/364d claim (No.56956841, 2023-12-12) "
                        "that called the %s ATH.") % ath["date"].isoformat(),
        "color": colour,
        "fields": fields,
        "footer": {"text": "timing model only - says nothing about price. not advice."},
    }


def alert_embed(kind, title, lines):
    colours = {"new_ath": GREEN, "new_low": RED, "window": AMBER, "resolved": BLUE}
    return {
        "title": title,
        "description": "\n".join(lines),
        "color": colours.get(kind, BLUE),
    }


def render_text(embeds):
    """What --dry-run prints. Same content, terminal-shaped."""
    out = []
    for e in embeds:
        out.append("=" * 62)
        out.append(e["title"])
        if e.get("description"):
            out.append(e["description"])
        out.append("-" * 62)
        for f in e.get("fields", []):
            out.append("%s:" % f["name"])
            out.append("   " + f["value"].replace("`", "").replace("**", ""))
        if e.get("footer"):
            out.append("-" * 62)
            out.append(e["footer"]["text"])
    out.append("=" * 62)
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
