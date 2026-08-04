#!/bin/bash
# Wrapper invoked by launchd. Pulls the Discord webhook from the macOS login
# keychain, then runs the daily snapshot.
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

set -e

cd /Users/mclarenx989/btc_cycle

mkdir -p logs

export DISCORD_WEBHOOK_URL="$(/usr/bin/security find-generic-password \
    -a btc-cycle -s btc-cycle-discord -w 2>/dev/null)"

if [ -z "$DISCORD_WEBHOOK_URL" ]; then
    echo "ERROR: no Discord webhook in keychain (service btc-cycle-discord)" >&2
    exit 1
fi

exec /usr/bin/env python3 btc_cycle.py "$@"
