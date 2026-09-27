#!/usr/bin/env bash
# Copy Voice.app into /Applications so Finder, Launchpad, and Spotlight see it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
APP="$ROOT/build/Voice.app"
DEST="${VOICE_INSTALL_DIR:-/Applications/Voice.app}"

if [[ "${1:-}" != "--skip-build" ]]; then
  "$HERE/build.sh"
fi
if [[ ! -d "$APP" ]]; then
  echo "Voice.app is missing. Run $HERE/build.sh first." >&2
  exit 1
fi

if pgrep -f "/Applications/Voice.app/Contents/MacOS/Voice|$DEST/Contents/MacOS/Voice" >/dev/null 2>&1; then
  osascript -e 'tell application "Voice" to quit' >/dev/null 2>&1 || true
  sleep 1
fi

ditto "$APP" "$DEST"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST" >/dev/null 2>&1 || true
echo "Installed $DEST"
echo "Open from Applications, Spotlight (Cmd+Space, type Voice), or: open \"$DEST\""
