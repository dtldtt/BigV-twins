#!/usr/bin/env bash
# Install BigV-twins user-level systemd units.
# Idempotent: safe to re-run after editing the units in this directory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

echo "==> ensuring linger is enabled for ${USER} (so user units run at boot)"
sudo loginctl enable-linger "$USER"

echo "==> installing units to ${UNIT_DIR}"
mkdir -p "$UNIT_DIR"
cp "$SCRIPT_DIR/bigv-twins-server.service" "$UNIT_DIR/"
cp "$SCRIPT_DIR/bigv-twins-daily.service"  "$UNIT_DIR/"
cp "$SCRIPT_DIR/bigv-twins-daily.timer"    "$UNIT_DIR/"

echo "==> systemctl --user daemon-reload"
systemctl --user daemon-reload

echo "==> enabling units"
systemctl --user enable bigv-twins-server.service
systemctl --user enable bigv-twins-daily.timer

echo
echo "Installed. Useful commands:"
echo "  systemctl --user start  bigv-twins-server.service"
echo "  systemctl --user stop   bigv-twins-server.service"
echo "  systemctl --user status bigv-twins-server.service"
echo "  systemctl --user list-timers --all | grep bigv-twins"
echo "  journalctl --user -u bigv-twins-server.service -f"
