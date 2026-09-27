"""Exercise launcher ownership without models, network ports, or personal config.

The launcher used to supervise two services; the sidecar was removed, so every
scenario here is about the one voice backend it still starts.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

VOICE_PORT = "18766"


@pytest.fixture
def launcher(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copy(root / "run-browser.sh", tmp_path)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    service = """#!/bin/bash
marker="$TEST_STATE/$PORT"
echo $$ > "$marker"
trap 'rm -f "$marker"; exit 0' TERM INT
while :; do sleep 0.1; done
"""
    # The real launcher recognises its service by command line ("chatbot
    # serve"), so the fake must present the same.
    (binaries / "chatbot").write_text(service)
    (tmp_path / "run-openrouter.sh").write_text('#!/bin/bash\nexec "$TEST_STATE/bin/chatbot" serve --port "$PORT"\n')
    (binaries / "lsof").write_text("""#!/bin/bash
for arg in "$@"; do
  case "$arg" in TCP:*) cat "$TEST_STATE/${arg#TCP:}" 2>/dev/null || true ;; esac
done
""")
    # Stands in for scripts/service_state.py (tested on its own): the health
    # verdict for a port is whatever the test wrote to state-<port>.
    (binaries / "python3").write_text("""#!/bin/bash
url="$2"
port="${url#http://127.0.0.1:}"
cat "$TEST_STATE/state-${port%%/*}" 2>/dev/null || echo unreachable
""")
    for file in [
        tmp_path / "run-openrouter.sh",
        binaries / "lsof",
        binaries / "python3",
        binaries / "chatbot",
    ]:
        file.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{binaries}:/usr/bin:/bin",
        "CHATBOT_ENV": str(tmp_path / "no-personal-config"),
        "TEST_STATE": str(tmp_path),
        "PORT": VOICE_PORT,
        "SERVER_LOG": str(tmp_path / "server.log"),
    }
    processes = []

    def start(*args):
        process = subprocess.Popen(
            ["/bin/bash", str(tmp_path / "run-browser.sh"), *args],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        return process

    def start_service(port):
        """A Chatbot-looking service nobody's launcher owns (started by hand)."""
        process = subprocess.Popen(
            [str(binaries / "chatbot"), "serve", "--port", port],
            env={**environment, "PORT": port},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        wait_for(lambda: (tmp_path / port).exists())
        return process

    yield tmp_path, start, start_service
    for process in processes:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate()


def pid_in(marker: Path) -> int:
    return int(marker.read_text().strip())


def test_reuse_starts_the_service_when_nothing_is_listening(launcher):
    directory, start, _ = launcher
    process = start("--reuse-running")
    wait_for(lambda: (directory / VOICE_PORT).exists())
    process.terminate()
    assert process.wait(timeout=5) == 143
    wait_for(lambda: not (directory / VOICE_PORT).exists())


def test_reuse_preserves_an_external_service_and_exits(launcher):
    directory, start, _ = launcher
    (directory / VOICE_PORT).write_text("external-service")
    process = start("--reuse-running")
    assert process.wait(timeout=5) == 0
    assert (directory / VOICE_PORT).read_text() == "external-service"


def test_sidecar_only_is_accepted_and_starts_nothing_of_its_own(launcher):
    """The flag outlived the sidecar; Voice.app may still pass it."""
    directory, start, _ = launcher
    process = start("--sidecar-only")
    wait_for(lambda: (directory / VOICE_PORT).exists())
    process.terminate()
    assert process.wait(timeout=5) == 143


def test_normal_start_refuses_an_occupied_port(launcher):
    directory, start, _ = launcher
    (directory / VOICE_PORT).write_text("external-service")
    assert start().wait(timeout=5) == 1
    assert (directory / VOICE_PORT).read_text() == "external-service"


def test_reuse_keeps_a_current_service(launcher):
    directory, start, start_service = launcher
    voice = start_service(VOICE_PORT)
    (directory / f"state-{VOICE_PORT}").write_text("current\n")
    process = start("--reuse-running")
    assert process.wait(timeout=5) == 0
    # The launcher only ever stops what it started.
    assert voice.poll() is None
    assert pid_in(directory / VOICE_PORT) == voice.pid


@pytest.mark.parametrize("verdict", ["stale", "unknown", "foreign", None])
def test_reuse_replaces_a_service_that_is_not_running_this_checkouts_code(launcher, verdict):
    """stale = disk changed under it; unknown = predates fingerprints; foreign =
    another checkout; None = listening but never answers (hung)."""
    directory, start, start_service = launcher
    old = start_service(VOICE_PORT)
    if verdict is not None:
        (directory / f"state-{VOICE_PORT}").write_text(f"{verdict}\n")
    process = start("--reuse-running")
    wait_for(lambda: old.poll() is not None)
    wait_for(lambda: (directory / VOICE_PORT).exists() and pid_in(directory / VOICE_PORT) != old.pid)
    process.terminate()
    assert process.wait(timeout=5) == 143
    wait_for(lambda: not (directory / VOICE_PORT).exists())


def test_reuse_stops_the_launcher_owning_a_stale_service(launcher):
    directory, start, _ = launcher
    first = start("--reuse-running")
    wait_for(lambda: (directory / VOICE_PORT).exists())
    old_pid = pid_in(directory / VOICE_PORT)
    # The voice backend runs code that has since changed.
    (directory / f"state-{VOICE_PORT}").write_text("stale\n")
    second = start("--reuse-running")
    # Stopping the first launcher takes its service down cleanly...
    assert first.wait(timeout=10) == 143
    # ...and the second launcher brings it back under its own supervision.
    wait_for(lambda: (directory / VOICE_PORT).exists() and pid_in(directory / VOICE_PORT) != old_pid)
    second.terminate()
    assert second.wait(timeout=5) == 143
    wait_for(lambda: not (directory / VOICE_PORT).exists())


def test_normal_start_still_refuses_a_stale_occupant(launcher):
    directory, start, start_service = launcher
    old = start_service(VOICE_PORT)
    (directory / f"state-{VOICE_PORT}").write_text("stale\n")
    assert start().wait(timeout=5) == 1
    assert old.poll() is None
