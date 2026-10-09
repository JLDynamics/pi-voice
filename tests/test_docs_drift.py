"""CLAUDE.md is loaded into every agent session, so it must stay small and must
not point at files that no longer exist. The architecture docs it links to get
the same reference check."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "CLAUDE.md", *sorted((ROOT / "docs" / "architecture").glob("*.md"))]
SKIP_DIRS = {".git", ".venv", "node_modules", ".build", "build", "__pycache__", ".agy-staff"}
CLAUDE_MD_BUDGET = 10 * 1024
# Backend docs name modules relative to the package root.
BASES = [ROOT, ROOT / "src" / "chatbot"]
# Files that exist only at runtime (under the user's config or temp dirs).
RUNTIME_FILES = {"session.json"}


def _repo_file_names() -> set[str]:
    names: set[str] = set()
    for path in ROOT.rglob("*"):
        if not SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            names.add(path.name)
    return names


def _referenced_paths(text: str) -> set[str]:
    tokens = set(re.findall(r"`([^`\s]+)`", text))
    tokens |= set(re.findall(r"\]\(([^)#\s]+)\)", text))
    return {
        t.rstrip(".,;:")
        for t in tokens
        if re.search(r"/|\.(py|swift|ts|mjs|sh|md|json)$", t) and not re.search(r"[<>*~$]|^/|^-|://|^:|^\.\.\.", t)
    }


def test_claude_md_stays_under_budget() -> None:
    size = (ROOT / "CLAUDE.md").stat().st_size
    assert size <= CLAUDE_MD_BUDGET, f"CLAUDE.md is {size} bytes; move detail to docs/architecture/"


def test_doc_references_exist() -> None:
    names = _repo_file_names()
    missing = []
    for doc in DOCS:
        for ref in sorted(_referenced_paths(doc.read_text())):
            rel = ref[2:] if ref.startswith("./") else ref
            if rel in RUNTIME_FILES:
                continue
            if "/" in rel.rstrip("/"):
                ok = any((base / rel).exists() for base in [*BASES, doc.parent])
            elif rel.startswith("+"):  # `+Tools.swift` = an extension file such as LiveVoiceBackend+Tools.swift
                ok = any(name.endswith(rel) for name in names)
            else:
                ok = rel.rstrip("/") in names
            if not ok:
                missing.append(f"{doc.relative_to(ROOT)}: {ref}")
    assert not missing, "stale references:\n" + "\n".join(missing)


def test_hot_files_stay_split() -> None:
    """Plan D6: the files agents touch most stay under 600 lines (U8/U9 splits)."""
    hot = [
        *(ROOT / "macos/Voice/Sources/Session").glob("LiveVoiceBackend*.swift"),
        *(p for p in (ROOT / "pi-voice/src").glob("voice*.ts") if not p.name.endswith(".test.ts")),
    ]
    assert len(hot) >= 2
    too_long = {p.name: n for p in hot if (n := len(p.read_text().splitlines())) > 600}
    assert not too_long, f"split these before they grow further: {too_long}"
