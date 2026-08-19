#!/bin/bash
# Wrapper invoked by launchd. Pulls the Discord webhook from the macOS login
# keychain, then runs the tracker. Passes through any arguments, so the
# watchdog job invokes this same script with --healthcheck.
#
# Same pattern as the sibling kalshi/ops/run_kalshi.sh, but with its own
# keychain service name ("btc-cycle-discord") so this tool's webhook is a
# separate entry and can point at a different channel.
#
# One-time manual setup required before this will work:
#   security add-generic-password -a btc-cycle \
#     -s btc-cycle-discord -w 'https://discord.com/api/webhooks/...'
#
# Discord is the only output here, so a missing entry is a hard failure rather
# than a silent no-op — a quiet success and a broken webhook would otherwise
# look identical.

set -u

cd /Users/mclarenx989/btc_cycle

mkdir -p logs

export DISCORD_WEBHOOK_URL="$(/usr/bin/security find-generic-password \
    -a btc-cycle -s btc-cycle-discord -w 2>/dev/null)"

if [ -z "$DISCORD_WEBHOOK_URL" ]; then
    echo "ERROR: no Discord webhook in keychain (service btc-cycle-discord)" >&2
    exit 1
fi

/usr/bin/env python3 btc_cycle.py "$@"
status=$?

# Report our own death, but only for the daily run. --healthcheck exits 1 by
# design when it finds staleness and has already posted its own alert; posting
# again here would double up.
#
# This catches failures where the process ran and the network is usable (an API
# 500, a bad payload). It CANNOT catch the case that actually bit us twice —
# no DNS at 09:10 — because the post below fails for the same reason the run
# did. That is what the separate watchdog job exists for; do not delete one
# thinking the other covers it.
if [ "$status" -ne 0 ] && [ "${1:-}" != "--healthcheck" ]; then
    /usr/bin/env python3 - "$status" <<'PY' || true
import os, sys
import report
report.post(os.environ["DISCORD_WEBHOOK_URL"], [report.alert_embed(
    "stale", "Daily run failed",
    ["Exited **%s**." % sys.argv[1],
     "",
     "The price series self-heals on the next successful run; the `snapshots` "
     "row for today does not.",
     "",
     "See `logs/launchd.err`."])])
PY
fi

exit "$status"
