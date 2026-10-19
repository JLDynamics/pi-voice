"""Rules CLAUDE.md states in prose that nothing else enforces.

Each failure names where the change belongs. Matches inside comments are
ignored so the code can still explain why something was removed.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWIFT_SOURCES = ROOT / "macos" / "Voice" / "Sources"
CODE_SUFFIXES = {".py", ".swift", ".ts", ".mjs", ".sh"}
SKIP_DIRS = {".git", ".venv", "node_modules", "build", ".build", "__pycache__", "tests", ".agy-staff"}
COMMENT_PREFIXES = ("//", "#", "*", "/*")


def _code_lines(paths: list[Path]) -> list[tuple[Path, int, str]]:
    lines = []
    for path in paths:
        for number, line in enumerate(path.read_text(errors="ignore").splitlines(), start=1):
            if not line.strip().startswith(COMMENT_PREFIXES):
                lines.append((path, number, line))
    return lines


def _hits(paths: list[Path], pattern: str) -> list[str]:
    regex = re.compile(pattern)
    return [f"{p.relative_to(ROOT)}:{n}: {line.strip()}" for p, n, line in _code_lines(paths) if regex.search(line)]


def _repo_code() -> list[Path]:
    return [
        p
        for p in ROOT.rglob("*")
        if p.suffix in CODE_SUFFIXES and p.is_file() and not SKIP_DIRS.intersection(p.relative_to(ROOT).parts)
    ]


def test_voice_app_has_no_window_or_menu() -> None:
    hits = _hits(
        sorted(SWIFT_SOURCES.rglob("*.swift")),
        r"import SwiftUI|\bNSWindow\b|\bWindowGroup\b|\bMenuBarExtra\b|\bNSStatusItem\b|\bNSHostingView\b",
    )
    assert not hits, (
        "Voice.app is headless by design; show the user things in Pi (pi-voice/src), not here:\n" + "\n".join(hits)
    )


def test_only_one_call_site_starts_the_player_node() -> None:
    hits = _hits(sorted(SWIFT_SOURCES.rglob("*.swift")), r"\bplayer\.play\(\)")
    assert len(hits) == 1 and hits[0].startswith("macos/Voice/Sources/Audio/AudioEngine.swift"), (
        "AVAudioPlayerNode.play() before the first IO cycle crashes; call AudioEngine.startPlayerIfRendering() "
        "instead of player.play():\n" + "\n".join(hits)
    )


def test_no_second_local_service_on_7860() -> None:
    hits = _hits(_repo_code(), r"\b7860\b")
    assert not hits, "Pi owns web reading and memory; do not reintroduce the :7860 sidecar:\n" + "\n".join(hits)


def test_no_process_name_kills() -> None:
    hits = _hits(_repo_code(), r"\b(pkill|killall)\b")
    assert not hits, (
        "Killing by process name can take down another session's services; stop the PID or launcher you started "
        "(scripts/service_state.py):\n" + "\n".join(hits)
    )
