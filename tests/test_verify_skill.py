"""Regression tests for the verify-pi-voice harness (.cursor/skills/verify-pi-voice/scripts/pv.py)."""

import importlib.util
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PV_PATH = Path(__file__).resolve().parents[1] / ".cursor/skills/verify-pi-voice/scripts/pv.py"


@pytest.fixture(scope="module")
def pv():
    spec = importlib.util.spec_from_file_location("pv_harness", PV_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["pv_harness"] = module
    spec.loader.exec_module(module)
    return module


def test_each_pi_drive_gets_its_own_shared_log_and_sessions(pv, tmp_path):
    """Pi drives in one run shared scratch/cfg, so the previous drive's call became
    the next drive's startup pack and the job-message lookup read the wrong file."""
    first = pv.pi_drive_dirs(tmp_path, tmp_path / "evidence/handoff-pi-1")
    second = pv.pi_drive_dirs(tmp_path, tmp_path / "evidence/memory-pi-2")
    assert first != second
    assert not {first[0], first[1]} & {second[0], second[1]}
    assert all(d.is_dir() and tmp_path in d.parents for d in (*first, *second))


def test_seeded_pack_uses_a_per_drive_config(pv):
    source = inspect.getsource(pv.seed_pack)
    assert 'run / "scratch/seed" / proof.dir.name' in source


def test_ack_checking_voice_drives_run_unmuted(pv):
    """Voice skips its fallback spoken ack while muted (shouldAcknowledgeHandoff), so a
    muted drive's "acknowledged aloud" check passed only when the model happened to speak."""
    for drive in (pv.drive_handoff_voice, pv.drive_steer, pv.drive_stop):
        assert "voice.start(muted=False)" in inspect.getsource(drive), drive.__name__
    for drive in (pv.drive_conversation, pv.drive_memory_voice):
        assert "voice.start(muted=True)" in inspect.getsource(drive), drive.__name__


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pty")
def test_pi_close_survives_an_exited_unreaped_child(pv, tmp_path):
    """Pi exits on Ctrl-D before close() reaps it; killpg on that zombie raised EPERM
    on macOS and turned a passing drive into NOT VERIFIED."""
    master, slave = os.openpty()
    child = subprocess.Popen([sys.executable, "-c", ""], start_new_session=True)
    pid = child.pid
    time.sleep(1)  # exited, deliberately not reaped
    term = object.__new__(pv.PiTerminal)
    term.pid, term.fd = pid, master
    term.screen = open(tmp_path / "pty.log", "ab")
    try:
        term.close()
    finally:
        os.close(slave)
        os.close(master)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
