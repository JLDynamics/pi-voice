#!/usr/bin/env bash
#
# Store API keys for this project, permanently and privately.
#
#   ./set-keys.sh
#
# Keys are typed into a hidden prompt (nothing shows on screen), written to
# ~/.config/chatbot/env with owner-only permissions, and picked up
# automatically by run-openrouter.sh and run-browser.sh.
#
# Why a separate file rather than ~/.zshrc:
#   - chmod 600, so only your account can read it (a .zshrc is world-readable
#     by default on most setups)
#   - keys are not exported into every program you run, only into this project
#   - one place to look, one file to delete if you ever want them gone
#
# Nothing is echoed, and because the key is read by the script rather than
# typed as a command, it never lands in your shell history either.

set -euo pipefail

ENV_DIR="$HOME/.config/chatbot"
ENV_FILE="$ENV_DIR/env"

mkdir -p "$ENV_DIR"
chmod 700 "$ENV_DIR"
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Read an existing value so "keep current" is possible without showing it.
current_of() {
  grep -E "^export $1=" "$ENV_FILE" 2>/dev/null | tail -1 | sed "s/^export $1=//" | tr -d "'\"" || true
}

# Replace (or add) one export line, preserving the rest of the file.
set_key() {
  local name="$1" value="$2" tmp
  tmp="$(mktemp)"
  grep -vE "^export $name=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
  printf "export %s='%s'\n" "$name" "$value" >> "$tmp"
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

# Strip anything pasted around the key itself: a leading `export NAME=`, quotes,
# stray whitespace. People paste the line they were given, not the bare value.
clean() {
  printf '%s' "$1" \
    | sed -E 's/^[[:space:]]*(export|set)[[:space:]]+//' \
    | sed -E 's/^[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=[[:space:]]*//' \
    | tr -d "'\"" \
    | xargs 2>/dev/null || printf '%s' "$1"
}

prompt_for() {
  local name="$1" label="$2" expected_prefix="$3" where="$4"
  local existing entered
  existing="$(current_of "$name")"

  echo
  echo "── $label ─────────────────────────────────"
  if [[ -n "$existing" ]]; then
    echo "   currently set (${#existing} chars, starts '${existing:0:8}')"
    echo "   press Enter to keep it, or paste a new key to replace"
  else
    echo "   not set yet — get one at $where"
    echo "   press Enter to skip"
  fi
  printf "   paste key (hidden): "
  read -rs entered
  echo

  [[ -z "$entered" ]] && { echo "   → unchanged"; return; }

  entered="$(clean "$entered")"
  if [[ -n "$expected_prefix" && "$entered" != $expected_prefix* ]]; then
    echo "   ! That does not look like a $label — expected it to start with '$expected_prefix'."
    echo "   ! Nothing saved. Run this again to retry."
    return
  fi
  set_key "$name" "$entered"
  echo "   → saved (${#entered} chars)"
}

echo "Keys are stored in $ENV_FILE (owner-only, chmod 600)."
echo "Nothing you type is shown on screen or kept in shell history."

prompt_for OPENROUTER_API_KEY "OpenRouter key (required)" "sk-or-v1-" "https://openrouter.ai/keys"

# Verify the OpenRouter key actually works, so a typo surfaces now rather than
# seven minutes into a model load.
echo
KEY="$(current_of OPENROUTER_API_KEY)"
if [[ -n "$KEY" ]]; then
  printf "Checking the OpenRouter key... "
  CODE="$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $KEY" https://openrouter.ai/api/v1/key || echo 000)"
  case "$CODE" in
    200) echo "valid ✓" ;;
    401) echo "REJECTED (401). The key is wrong, revoked, or incomplete." ;;
    000) echo "could not reach OpenRouter (offline?). Key saved anyway." ;;
    *)   echo "unexpected response $CODE. Key saved; try running the app." ;;
  esac
fi

echo
echo "Done. Just run ./run-browser.sh — the launchers read this file themselves."
echo "To see what is stored:  cat $ENV_FILE"
echo "To remove everything:   rm $ENV_FILE"
