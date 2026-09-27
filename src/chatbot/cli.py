from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Literal

Command = Literal["serve"]


def parse_command(argv: Sequence[str] | None = None) -> tuple[Command, list[str]]:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="chatbot",
        description="Run the Mac-first live voice backend.",
    )
    parser.add_argument("command", choices=("serve",))
    if not args or args[0] in {"-h", "--help"}:
        parser.parse_args(args)
    if args[0] != "serve":
        parser.error(f"unknown command {args[0]!r}; choose serve")
    return "serve", args[1:]


def main() -> None:
    _, command_args = parse_command()
    from chatbot.s2s_pipeline import run_pipeline_command

    run_pipeline_command("serve", command_args)


if __name__ == "__main__":
    main()
