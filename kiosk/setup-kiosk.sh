#!/bin/bash
# Setup script for Raspberry Pi kiosk mode
# Run once: sudo bash setup-kiosk.sh
#
# Prerequisites:
#   - Raspberry Pi OS with desktop (or Lite + X11)
#   - Chromium browser installed (apt install chromium-browser)
#   - Dashboard running on localhost:5000

set -e

KIOSK_USER="${SUDO_USER:-pi}"
SERVICE_DIR="/home/$KIOSK_USER/.config/systemd/user"

echo "Setting up kiosk mode for user: $KIOSK_USER"

# Install dependencies if needed
apt-get install -y --no-install-recommends \
    chromium-browser \
    xdotool \
    unclutter 2>/dev/null || true

# Create systemd user service
mkdir -p "$SERVICE_DIR"
cat > "$SERVICE_DIR/solar-kiosk.service" << 'EOF'
[Unit]
Description=Solar Dashboard Kiosk (Chromium)
After=graphical-session.target
Wants=graphical-session.target

[Service]
Type=simple
Environment=DISPLAY=:0
ExecStartPre=/bin/sleep 5
ExecStart=/usr/bin/chromium-browser \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-translate \
    --disable-features=TranslateUI \
    --disable-component-update \
    --check-for-update-interval=31536000 \
    --autoplay-policy=no-user-gesture-required \
    --no-first-run \
    --start-fullscreen \
    --window-size=800,480 \
    http://localhost:5000/?kiosk=1
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

# Enable lingering so user services start at boot
loginctl enable-linger "$KIOSK_USER"

echo ""
echo "Done! To enable:"
echo "  systemctl --user enable solar-kiosk.service"
echo "  systemctl --user start solar-kiosk.service"
echo ""
echo "To disable:"
echo "  systemctl --user stop solar-kiosk.service"
echo "  systemctl --user disable solar-kiosk.service"
echo ""
echo "Screen will show: http://localhost:5000/?kiosk=1"
