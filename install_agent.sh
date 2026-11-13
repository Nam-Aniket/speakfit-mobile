#!/bin/bash
# Keeps server.py running in the background (starts at login, restarts if it dies).
# Undo: launchctl bootout gui/$(id -u)/com.aniket.speakfit-mobile && rm ~/Library/LaunchAgents/com.aniket.speakfit-mobile.plist
set -euo pipefail
cd "$(dirname "$0")"; DIR="$PWD"
PLIST="$HOME/Library/LaunchAgents/com.aniket.speakfit-mobile.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.aniket.speakfit-mobile</string>
  <key>ProgramArguments</key><array><string>$DIR/.venv/bin/python</string><string>-u</string><string>$DIR/server.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/data/server.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/server.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/com.aniket.speakfit-mobile" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. Log: $DIR/data/server.log"
