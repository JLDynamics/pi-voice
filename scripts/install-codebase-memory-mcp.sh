#!/usr/bin/env bash
# Install DeusData codebase-memory-mcp into THIS checkout only.
# Does not write ~/.cursor/mcp.json or any other global agent config.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="$ROOT/tools/codebase-memory-mcp"
INSTALLER_URL="https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh"

mkdir -p "$DEST_DIR" "$ROOT/.cursor"
echo "Installing codebase-memory-mcp into $DEST_DIR (skip agent auto-config)"
curl -fsSL "$INSTALLER_URL" | bash -s -- --dir "$DEST_DIR" --skip-config

# Official installer still appends this dir to ~/.zshrc PATH even with --skip-config.
# Keep the binary project-local: strip that PATH line if we just added it.
if [[ -f "$HOME/.zshrc" ]]; then
  DEST_DIR="$DEST_DIR" python3 - <<'PY'
import os
from pathlib import Path
dest = os.environ["DEST_DIR"]
p = Path.home() / ".zshrc"
text = p.read_text()
needle = f'\n# Added by codebase-memory-mcp install\nexport PATH="{dest}:$PATH"\n'
if needle in text:
    p.write_text(text.replace(needle, "\n"))
    print("Removed installer PATH line from ~/.zshrc (project-local only)")
PY
fi

# Cursor does not expand ${workspaceFolder} in MCP env values. CBM treats a
# literal '${workspaceFolder}' as CBM_ALLOWED_ROOT and exits with
# "daemon session context was rejected". Use absolute paths.
cat > "$ROOT/.cursor/mcp.json" <<EOF
{
  "mcpServers": {
    "codebase-memory": {
      "type": "stdio",
      "command": "$DEST_DIR/codebase-memory-mcp",
      "args": [],
      "env": {
        "CBM_ALLOWED_ROOT": "$ROOT"
      }
    }
  }
}
EOF

echo "Wrote $ROOT/.cursor/mcp.json (Cursor project MCP; not global)"
echo "Binary: $DEST_DIR/codebase-memory-mcp"
echo "Enable it in Cursor Settings → MCP, then click Refresh (or reload the window)."
