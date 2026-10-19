"""Temp directories belong under the OS temp dir, never the working directory."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "node_modules", "build", ".build", "__pycache__"}


def test_temp_dirs_are_created_under_tmpdir() -> None:
    hits = []
    for path in ROOT.rglob("*"):
        if path.suffix not in {".ts", ".mjs"} or SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            continue
        text = path.read_text(errors="ignore")
        names = {"mkdtempSync", *re.findall(r"mkdtempSync:\s*(\w+)", text)}
        pattern = re.compile(rf"\b(?:{'|'.join(names)})\(\s*(?!join\(tmpdir\(\)|tmpdir\(\))")
        hits += [f"{path.relative_to(ROOT)}: {m.group(0)}" for m in pattern.finditer(text)]
    assert not hits, (
        "mkdtempSync with a bare prefix creates the directory in the current working directory, which litters the "
        'repo root; use mkdtempSync(join(tmpdir(), "prefix-")):\n' + "\n".join(hits)
    )
