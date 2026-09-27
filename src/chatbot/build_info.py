"""Tell a running local service apart from the code on disk.

Voice.app reuses whatever already listens on the local ports. That is right
when the process is current, and wrong the moment the code changes under it:
a merge or an agent edit lands on disk, the old process keeps serving the old
behavior, and the app that opens next talks to a backend that no longer
matches its own contract (this is how "search runs on the server" met a
server that had never heard of server-side tools).

Each service records the fingerprint of the sources it runs from when it
starts and reports, on its health endpoint, whether that fingerprint still
matches the files on disk. The launcher and the app read that one flag and
restart a stale service instead of reusing it.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Sequence
from pathlib import Path

# The realtime voice backend: its package, the scripts that launch it, and the
# locked environment (a dependency added for a fix is as much "the code" as
# the file that calls it).
BACKEND_SOURCES: tuple[str, ...] = ("src/chatbot", "run-openrouter.sh", "run-browser.sh", "pyproject.toml", "uv.lock")
# Only these count as source; caches, logs and compiled files change on their own.
SOURCE_SUFFIXES: frozenset[str] = frozenset({".py", ".sh", ".toml", ".lock"})


def repository_root() -> Path | None:
    """The checkout this package was imported from, or None for a wheel install."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() else None


def _source_files(root: Path, sources: Sequence[str]) -> list[Path]:
    files: list[Path] = []
    for entry in sources:
        path = root / entry
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file() and candidate.suffix in SOURCE_SUFFIXES and "__pycache__" not in candidate.parts
            )
    return sorted(files)


def source_fingerprint(root: Path | None, sources: Sequence[str]) -> str | None:
    """A short content hash of *sources* under *root*; None when there is no checkout.

    Content, not modification time: a git checkout that rewrites identical
    files must not look like a change, and an edit must never hide behind a
    preserved timestamp.
    """
    if root is None:
        return None
    digest = hashlib.sha256()
    for file in _source_files(root, sources):
        digest.update(str(file.relative_to(root)).encode())
        digest.update(b"\0")
        try:
            digest.update(file.read_bytes())
        except OSError:
            # A file removed between listing and reading still changes the tree.
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()[:16]


class _DetectRoot:
    """Default for ``SourceSnapshot(root=...)``: locate the checkout ourselves."""


DETECT_ROOT = _DetectRoot()


class SourceSnapshot:
    """The fingerprint a service started with, and whether it still matches disk."""

    # Health endpoints are polled several times a second while a client waits
    # for models to load; rehashing the tree that often buys nothing.
    RECHECK_INTERVAL_S = 1.0

    def __init__(self, sources: Sequence[str], root: Path | None | _DetectRoot = DETECT_ROOT) -> None:
        self.root = repository_root() if isinstance(root, _DetectRoot) else root
        self.sources = tuple(sources)
        self.started_fingerprint = source_fingerprint(self.root, self.sources)
        self._checked_at = time.monotonic()
        self._current = self.started_fingerprint

    def current_fingerprint(self) -> str | None:
        now = time.monotonic()
        if now - self._checked_at >= self.RECHECK_INTERVAL_S:
            self._current = source_fingerprint(self.root, self.sources)
            self._checked_at = now
        return self._current

    def stale(self) -> bool | None:
        """True when the code on disk differs from what this process loaded.

        None when the package was not imported from a checkout (nothing to
        compare against), so callers can tell "current" from "unknowable".
        """
        if self.started_fingerprint is None:
            return None
        return self.current_fingerprint() != self.started_fingerprint

    def describe(self) -> dict[str, object]:
        """Health-endpoint fields shared by the backend and the sidecar."""
        from chatbot import __version__

        return {
            "version": __version__,
            "pid": os.getpid(),
            "source": str(self.root) if self.root is not None else None,
            "fingerprint": self.started_fingerprint,
            "stale": self.stale(),
        }
