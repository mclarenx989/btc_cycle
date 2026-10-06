#!/bin/bash
# Wrapper invoked by launchd. Pulls the Discord webhook from the macOS login
# keychain, waits for DNS, then runs the tracker. Arguments pass through, so
# the watchdog job invokes this same script with --healthcheck.
#
# One-time manual setup:
#   security add-generic-password -a btc-cycle \
#     -s btc-cycle-discord -w 'https://discord.com/api/webhooks/...'

set -u

# Resolve the project root from this script's own location rather than a
# baked-in path, so a clone works anywhere.
cd "$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p logs

export DISCORD_WEBHOOK_URL="$(/usr/bin/security find-generic-password \
    -a btc-cycle -s btc-cycle-discord -w 2>/dev/null)"

if [ -z "$DISCORD_WEBHOOK_URL" ]; then
    echo "ERROR: no Discord webhook in keychain (service btc-cycle-discord)" >&2
    exit 1
fi

# Wait for DNS. launchd fires at 09:10, or immediately on wake if the machine
# slept through the slot, and the network stack may not be up yet. The failure
# is instant ("nodename nor servname provided"), not a timeout, so the run dies
# in under a second and the day is lost. 9 of the first 44 days went exactly
# this way. Same 10-minute poll as ~/a sibling tracker's wrapper.
#
# discord.com is the host checked because delivery is the thing that must work:
# price data has a two-source fallback, the alert has none.
for attempt in $(seq 1 20); do
    if /usr/bin/env python3 -c \
        "import socket,sys; sys.exit(0 if socket.getaddrinfo('discord.com', 443) else 1)" \
        2>/dev/null; then
        break
    fi
    if [ "$attempt" -eq 20 ]; then
        # Do NOT just exit. Queue the failure locally (a DB write, no network)
        # so the next run that gets through reports this day instead of it
        # vanishing. the sibling's version exits here and loses the day.
        echo "ERROR: DNS for discord.com never resolved (10 min)" >&2
        /usr/bin/env python3 - <<'PY' || true
import btc_cycle as bc, report
conn = bc.connect()
bc.queue_post(conn, [report.alert_embed(
    "stale", "Run aborted — no DNS",
    ["Waited 10 minutes for `discord.com` to resolve and gave up.",
     "",
     "Queued locally; you are seeing it because a later run got through."])])
print("queued no-DNS alert")
PY
        exit 1
    fi
    sleep 30
done

/usr/bin/env python3 btc_cycle.py "$@"
status=$?

# Non-zero from the daily run: queue a failure notice. --healthcheck exits 1 by
# design when stale and has already queued/sent its own alert, so skip it.
if [ "$status" -ne 0 ] && [ "${1:-}" != "--healthcheck" ]; then
    /usr/bin/env python3 - "$status" <<'PY' || true
import sys
import btc_cycle as bc, report
conn = bc.connect()
embeds = [report.alert_embed("stale", "Daily run failed",
    ["Exited **%s**." % sys.argv[1], "",
     "See `logs/launchd.err`."])]
hook = __import__("os").environ["DISCORD_WEBHOOK_URL"]
bc.flush_pending(conn, hook)
bc.deliver(conn, hook, embeds)
PY
fi

exit "$status"
