#!/bin/bash
# Render the launchd plists for wherever this clone actually lives, then load
# them. launchd requires absolute paths inside the plist, so they are kept as
# templates in git and rendered here - that is the only reason this script
# exists.
#
#   ./ops/install.sh          install and load both jobs
#   ./ops/install.sh --dry    print what would be written
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DRY="${1:-}"

for name in snapshot watchdog; do
    tpl="$ROOT/ops/com.btccycle.$name.plist.template"
    out="$AGENTS/com.btccycle.$name.plist"
    rendered="$(sed "s#__PROJECT_DIR__#$ROOT#g" "$tpl")"

    if [ "$DRY" = "--dry" ]; then
        echo "--- would write $out"
        echo "$rendered" | grep -E "ProgramArguments|string>$ROOT" | head -3
        continue
    fi

    mkdir -p "$AGENTS"
    printf '%s\n' "$rendered" > "$out"
    launchctl unload "$out" 2>/dev/null || true
    launchctl load "$out"
    echo "loaded com.btccycle.$name"
done

[ "$DRY" = "--dry" ] || launchctl list | grep btccycle || true
