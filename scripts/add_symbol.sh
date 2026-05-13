#!/usr/bin/env bash
# Add a second (or Nth) symbol collector as a separate systemd service instance.
#
# Usage:
#   bash scripts/add_symbol.sh ETHUSDT
#   bash scripts/add_symbol.sh SOLUSDT

set -euo pipefail

SYMBOL="${1:?Usage: $0 <SYMBOL>}"
SYMBOL_UPPER="${SYMBOL^^}"

TEMPLATE_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/collector@.service"

if [ ! -f "${TEMPLATE_SRC}" ]; then
    echo "ERROR: template unit not found: ${TEMPLATE_SRC}" >&2
    exit 1
fi

# Install template unit if not already present
if [ ! -f /etc/systemd/system/collector@.service ]; then
    echo "Installing template unit collector@.service..."
    sudo cp "${TEMPLATE_SRC}" /etc/systemd/system/collector@.service
    sudo systemctl daemon-reload
fi

# Enable and start the instance
echo "Starting collector@${SYMBOL_UPPER}..."
sudo systemctl enable --now "collector@${SYMBOL_UPPER}.service"

echo ""
echo "Status:"
sudo systemctl status "collector@${SYMBOL_UPPER}.service" --no-pager || true
echo ""
echo "Logs: sudo journalctl -u collector@${SYMBOL_UPPER} -f"
