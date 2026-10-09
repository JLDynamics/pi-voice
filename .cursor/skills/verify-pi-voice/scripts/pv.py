#!/usr/bin/env python3
"""Isolated launch, doctor, drive and cleanup for pi-voice verification.

Every run lives in $PV_VERIFY_ROOT/<run-id> (default /tmp/pv-verify).
scratch/ (config, TMPDIR, Pi sessions, fixture project) is removed by cleanup;
logs/ and evidence/ are kept. Nothing here reads ~/.config/pi-voice, writes
~/.pi, or touches a process this run did not start.
"""

from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO = Path(
    subprocess.check_output(["git", "-C", str(SKILL_DIR), "rev-parse", "--show-toplevel"], text=True).strip()
).resolve()
ROOT = Path(os.environ.get("PV_VERIFY_ROOT", "/tmp/pv-verify"))
USER_PORT = 8766
VOICE_BIN = REPO / "macos/Voice/build/Voice.app/Contents/MacOS/Voice"
EXTENSION = REPO / "pi-voice/src/index.ts"
FIXTURE = {
    "README.md": "# Fixture project\n\nA tiny folder for pi-voice verification.\n",
    "notes.txt": "TODO: write the release notes\n",
    "main.py": "# FIXME: handle empty input\nprint('hello')\n",
}
JOB_MARKER = re.compile(r"\[Pi voice job id: [^\]]+\]$")

def die(message: str) -> None:
    print(f"pv: {message}", file=sys.stderr)
    sys.exit(2)

def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")

def run_dir(run_id: Optional[str]) -> Path:
    if not run_id:
        latest = ROOT / "latest"
        if not latest.exists():
            die(f"no run yet; start one with: {Path(__file__)} launch")
        run_id = latest.read_text().strip()
    path = ROOT / run_id
    if not (path / "state.json").exists():
        die(f"no state.json in {path}")
    return path

def load_state(run: Path) -> dict[str, Any]:
    return json.loads((run / "state.json").read_text())

def save_state(run: Path, state: dict[str, Any]) -> None:
    tmp = run / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(run / "state.json")

def remember_child(run: Path, pid: int, kind: str, match: str) -> None:
    state = load_state(run)
    state.setdefault("children", []).append({"pid": pid, "pgid": pid, "kind": kind, "match": match})
    save_state(run, state)

def ps_command(pid: int) -> str:
    out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return out.stdout.strip()

def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

def group_members(pgid: int) -> list[tuple[int, str]]:
    out = subprocess.run(["ps", "-axo", "pid=,pgid=,command="], capture_output=True, text=True).stdout
    members = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] == str(pgid):
            members.append((int(parts[0]), parts[2]))
    return members

def listener(port: int) -> Optional[int]:
    out = subprocess.run(["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
    pids = [int(p) for p in out.split()]
    return pids[0] if pids else None

def pgid_of(pid: int) -> Optional[int]:
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None

def free_port() -> int:
    for port in range(18766, 18866):
        if listener(port):
            continue
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    die("no free port in 18766-18865")
    return 0

def http_json(url: str, timeout: float = 2.0) -> Optional[dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except Exception:
        return None

def stop_group(pgid: int, label: str, must_match: tuple[str, ...]) -> str:
    members = group_members(pgid)
    if not members:
        return f"{label}: already gone"
    if not any(any(m in cmd for m in must_match) for _, cmd in members):
        return f"{label}: pgid {pgid} no longer runs {must_match}; left alone"
    os.killpg(pgid, signal.SIGTERM)
    for _ in range(75):
        if not group_members(pgid):
            return f"{label}: stopped pgid {pgid} with SIGTERM"
        time.sleep(0.2)
    os.killpg(pgid, signal.SIGKILL)
    return f"{label}: SIGKILL pgid {pgid} after 15s"

def write_wrapper(run: Path, port: int) -> Path:
    """VOICE_BIN target: the real Voice with a per-process wsUrl (NSArgumentDomain).

    `defaults write` would change the user's installed Voice.app too; an argument
    only lives for this process. It also saves VOICE_HISTORY when PV_HISTORY_DUMP is set.
    """
    wrapper = run / "scratch/bin/voice"
    wrapper.write_text(
        "#!/bin/bash\n"
        'if [ -n "${PV_HISTORY_DUMP:-}" ]; then\n'
        '  mkdir -p "$PV_HISTORY_DUMP"\n'
        '  printf "%s" "${VOICE_HISTORY:-}" > "$PV_HISTORY_DUMP/voice-history.$(date +%H%M%S).$$.json"\n'
        "fi\n"
        f'exec "{VOICE_BIN}" "$@" -voice.wsUrl "ws://127.0.0.1:{port}/v1/realtime"\n'
    )
    wrapper.chmod(0o755)
    return wrapper

def newest_source_mtime() -> float:
    return max(p.stat().st_mtime for p in (REPO / "macos/Voice/Sources").rglob("*.swift"))

def cmd_launch(args: argparse.Namespace) -> None:
    port = args.port or free_port()
    if port == USER_PORT:
        die("refusing :8766; that port belongs to the user's own Voice.app/backend")
    if listener(port):
        die(f"port {port} is already in use")
    run_id = stamp()
    run = ROOT / run_id
    for sub in ("scratch/cfg", "scratch/tmp", "scratch/sessions", "scratch/proj", "scratch/bin", "logs", "evidence"):
        (run / sub).mkdir(parents=True, exist_ok=True)
    for name, text in FIXTURE.items():
        (run / "scratch/proj" / name).write_text(text)
    if args.build or not VOICE_BIN.exists():
        print("building Voice.app (log: logs/build.log)...")
        with open(run / "logs/build.log", "wb") as log:
            if subprocess.run(["bash", str(REPO / "macos/Voice/scripts/build.sh")], stdout=log, stderr=subprocess.STDOUT).returncode:
                die(f"Voice build failed; see {run / 'logs/build.log'}")
    wrapper = write_wrapper(run, port)
    env = dict(os.environ, PORT=str(port), SERVER_LOG=str(run / "logs/server.log"))
    with open(run / "logs/launcher.log", "ab") as out:
        proc = subprocess.Popen(
            ["bash", str(REPO / "run-browser.sh")],
            cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    state = {
        "run_id": run_id, "repo": str(REPO), "port": port,
        "ws_url": f"ws://127.0.0.1:{port}/v1/realtime",
        "launcher_pid": proc.pid, "pgid": proc.pid, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "voice_bin": str(VOICE_BIN), "wrapper": str(wrapper), "children": [],
    }
    save_state(run, state)
    (ROOT / "latest").write_text(run_id + "\n")
    print(f"run {run_id}: backend launcher pid {proc.pid} on port {port}; waiting for /health ready...")
    deadline = time.time() + args.timeout
    health: Optional[dict[str, Any]] = None
    while time.time() < deadline:
        if proc.poll() is not None:
            die(f"launcher exited with {proc.returncode}; see {run / 'logs/launcher.log'} and {run / 'logs/server.log'}")
        health = http_json(f"http://127.0.0.1:{port}/health")
        if health and health.get("ready"):
            break
        time.sleep(1)
    else:
        die(f"backend not ready after {args.timeout}s; run `pv.py doctor`, then `pv.py cleanup`")
    print(json.dumps({"run": str(run), "port": port, "health": health}, indent=2))
    print(f"ready. next: {Path(__file__)} doctor")

def cmd_doctor(args: argparse.Namespace) -> None:
    run = run_dir(args.run)
    st = load_state(run)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    port = st["port"]
    launcher_cmd = ps_command(st["launcher_pid"]) if alive(st["launcher_pid"]) else ""
    check("launcher alive and is run-browser.sh", "run-browser.sh" in launcher_cmd, launcher_cmd or "not running")
    lp = listener(port)
    check(f"port {port} listener belongs to this run's process group",
          lp is not None and pgid_of(lp) == st["pgid"], {"listener_pid": lp, "pgid": pgid_of(lp) if lp else None})
    health = http_json(f"http://127.0.0.1:{port}/health") or {}
    check("/health ready", health.get("ready") is True, {k: health.get(k) for k in ("status", "ready", "server_tools")})
    source = health.get("source")
    check("/health source is this checkout", bool(source) and Path(source).resolve() == REPO, source)
    check("/health not stale (disk matches loaded code)", health.get("stale") is False, health.get("stale"))
    built = VOICE_BIN.stat().st_mtime if VOICE_BIN.exists() else 0
    check("Voice.app built from this checkout", VOICE_BIN.exists(), str(VOICE_BIN))
    check("Voice.app build newer than Swift sources", built >= newest_source_mtime(),
          "rebuild: pv.py launch --build (or bash macos/Voice/scripts/build.sh)" if built < newest_source_mtime() else "fresh")
    wrapper = Path(st["wrapper"])
    check("voice wrapper targets this run's port", wrapper.exists() and f":{port}/" in wrapper.read_text(), str(wrapper))
    pi = shutil.which("pi")
    pi_version = subprocess.run([pi, "--version"], capture_output=True, text=True).stdout.strip() if pi else ""
    check("pi CLI available (needed only for --via pi)", bool(pi), pi_version or "missing")
    check("isolated dirs exist", all((run / f"scratch/{d}").is_dir() for d in ("cfg", "tmp", "sessions", "proj")), str(run / "scratch"))
    stray = [c for c in st.get("children", []) if alive(c["pid"])]
    check("no stray drive children", not stray, stray)
    user_listener = listener(USER_PORT)
    clamshell = subprocess.run(["ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"], capture_output=True, text=True).stdout
    lid_closed = '"AppleClamshellState" = Yes' in clamshell
    info = {"user_port_8766_listener": user_listener, "note": "informational; never touched", "lid_closed": lid_closed}
    if lid_closed:
        print("WARN  lid is closed: built-in audio does no IO, so --via voice and --via pi crash Voice "
              "('player did not see an IO cycle'). --via backend still works.")
    report = {"run": str(run), "checks": checks, "info": info}
    out = run / "evidence" / f"doctor-{stamp()}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    for c in checks:
        print(f"{'PASS' if c['ok'] else 'FAIL'}  {c['check']}  {c['detail'] if not c['ok'] else ''}")
    print(f"info: {info}")
    print(f"saved {out}")
    sys.exit(0 if all(c["ok"] for c in checks) else 1)

class Proof:
    def __init__(self, run: Path, feature: str, via: str):
        self.dir = run / "evidence" / f"{feature}-{via}-{stamp()}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.feature, self.via = feature, via
        self.checks: list[dict[str, Any]] = []
        self.t0 = time.time()
        self.log = open(self.dir / "drive.log", "a")

    def note(self, *parts: Any) -> None:
        line = f"[{time.time() - self.t0:7.2f}] " + " ".join(str(p) for p in parts)
        print(line, flush=True)
        self.log.write(line + "\n")
        self.log.flush()

    def check(self, name: str, ok: Any, detail: Any = "") -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        self.note("CHECK", "PASS" if ok else "FAIL", name, json.dumps(detail, default=str)[:400])
        return bool(ok)

    def finish(self) -> None:
        verdict = "VERIFIED" if self.checks and all(c["ok"] for c in self.checks) else "NOT VERIFIED"
        summary = {"feature": self.feature, "via": self.via, "verdict": verdict, "checks": self.checks}
        (self.dir / "checks.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
        self.note(f"{verdict}: {self.feature} via {self.via}; evidence {self.dir}")
        self.log.close()
        sys.exit(0 if verdict == "VERIFIED" else 1)

class Voice:
    """Headless Voice.app over NDJSON stdio; the harness plays the Pi extension's part."""

    def __init__(self, run: Path, st: dict[str, Any], proof: Proof, history: list[dict[str, str]], label: str):
        self.proof = proof
        self.events: list[tuple[float, dict[str, Any]]] = []
        self.cond = threading.Condition()
        env = dict(os.environ, TMPDIR=str(run / "scratch/tmp") + "/", VOICE_THINKER="luna",
                   VOICE_HISTORY=json.dumps(history), PV_HISTORY_DUMP=str(proof.dir))
        self.transcript = open(proof.dir / f"{label}.ndjson", "a")
        self.stderr_path = run / "logs" / f"voice-{label}-{stamp()}.stderr.log"
        self.proc = subprocess.Popen(
            [st["wrapper"], "--headless"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(self.stderr_path, "ab"), env=env, start_new_session=True,
        )
        remember_child(run, self.proc.pid, "voice", str(VOICE_BIN))
        proof.note("spawned headless Voice pid", self.proc.pid)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout
        for raw in self.proc.stdout:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            self.transcript.write(json.dumps({"t": round(time.time() - self.proof.t0, 2), "dir": "out", "event": event}) + "\n")
            self.transcript.flush()
            if event.get("type") != "spoken_delta":
                self.proof.note("<-", json.dumps(event)[:300])
            with self.cond:
                self.events.append((time.time(), event))
                self.cond.notify_all()

    def crash_reason(self) -> str:
        text = self.stderr_path.read_text(errors="replace") if self.stderr_path.exists() else ""
        found = re.findall(r"reason: '([^']+)'", text)
        return found[-1] if found else f"exit {self.proc.returncode}"

    def send(self, obj: dict[str, Any]) -> float:
        assert self.proc.stdin
        if self.proc.poll() is not None:
            raise RuntimeError(f"Voice exited before {obj.get('type')}: {self.crash_reason()} (see {self.stderr_path})")
        self.transcript.write(json.dumps({"t": round(time.time() - self.proof.t0, 2), "dir": "in", "event": obj}) + "\n")
        self.transcript.flush()
        self.proof.note("->", json.dumps(obj)[:300])
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()
        return time.time()

    def wait(self, pred: Callable[[dict[str, Any]], bool], timeout: float, since: float = 0) -> Optional[dict[str, Any]]:
        deadline = time.time() + timeout
        with self.cond:
            while True:
                for t, event in self.events:
                    if t >= since and pred(event):
                        return event
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self.cond.wait(remaining)

    def after(self, since: float, kind: str) -> list[dict[str, Any]]:
        with self.cond:
            return [e for t, e in self.events if t >= since and e.get("type") == kind]

    def say(self, text: str, kinds: tuple[str, ...] = ("spoken",), timeout: float = 60) -> tuple[float, Optional[dict[str, Any]]]:
        t = self.send({"type": "user", "text": text})
        return t, self.wait(lambda e: e.get("type") in kinds + ("error", "request_error"), timeout, t)

    def start(self, muted: bool = True) -> bool:
        """Muted keeps the real mic and speaker closed. Unmuted is needed wherever a
        spoken acknowledgement is checked: Voice skips its silent-handoff ack while
        muted by design (VoiceToolFollowUp.shouldAcknowledgeHandoff)."""
        self.muted = muted
        self.send({"type": "mute", "muted": muted})
        ready = self.wait(lambda e: e.get("type") in ("ready", "error"), 240)
        self.send({"type": "mute", "muted": muted})
        label = "muted: mic and speaker closed" if muted else "unmuted: real mic and speaker open"
        if not self.proof.check(f"Voice ready ({label})", ready and ready["type"] == "ready", ready):
            return False
        deadline = time.time() + 20
        running = False
        while time.time() < deadline and self.proc.poll() is None and not running:
            running = "running rate=" in self.stderr_path.read_text(errors="replace")
            time.sleep(0.5)
        detail = "audio engine running" if running else self.crash_reason() if self.proc.poll() is not None else "no 'running rate=' in 20s"
        return self.proof.check("Voice audio engine started (needs working audio IO)", running, detail)

    def quit(self) -> None:
        if not getattr(self, "muted", True):
            heard = [e for _, e in self.events if e.get("type") in ("heard", "speech_started")]
            self.proof.check("no room speech or echo heard while the mic was open", not heard, heard[:3])
        if self.proc.poll() is not None:
            self.proof.check("Voice still running at quit", False, self.crash_reason())
            return
        t = self.send({"type": "quit"})
        try:
            code = self.proc.wait(10)
            self.proof.check("quit exits cleanly", code == 0, f"rc={code} in {time.time() - t:.1f}s")
        except subprocess.TimeoutExpired:
            self.proof.check("quit exits cleanly", False, "no exit in 10s; SIGTERM")
            os.killpg(self.proc.pid, signal.SIGTERM)

def journal_events(run: Path) -> list[dict[str, Any]]:
    path = run / "scratch/tmp/pi-voice.jobs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def mentions(event: Optional[dict[str, Any]], pattern: str) -> bool:
    return bool(event and event.get("type") == "spoken" and re.search(pattern, event.get("text", ""), re.I))

def usage(st: dict[str, Any]) -> Optional[dict[str, Any]]:
    return http_json(f"http://127.0.0.1:{st['port']}/v1/usage")

def drive_conversation(run: Path, st: dict[str, Any], proof: Proof) -> None:
    before = usage(st)
    voice = Voice(run, st, proof, [], "conversation")
    try:
        if not voice.start(muted=True):
            return
        _, reply = voice.say("Hi there. Reply with just the single word hello.")
        proof.check("typed turn gets a spoken reply", mentions(reply, r"hello"), reply)
        time.sleep(2)
        _, reply = voice.say("What is two plus two? One short sentence.")
        proof.check("second typed turn gets a correct reply", mentions(reply, r"\b(4|four)\b"), reply)
        after = usage(st)
        (proof.dir / "usage.json").write_text(json.dumps({"before": before, "after": after}, indent=2) + "\n")
        proof.check("backend /v1/usage changed (model really called on this run's port)", before != after, "usage.json")
    finally:
        voice.quit()

def backend_python() -> list[str]:
    venv = REPO / ".venv/bin/python"
    return [str(venv)] if venv.exists() else ["uv", "run", "--project", str(REPO), "python"]


def drive_conversation_backend(run: Path, st: dict[str, Any], proof: Proof) -> None:
    port = st["port"]
    env = dict(os.environ, VOICE_HTTP=f"http://127.0.0.1:{port}", VOICE_WS=st["ws_url"])
    out = subprocess.run(backend_python() + [str(REPO / "scripts/verify-voice.py"), "--out", str(proof.dir / "verify-voice")],
                         cwd=REPO, env=env, capture_output=True, text=True)
    (proof.dir / "verify-voice.stdout.txt").write_text(out.stdout + out.stderr)
    try:
        summary = json.loads((proof.dir / "verify-voice/summary.json").read_text())
    except (OSError, ValueError):
        proof.check("verify-voice.py wrote summary.json", False, out.stdout[-400:] + out.stderr[-400:])
        return
    for mark, name, detail in re.findall(r"^\[(ok|FAIL)\] (.+?)(?: \u2014 (.*))?$", out.stdout, re.M):
        proof.check(f"verify-voice: {name}", mark == "ok", detail[:200])
    proof.check("verify-voice.py summary ok and exit 0", summary.get("ok") is True and out.returncode == 0,
                {"failed": summary.get("failed"), "rc": out.returncode})


def drive_memory_backend(run: Path, st: dict[str, Any], proof: Proof) -> None:
    pack = seed_pack(run, proof)
    text = pack[0]["text"] if pack else ""
    if not proof.check("pack is one user turn with Latest and a Pi result",
                       len(pack) == 1 and pack[0]["role"] == "user" and "Latest:" in text and "[Pi result" in text,
                       text[:300]):
        return
    question = "Without asking Pi, from what you remember: what did I name this project, and which files did Pi find? One sentence."
    out = subprocess.run(backend_python() + [str(SKILL_DIR / "scripts/ws_recall.py"), st["ws_url"], str(proof.dir / "startup-pack.json"),
                                             question, str(proof.dir / "recall.json")], capture_output=True, text=True)
    (proof.dir / "ws_recall.stderr.txt").write_text(out.stderr)
    try:
        recall = json.loads((proof.dir / "recall.json").read_text())
    except (OSError, ValueError):
        proof.check("ws_recall.py wrote recall.json", False, out.stderr[-400:])
        return
    proof.check("pack injected without a reply (no response before the question)", recall.get("responses_before_question") == 0,
                recall.get("responses_before_question"))
    proof.check("Agent recalls the earlier call from the pack", re.search(r"pelican", recall.get("transcript", ""), re.I),
                recall.get("transcript"))
    proof.check("recall used no tool (answered from the pack)", not recall.get("tool_calls"), recall.get("tool_calls"))


def drive_handoff_voice(run: Path, st: dict[str, Any], proof: Proof) -> None:
    voice = Voice(run, st, proof, [], "handoff")
    try:
        if not voice.start(muted=False):
            return
        t, work = voice.say("Please have Pi list the files in the current project folder, the one Pi is working in.", ("work",), 45)
        if not proof.check("spawn_thinking emits a work event with id and brief",
                           work and work.get("type") == "work" and work.get("id") and work.get("brief"), work):
            return
        ack = voice.wait(lambda e: e.get("type") == "spoken", 15, t)
        proof.check("handoff is acknowledged aloud", ack, ack)
        time.sleep(3)
        proof.check("one handoff, not repeated by the ack", len(voice.after(t, "work")) == 1, len(voice.after(t, "work")))
        voice.send({"type": "job_update", "id": work["id"], "status": "working"})
        time.sleep(2)
        voice.send({"type": "job_update", "id": work["id"], "status": "done"})
        answer = "The project folder has 3 files: README.md, notes.txt, and main.py."
        t = voice.send({"type": "result", "id": work["id"], "speak": answer, "full": answer})
        final = voice.wait(lambda e: e.get("type") == "spoken", 40, t)
        proof.check("[FINAL] result is spoken back", mentions(final, r"readme|main|notes|three|\b3\b"), final)
        journal = journal_events(run)
        (proof.dir / "jobs-journal.json").write_text(json.dumps(journal, indent=2) + "\n")
        proof.check("job journal written under the run's TMPDIR", any(j.get("id") == work["id"] for j in journal),
                    [j.get("event") for j in journal])
    finally:
        voice.quit()

def drive_steer(run: Path, st: dict[str, Any], proof: Proof) -> None:
    voice = Voice(run, st, proof, [], "steer")
    try:
        if not voice.start(muted=False):
            return
        _, first = voice.say("Have Pi search my project for TODO comments.", ("work",), 45)
        if not proof.check("first handoff emits work", first and first.get("type") == "work", first):
            return
        time.sleep(6)
        voice.send({"type": "job_update", "id": first["id"], "status": "working"})
        time.sleep(1)
        t, second = voice.say("Actually, tell Pi to look for FIXME comments instead of TODO.", ("work", "stop_work"), 45)
        if not proof.check("redirect while busy emits a new work id (steer, not stop)",
                           second and second.get("type") == "work" and second.get("id") != first["id"], second):
            return
        ack = voice.wait(lambda e: e.get("type") == "spoken", 15, t)
        proof.check("redirect is acknowledged aloud", ack, ack)
        voice.send({"type": "job_update", "id": first["id"], "status": "superseded", "note": "Task updated; Pi continues"})
        voice.send({"type": "job_update", "id": second["id"], "status": "working"})
        time.sleep(3)
        answer = "One FIXME: main.py line 1 says handle empty input."
        voice.send({"type": "job_update", "id": second["id"], "status": "done"})
        t = voice.send({"type": "result", "id": second["id"], "speak": answer, "full": answer})
        final = voice.wait(lambda e: e.get("type") == "spoken", 40, t)
        proof.check("the redirected job's result is spoken", mentions(final, r"fixme|main|empty input"), final)
        journal = journal_events(run)
        (proof.dir / "jobs-journal.json").write_text(json.dumps(journal, indent=2) + "\n")
        proof.check("journal records the steer", any(j.get("event") == "steered" and j.get("id") == second["id"] for j in journal),
                    [(j.get("event"), j.get("id")) for j in journal])
    finally:
        voice.quit()

def drive_stop(run: Path, st: dict[str, Any], proof: Proof) -> None:
    voice = Voice(run, st, proof, [], "stop")
    try:
        if not voice.start(muted=False):
            return
        _, work = voice.say("Have Pi count the lines in the README file.", ("work",), 45)
        if not proof.check("handoff emits work", work and work.get("type") == "work", work):
            return
        time.sleep(6)
        voice.send({"type": "job_update", "id": work["id"], "status": "working"})
        time.sleep(1)
        t, stop = voice.say("Actually never mind, cancel that task.", ("stop_work",), 40)
        proof.check("cancel calls stop_thinking (stop_work emitted)", stop and stop.get("type") == "stop_work", stop)
        voice.send({"type": "job_update", "id": work["id"], "status": "stopped"})
        ack = voice.wait(lambda e: e.get("type") == "spoken", 20, t)
        proof.check("stop is acknowledged aloud", ack, ack)
        time.sleep(3)
        t, idle = voice.say("Stop whatever Pi is doing.", ("spoken",), 25)
        proof.check("stop with nothing running still answers", idle and idle.get("type") == "spoken", idle)
        proof.check("stop with nothing running forwards nothing", not voice.after(t, "stop_work"), voice.after(t, "stop_work"))
    finally:
        voice.quit()

def seed_pack(run: Path, proof: Proof) -> list[dict[str, str]]:
    """Build the startup pack with the extension's own Conversation code in an isolated config dir."""
    cfg = run / "scratch/cfg-seed"
    cfg.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PI_VOICE_CONFIG=str(cfg), PV_PROJ=str(run / "scratch/proj"), PV_SRC=str(REPO / "pi-voice/src"))
    out = subprocess.run(["node", str(SKILL_DIR / "scripts/seed-history.mjs")], env=env, capture_output=True, text=True)
    (proof.dir / "seed-history.stderr.txt").write_text(out.stderr)
    if out.returncode:
        proof.check("startup pack built by the extension code", False, out.stderr[-400:])
        return []
    pack = json.loads(out.stdout)
    (proof.dir / "startup-pack.json").write_text(json.dumps(pack, indent=2) + "\n")
    return pack

def drive_memory_voice(run: Path, st: dict[str, Any], proof: Proof) -> None:
    pack = seed_pack(run, proof)
    text = pack[0]["text"] if pack else ""
    if not proof.check("pack is one user turn with Latest and a Pi result",
                       len(pack) == 1 and pack[0]["role"] == "user" and "Latest:" in text and "[Pi result" in text,
                       text[:300]):
        return
    voice = Voice(run, st, proof, pack, "memory")
    try:
        if not voice.start(muted=True):
            return
        t, reply = voice.say(
            "Without asking Pi, from what you remember: what did I name this project, and which files did Pi find? One sentence.",
            ("spoken", "work"), 60)
        proof.check("Agent recalls the earlier call from the pack", mentions(reply, r"pelican"), reply)
        proof.check("recall did not hand off to Pi", not voice.after(t, "work"), voice.after(t, "work"))
    finally:
        voice.quit()

class PiTerminal:
    """Interactive Pi with this checkout's extension, isolated config, sessions and TMPDIR."""

    def __init__(self, run: Path, st: dict[str, Any], proof: Proof):
        self.run, self.proof = run, proof
        self.sessions = run / "scratch/sessions"
        self.cfg = run / "scratch/cfg"
        self.tmp = run / "scratch/tmp"
        env = dict(os.environ, PI_VOICE_CONFIG=str(self.cfg), TMPDIR=str(self.tmp) + "/", VOICE_BIN=st["wrapper"],
                   TERM="xterm-256color", PV_HISTORY_DUMP=str(proof.dir))
        argv = ["pi", "--tui-mode", "regular", "--offline", "--no-mcp", "--session-dir", str(self.sessions),
                "-na", "-ne", "-ns", "-np", "-e", str(EXTENSION)]
        proof.note("spawn", " ".join(argv))
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(run / "scratch/proj")
            os.execvpe("pi", argv, env)
        import fcntl
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        self.pid, self.fd = pid, fd
        remember_child(run, pid, "pi", "pi")
        self.screen = open(proof.dir / "pi-pty.log", "ab")

    def pump(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], min(0.5, max(0.0, end - time.time())))
            if ready:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    return
                if not data:
                    return
                self.screen.write(data)
                self.screen.flush()

    def type_line(self, text: str) -> None:
        self.proof.note("TYPE", text)
        os.write(self.fd, text.encode())
        self.pump(0.6)
        os.write(self.fd, b"\r")
        self.pump(0.5)
        if text.startswith("/") and " " in text:
            os.write(self.fd, b"\r")
            self.pump(0.5)

    def wait_for(self, pred: Callable[[], Any], timeout: float) -> Any:
        end = time.time() + timeout
        while time.time() < end:
            value = pred()
            if value:
                return value
            self.pump(1.0)
        return None

    def entries(self) -> list[dict[str, Any]]:
        files = sorted(self.sessions.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not files:
            return []
        out = []
        for line in files[-1].read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    @staticmethod
    def text_of(entry: dict[str, Any]) -> str:
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        return ""

    def spoken_after(self, n: int) -> list[dict[str, Any]]:
        return [e["data"] for e in self.entries()[n:]
                if e.get("type") == "custom" and e.get("customType") == "pi-voice-face"
                and (e.get("data") or {}).get("kind") in ("spoken", "spoken-final")]

    def lease_voice_pid(self) -> Optional[int]:
        try:
            return json.loads((self.tmp / "pi-voice.lease.json").read_text())["voicePid"]
        except (OSError, ValueError, KeyError):
            return None

    def voice_ready(self, pid: int) -> bool:
        log = self.tmp / f"pi-voice.{pid}.stderr.log"
        return log.exists() and "running rate=" in log.read_text(errors="replace")

    def rows(self) -> list[tuple[Any, ...]]:
        try:
            cid = json.loads((self.cfg / "session.json").read_text())["conversations"]
            db_id = next(iter(cid.values()))
        except (OSError, ValueError, StopIteration):
            return []
        db = sqlite3.connect(f"file:{self.cfg}/history-{db_id}.sqlite?mode=ro", uri=True)
        try:
            return db.execute("select id, t, role, kind, text from turns order by id").fetchall()
        finally:
            db.close()

    def start_voice(self, label: str) -> Optional[int]:
        self.type_line("/voice")
        pid = self.wait_for(self.lease_voice_pid, 30)
        ready = pid and self.wait_for(lambda: self.voice_ready(pid), 120)
        self.pump(1.5)
        self.proof.check(f"{label}: /voice started headless Voice from this checkout", ready, {"voice_pid": pid})
        return pid if ready else None

    def stop_voice(self, label: str) -> None:
        pid = self.lease_voice_pid()
        self.type_line("/voice stop")
        gone = self.wait_for(lambda: pid is None or (not alive(pid) and self.lease_voice_pid() is None), 20)
        self.proof.check(f"{label}: /voice stop ends the Voice child", gone, {"voice_pid": pid})

    def save(self) -> None:
        files = sorted(self.sessions.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if files:
            shutil.copy(files[-1], self.proof.dir / "pi-session.jsonl")
        rows = self.rows()
        (self.proof.dir / "history-rows.json").write_text(json.dumps(rows, indent=2) + "\n")
        journal = journal_events(self.run)
        (self.proof.dir / "jobs-journal.json").write_text(json.dumps(journal, indent=2) + "\n")

    def close(self) -> None:
        for key in (b"\x03", b"\x03", b"\x04"):
            try:
                os.write(self.fd, key)
            except OSError:
                break
            self.pump(1)
        if alive(self.pid):
            self.pump(2)
        if alive(self.pid):
            os.killpg(self.pid, signal.SIGTERM)
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass
        self.screen.close()

def drive_handoff_pi(run: Path, st: dict[str, Any], proof: Proof) -> None:
    pi = PiTerminal(run, st, proof)
    try:
        pi.pump(6)
        if not pi.start_voice("call"):
            return
        n = len(pi.entries())
        pi.type_line("Hi, reply with just the single word hello.")
        proof.check("typed text reaches Agent and is spoken", pi.wait_for(lambda: pi.spoken_after(n), 45), pi.spoken_after(n))
        n = len(pi.entries())
        pi.type_line("Please have Pi list the files in the current working directory and tell me their names.")
        job = pi.wait_for(lambda: next((e for e in pi.entries()[n:] if e.get("type") == "message"
                                        and (e.get("message") or {}).get("role") == "user"
                                        and "[Pi voice job id:" in pi.text_of(e)), None), 60)
        proof.check("Pi receives the job message, marker last", job and JOB_MARKER.search(pi.text_of(job)),
                    pi.text_of(job)[-200:] if job else None)
        final = pi.wait_for(lambda: next((d for d in pi.spoken_after(n) if re.search(r"readme|main|notes", d.get("text", ""), re.I)), None), 240)
        proof.check("Pi's result is spoken back by Agent", final, final or pi.spoken_after(n))
        rows = pi.rows()
        proof.check("shared log stores the [Pi handoff] brief", any(r[3] == "work" and r[4].startswith("[Pi handoff]") for r in rows),
                    [r[4][:80] for r in rows])
        proof.check("shared log stores the full [Pi result]", any(r[3] == "work" and r[4].startswith("[Pi result") for r in rows), "")
        journal = journal_events(run)
        proof.check("job journal lands in the isolated TMPDIR", any(j.get("event") == "queued" for j in journal),
                    [j.get("event") for j in journal])
        pi.stop_voice("call")
    finally:
        pi.save()
        pi.close()

def drive_memory_pi(run: Path, st: dict[str, Any], proof: Proof) -> None:
    pi = PiTerminal(run, st, proof)
    try:
        pi.pump(6)
        if not pi.start_voice("call 1"):
            return
        n = len(pi.entries())
        pi.type_line("Please remember that this project is called Pelican Harbor. Just say okay.")
        proof.check("call 1: Agent answers", pi.wait_for(lambda: pi.spoken_after(n), 45), pi.spoken_after(n))
        pi.pump(3)
        pi.stop_voice("call 1")
        dumps_before = set(proof.dir.glob("voice-history.*.json"))
        if not pi.start_voice("call 2"):
            return
        new = sorted(set(proof.dir.glob("voice-history.*.json")) - dumps_before)
        pack = json.loads(new[-1].read_text()) if new else []
        text = pack[0]["text"] if pack else ""
        proof.check("call 2: Voice child got one dated user-role pack with call 1",
                    len(pack) == 1 and pack[0]["role"] == "user" and "Pelican Harbor" in text and "Latest:" in text,
                    text[:300])
        n = len(pi.entries())
        pi.type_line("Without asking Pi: what is this project called? One short sentence.")
        recall = pi.wait_for(lambda: next((d for d in pi.spoken_after(n) if re.search(r"pelican", d.get("text", ""), re.I)), None), 45)
        proof.check("call 2: Agent recalls call 1 after reconnect", recall, recall or pi.spoken_after(n))
        pi.stop_voice("call 2")
    finally:
        pi.save()
        pi.close()

DRIVES: dict[tuple[str, str], Callable[[Path, dict[str, Any], Proof], None]] = {
    ("conversation", "voice"): drive_conversation,
    ("conversation", "backend"): drive_conversation_backend,
    ("memory", "backend"): drive_memory_backend,
    ("handoff", "voice"): drive_handoff_voice,
    ("handoff", "pi"): drive_handoff_pi,
    ("steer", "voice"): drive_steer,
    ("stop", "voice"): drive_stop,
    ("memory", "voice"): drive_memory_voice,
    ("memory", "pi"): drive_memory_pi,
}

def cmd_drive(args: argparse.Namespace) -> None:
    run = run_dir(args.run)
    st = load_state(run)
    drive = DRIVES.get((args.feature, args.via))
    if not drive:
        die(f"no drive for {args.feature} via {args.via}; have {sorted(DRIVES)}")
    health = http_json(f"http://127.0.0.1:{st['port']}/health") or {}
    if not health.get("ready"):
        die("backend not ready; run pv.py doctor")
    proof = Proof(run, args.feature, args.via)
    try:
        drive(run, st, proof)
    except Exception as error:  # a crashed drive is a failed proof, not a silent pass
        proof.check("drive ran to completion", False, repr(error))
    proof.finish()

def cmd_env(args: argparse.Namespace) -> None:
    run = run_dir(args.run)
    st = load_state(run)
    print(f"export PI_VOICE_CONFIG={run / 'scratch/cfg'}")
    print(f"export TMPDIR={run / 'scratch/tmp'}/")
    print(f"export VOICE_BIN={st['wrapper']}")
    print(f"export PV_PORT={st['port']}")
    print(f"export PV_PROJ={run / 'scratch/proj'}")
    print(f"export PV_SESSIONS={run / 'scratch/sessions'}")

def cmd_cleanup(args: argparse.Namespace) -> None:
    run = run_dir(args.run)
    st = load_state(run)
    actions = []
    for child in st.get("children", []):
        if alive(child["pid"]):
            actions.append(stop_group(child["pgid"], f"{child['kind']} {child['pid']}", (child["match"], str(VOICE_BIN))))
    try:
        lease = json.loads((run / "scratch/tmp/pi-voice.lease.json").read_text())
        pid = int(lease["voicePid"])
        if alive(pid) and str(VOICE_BIN) in ps_command(pid):
            os.kill(pid, signal.SIGTERM)
            actions.append(f"voice {pid} from lease: SIGTERM")
    except (OSError, ValueError, KeyError):
        pass
    actions.append(stop_group(st["pgid"], "backend launcher", ("run-browser.sh", "run-openrouter.sh", "chatbot serve")))
    port_free = False
    for _ in range(50):
        if not listener(st["port"]):
            port_free = True
            break
        time.sleep(0.2)
    shutil.rmtree(run / "scratch", ignore_errors=True)
    kept = sorted(str(p.relative_to(run)) for p in run.rglob("*") if p.is_file())
    report = {"actions": actions, "port": st["port"], "port_free": port_free, "scratch_removed": not (run / "scratch").exists(), "kept": kept}
    (run / "evidence" / f"cleanup-{stamp()}.json").write_text(json.dumps(report, indent=2) + "\n")
    for action in actions:
        print(action)
    print(f"port {st['port']} free: {port_free}; scratch removed; evidence kept in {run / 'evidence'} and {run / 'logs'}")
    sys.exit(0 if port_free else 1)

def main() -> None:
    parser = argparse.ArgumentParser(prog="pv.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    launch = sub.add_parser("launch", help="start an isolated backend on its own port")
    launch.add_argument("--port", type=int, default=0)
    launch.add_argument("--build", action="store_true", help="rebuild Voice.app first")
    launch.add_argument("--timeout", type=int, default=420)
    launch.set_defaults(fn=cmd_launch)
    for name, fn, text in (("doctor", cmd_doctor, "is this run worth driving?"), ("cleanup", cmd_cleanup, "stop what this run started, keep evidence"),
                           ("env", cmd_env, "print exports for manual drives")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--run")
        p.set_defaults(fn=fn)
    drive = sub.add_parser("drive", help="drive one feature and record evidence")
    drive.add_argument("feature", choices=sorted({f for f, _ in DRIVES}))
    drive.add_argument("--via", choices=["backend", "voice", "pi"], default="voice")
    drive.add_argument("--run")
    drive.set_defaults(fn=cmd_drive)
    args = parser.parse_args()
    args.fn(args)

if __name__ == "__main__":
    main()
