"""A running service can tell when the code it loaded no longer matches disk."""

from __future__ import annotations

from pathlib import Path

import pytest

from chatbot import build_info
from chatbot.build_info import BACKEND_SOURCES, SourceSnapshot, repository_root, source_fingerprint


def _checkout(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    package = tmp_path / "src" / "chatbot"
    package.mkdir(parents=True)
    (package / "a.py").write_text("A = 1\n")
    (package / "b.py").write_text("B = 2\n")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"\x00")
    (tmp_path / "run-openrouter.sh").write_text("echo hi\n")
    return tmp_path


def test_fingerprint_is_stable_and_ignores_non_source_files(tmp_path):
    root = _checkout(tmp_path)
    first = source_fingerprint(root, BACKEND_SOURCES)
    assert first and len(first) == 16
    (root / "src" / "chatbot" / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"\x01\x02")
    (root / "src" / "chatbot" / "notes.txt").write_text("not code\n")
    assert source_fingerprint(root, BACKEND_SOURCES) == first


@pytest.mark.parametrize("edit", ["src/chatbot/a.py", "run-openrouter.sh", "pyproject.toml"])
def test_fingerprint_changes_when_any_backend_source_changes(tmp_path, edit):
    root = _checkout(tmp_path)
    before = source_fingerprint(root, BACKEND_SOURCES)
    (root / edit).write_text("changed\n")
    assert source_fingerprint(root, BACKEND_SOURCES) != before


def test_fingerprint_changes_when_a_source_file_is_added_or_removed(tmp_path):
    root = _checkout(tmp_path)
    before = source_fingerprint(root, BACKEND_SOURCES)
    (root / "src" / "chatbot" / "c.py").write_text("C = 3\n")
    added = source_fingerprint(root, BACKEND_SOURCES)
    assert added != before
    (root / "src" / "chatbot" / "b.py").unlink()
    assert source_fingerprint(root, BACKEND_SOURCES) not in {before, added}


def test_the_backend_watches_only_its_own_tree(tmp_path):
    root = _checkout(tmp_path)
    (root / "unrelated").mkdir()
    (root / "unrelated" / "server.py").write_text("app = 1\n")
    backend_before = source_fingerprint(root, BACKEND_SOURCES)
    (root / "unrelated" / "server.py").write_text("app = 2\n")
    assert source_fingerprint(root, BACKEND_SOURCES) == backend_before
    # The launcher and lockfile are part of the backend.
    (root / "pyproject.toml").write_text("[project]\nname = 'y'\n")
    assert source_fingerprint(root, BACKEND_SOURCES) != backend_before


def test_snapshot_reports_stale_once_disk_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(SourceSnapshot, "RECHECK_INTERVAL_S", 0.0)
    root = _checkout(tmp_path)
    snapshot = SourceSnapshot(BACKEND_SOURCES, root=root)
    assert snapshot.stale() is False
    (root / "src" / "chatbot" / "a.py").write_text("A = 2\n")
    assert snapshot.stale() is True
    description = snapshot.describe()
    assert description["fingerprint"] == snapshot.started_fingerprint
    assert description["stale"] is True
    assert description["source"] == str(root)
    assert description["pid"] > 0
    assert isinstance(description["version"], str)


def test_snapshot_rechecks_disk_at_most_once_per_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(SourceSnapshot, "RECHECK_INTERVAL_S", 3600.0)
    root = _checkout(tmp_path)
    snapshot = SourceSnapshot(BACKEND_SOURCES, root=root)
    (root / "src" / "chatbot" / "a.py").write_text("A = 2\n")
    # Within the interval the cached answer stands; polling stays cheap.
    assert snapshot.stale() is False
    snapshot._checked_at -= 3600.0
    assert snapshot.stale() is True


def test_snapshot_without_a_checkout_cannot_judge_staleness(tmp_path):
    snapshot = SourceSnapshot(BACKEND_SOURCES, root=None)
    assert snapshot.started_fingerprint is None
    assert snapshot.stale() is None
    assert snapshot.describe()["source"] is None
    assert source_fingerprint(None, BACKEND_SOURCES) is None


def test_repository_root_is_this_checkout():
    root = repository_root()
    assert root is not None
    assert (root / "pyproject.toml").is_file()
    assert (root / "src" / "chatbot" / "build_info.py").samefile(Path(build_info.__file__))
