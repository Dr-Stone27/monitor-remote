#!/bin/sh
# Builds the ddc tool, enrols your monitor and starts the remote at login.
# Safe to run again after changing config.json.
set -eu

cd "$(dirname "$0")"
DIR=$(pwd)
LABEL=local.monitor-remote
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "$(uname -m)" != "arm64" ]; then
  echo "This needs an Apple Silicon Mac (M1 or later)."; exit 1
fi
case "$DIR" in
  "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
    echo "macOS blocks background items from Documents, Desktop and Downloads."
    echo "Move this folder somewhere else (for example ~/monitor-remote) and run ./install.sh again."; exit 1;;
esac
if ! command -v clang >/dev/null 2>&1; then
  echo "Needs Apple's command line tools. Install them with: xcode-select --install"; exit 1
fi
if [ ! -f config.json ]; then
  echo "No config.json yet. Start from a profile, for example:"
  echo "  cp profiles/generic.json config.json"
  ls profiles; exit 1
fi

python3 -I tools.py check
clang -O2 -Wall -o ddc ddc.c -framework IOKit -framework CoreFoundation
[ -f .monitor-id ] || python3 -I tools.py enroll
chmod 700 "$DIR"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>-I</string><string>$DIR/server.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <!-- Background priority stretches the short pauses DDC needs and makes every tap slow -->
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$DIR/server.out</string>
  <key>StandardErrorPath</key><string>$DIR/server.out</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 3
python3 -I tools.py link
echo
echo "If macOS asks whether Python may accept incoming connections, choose Allow."
