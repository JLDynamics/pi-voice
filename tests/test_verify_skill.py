"""Regression tests for the verify-pi-voice harness (.cursor/skills/verify-pi-voice/scripts/pv.py)."""

import importlib.util
import inspect
import sys
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


def test_ack_checking_voice_drives_run_unmuted(pv):
    """Voice skips its fallback spoken ack while muted (shouldAcknowledgeHandoff), so a
    muted drive's "acknowledged aloud" check passed only when the model happened to speak."""
    for drive in (pv.drive_handoff_voice, pv.drive_steer, pv.drive_stop):
        assert "voice.start(muted=False)" in inspect.getsource(drive), drive.__name__
    for drive in (pv.drive_conversation, pv.drive_memory_voice):
        assert "voice.start(muted=True)" in inspect.getsource(drive), drive.__name__
