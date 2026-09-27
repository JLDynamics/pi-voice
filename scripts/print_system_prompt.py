#!/usr/bin/env python3
"""Print the assembled voice system prompt the model actually receives."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# E402 is intentional here: the bootstrap above must run before this import so
# the script works as `python3 scripts/print_system_prompt.py` without install.
from chatbot.LLM.voice_prompt import build_voice_system_prompt  # noqa: E402

DEFAULT_PERSONA = (
    "You are an AI conversation partner: perceptive, relaxed, warm, and quietly "
    "playful. You enjoy exploring ideas and have something thoughtful to contribute. "
    "Speak with the ease of someone comfortable in the conversation."
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--persona",
        default=DEFAULT_PERSONA,
        help="Session persona (defaults to the Voice.app conversation-partner prompt)",
    )
    args = parser.parse_args()
    prompt = build_voice_system_prompt(args.persona)
    print(prompt)
    print(f"\n--- {len(prompt)} chars, {len(prompt.split())} words ---", file=sys.stderr)
    if "perceptive, relaxed, warm, and quietly playful" not in prompt:
        raise SystemExit("personality missing from assembled prompt")
    if "Speech is the default" not in prompt:
        raise SystemExit("tool voice rules missing from assembled prompt")


if __name__ == "__main__":
    main()
