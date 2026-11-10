#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Kieran Cox
# Install netwatch for the current user: systemd user timers + tray-icon autostart.
# Safe to re-run. Remove with: ./install.sh --uninstall
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNITS="$HOME/.config/systemd/user"
AUTOSTART="$HOME/.config/autostart/netwatch-indicator.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl --user disable --now netwatch-ping.timer netwatch-bw.timer 2>/dev/null || true
    rm -f "$UNITS"/netwatch-{ping,bw}.{service,timer} "$AUTOSTART"
    pkill -f netwatch_indicator.py 2>/dev/null || true
    systemctl --user daemon-reload
    echo "netwatch removed (data kept in ~/.local/share/netwatch)"
    exit 0
fi

mkdir -p "$UNITS" "$HOME/.config/autostart" "$HOME/.local/share/netwatch"
# units use %h, so copying is enough; the scripts stay in $HERE
if [[ "$HERE" != "$HOME/dev/netwatch" ]]; then
    sed "s#%h/dev/netwatch#$HERE#g" "$HERE"/systemd/netwatch-ping.service > "$UNITS/netwatch-ping.service"
    sed "s#%h/dev/netwatch#$HERE#g" "$HERE"/systemd/netwatch-bw.service > "$UNITS/netwatch-bw.service"
else
    cp "$HERE"/systemd/netwatch-{ping,bw}.service "$UNITS"/
fi
cp "$HERE"/systemd/netwatch-{ping,bw}.timer "$UNITS"/

cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Netwatch light
Comment=Connection speed/latency light in the top bar
Exec=/usr/bin/python3 $HERE/netwatch_indicator.py
X-GNOME-Autostart-enabled=true
EOF

systemctl --user daemon-reload
systemctl --user enable --now netwatch-ping.timer netwatch-bw.timer
systemctl --user restart netwatch-ping.timer netwatch-bw.timer   # pick up changed timer files on re-runs
echo "installed. Timers:"
systemctl --user list-timers 'netwatch-*' --no-pager
echo
echo "Start the light now with:  setsid python3 $HERE/netwatch_indicator.py >/dev/null 2>&1 &"
