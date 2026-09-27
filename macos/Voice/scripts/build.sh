#!/usr/bin/env bash
# Build Voice.app from Sources/ without requiring an Xcode project.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="${1:-$ROOT/build/Voice.app}"
BIN="$OUT/Contents/MacOS/Voice"
SDK="$(xcrun --sdk macosx --show-sdk-path)"
ARCH="$(uname -m)"
TARGET="${ARCH}-apple-macos14.0"

SOURCES=()
while IFS= read -r f; do
  SOURCES+=("$f")
done < <(find "$ROOT/Sources" -name '*.swift' | sort)

rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
# Keep this build tree out of Spotlight/Launchpad so only /Applications/Voice.app shows.
touch "$(dirname "$OUT")/.metadata_never_index"
cp "$ROOT/Resources/Info.plist" "$OUT/Contents/Info.plist"
cp "$ROOT/Resources/Voice.entitlements" "$OUT/Contents/Resources/Voice.entitlements"
printf '%s\n' "$(cd "$ROOT/../.." && pwd)" > "$OUT/Contents/Resources/RepositoryPath.txt"

embed_app_icon() {
  local logo="$ROOT/../../logo.png"
  [[ -f "$logo" ]] || return 0
  local work="$ROOT/build/iconwork"
  rm -rf "$work"
  mkdir -p "$work/Voice.iconset"
  local square="$work/square.png"
  local w h side
  w="$(sips -g pixelWidth "$logo" | awk '/pixelWidth/ {print $2}')"
  h="$(sips -g pixelHeight "$logo" | awk '/pixelHeight/ {print $2}')"
  side="$w"
  [[ "$h" -gt "$w" ]] && side="$h"
  sips --padToHeightWidth "$side" "$side" "$logo" --out "$square" >/dev/null
  local iconset="$work/Voice.iconset"
  sips -z 16 16     "$square" --out "$iconset/icon_16x16.png" >/dev/null
  sips -z 32 32     "$square" --out "$iconset/icon_16x16@2x.png" >/dev/null
  sips -z 32 32     "$square" --out "$iconset/icon_32x32.png" >/dev/null
  sips -z 64 64     "$square" --out "$iconset/icon_32x32@2x.png" >/dev/null
  sips -z 128 128   "$square" --out "$iconset/icon_128x128.png" >/dev/null
  sips -z 256 256   "$square" --out "$iconset/icon_128x128@2x.png" >/dev/null
  sips -z 256 256   "$square" --out "$iconset/icon_256x256.png" >/dev/null
  sips -z 512 512   "$square" --out "$iconset/icon_256x256@2x.png" >/dev/null
  sips -z 512 512   "$square" --out "$iconset/icon_512x512.png" >/dev/null
  sips -z 1024 1024 "$square" --out "$iconset/icon_512x512@2x.png" >/dev/null
  iconutil -c icns "$iconset" -o "$OUT/Contents/Resources/AppIcon.icns"
  rm -rf "$work"
}
embed_app_icon

echo "Compiling ${#SOURCES[@]} Swift files → $BIN"
xcrun swiftc \
  -sdk "$SDK" \
  -target "$TARGET" \
  -parse-as-library \
  -O \
  -framework SwiftUI \
  -framework AppKit \
  -framework AVFoundation \
  -framework Carbon \
  "${SOURCES[@]}" \
  -o "$BIN"

# A persistent identity keeps Screen Recording / mic grants valid across
# rebuilds. Ad-hoc signing gets a new TCC identity every binary.
if [[ -n "${VOICE_SIGNING_IDENTITY:-}" ]]; then
  SIGNING_IDENTITY="$VOICE_SIGNING_IDENTITY"
else
  SIGNING_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null | awk -F'"' '/Voice Local Signing/ {print $2; exit}')"
  SIGNING_IDENTITY="${SIGNING_IDENTITY:--}"
fi
codesign --force --deep --sign "$SIGNING_IDENTITY" \
  --entitlements "$ROOT/Resources/Voice.entitlements" \
  "$OUT"

if [[ "$SIGNING_IDENTITY" == "-" ]]; then
  echo "Local ad-hoc build: macOS may ask for permissions again after rebuilding."
  echo "Set VOICE_SIGNING_IDENTITY to an installed signing identity to preserve grants across builds."
else
  echo "Signed with $SIGNING_IDENTITY (Screen Recording grants survive rebuilds)."
fi

echo "Built $OUT"
echo "Launch via Pi's /voice (opening by hand without stdin quits)."
echo "Install in Applications: $HERE/install.sh --skip-build"
