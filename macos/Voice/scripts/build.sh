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

echo "Compiling ${#SOURCES[@]} Swift files → $BIN"
xcrun swiftc \
  -sdk "$SDK" \
  -target "$TARGET" \
  -parse-as-library \
  -O \
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
