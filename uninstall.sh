#!/bin/sh
# Stops the remote and removes it from login. Leaves this folder in place.
set -eu
LABEL=local.monitor-remote
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Remote stopped and removed from login. Delete this folder to remove it completely."
