#!/usr/bin/env python3
"""Daily BTC candles from public, keyless exchange APIs.

Two sources, one shape out. Bitstamp reaches back to 2011 and is what the
backfill uses; Coinbase is the daily tick and the cross-check, because it is
the exchange the $126k ATH headlines quote.

EVERY timestamp is converted in UTC. Exchange candles are stamped at UTC
midnight, so a local-time conversion shifts every candle back a day in US
Eastern and silently moves every pivot. See the design spec.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

USER_AGENT = "btc-cycle/1.0"
BITSTAMP = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles"


def _utc_date(epoch_seconds):
    """The one and only timestamp->date conversion in this project."""
    return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc).date()


def _get(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit("HTTP %s from %s: %s" % (exc.code, url, body))
    except urllib.error.URLError as exc:
        raise SystemExit("Could not reach %s: %s" % (url, exc.reason))


def _candle(d, o, h, l, c, source):
    return {"date": d, "open": o, "high": h, "low": l, "close": c, "source": source}


def fetch_bitstamp(start, end):
    """Daily candles [start, end]. Paginates; Bitstamp caps at 1000 per call."""
    out = {}
    cursor = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp())
    while cursor <= end_ts:
        page = _get(BITSTAMP, {"step": "86400", "limit": "1000", "start": str(cursor)})
        rows = page.get("data", {}).get("ohlc", [])
        if not rows:
            break
        for r in rows:
            d = _utc_date(r["timestamp"])
            if start <= d <= end:
                out[d] = _candle(d, float(r["open"]), float(r["high"]),
                                 float(r["low"]), float(r["close"]), "bitstamp")
        last = int(rows[-1]["timestamp"])
        if last <= cursor:
            break
        cursor = last + 86400
    return [out[d] for d in sorted(out)]


def fetch_coinbase(start, end):
    """Daily candles [start, end]. Coinbase caps at 300 candles per request."""
    out = {}
    lo = start
    while lo <= end:
        hi = min(lo + timedelta(days=290), end)
        rows = _get(COINBASE, {
            "granularity": "86400",
            "start": lo.isoformat() + "T00:00:00Z",
            "end": hi.isoformat() + "T00:00:00Z",
        })
        if not isinstance(rows, list):
            raise SystemExit("Unexpected Coinbase payload: %s" % str(rows)[:200])
        for r in rows:
            # [time, low, high, open, close, volume]
            d = _utc_date(r[0])
            if start <= d <= end:
                out[d] = _candle(d, float(r[3]), float(r[2]),
                                 float(r[1]), float(r[4]), "coinbase")
        lo = hi + timedelta(days=1)
    return [out[d] for d in sorted(out)]


def fetch_daily(start, end, prefer="coinbase"):
    """Coinbase first, Bitstamp as fallback. Never invent a price."""
    order = [fetch_coinbase, fetch_bitstamp] if prefer == "coinbase" else [fetch_bitstamp, fetch_coinbase]
    last_err = None
    for fn in order:
        try:
            rows = fn(start, end)
            if rows:
                return rows
        except SystemExit as exc:
            last_err = exc
    if last_err:
        raise last_err
    return []
