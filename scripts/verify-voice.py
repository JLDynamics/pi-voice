#!/usr/bin/env python3
"""API verify for the headless Voice backend.

Checks voice health and the current-code contract, then drives a short live
model reply over the realtime websocket. Voice.app has no window to click and
no sidecar beside it, so this is an API-and-turn harness.

Usage (from the repo root):

    uv run python scripts/verify-voice.py
    uv run python scripts/verify-voice.py --research   # bash/curl + verify-first + dated news RSS

Start the backend first (``./run-browser.sh``). This script never starts,
stops or restarts a service. It also confirms the backend runs this
checkout's current code (the health payload carries a source fingerprint),
which is how a stale backend that silently lacked server-side search was
caught.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VOICE_HTTP = os.environ.get("VOICE_HTTP", "http://127.0.0.1:8766")
VOICE_WS = os.environ.get("VOICE_WS", "ws://127.0.0.1:8766/v1/realtime")


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)
    out_dir: Path = Path("/tmp/voice-verify")

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append(Result(name, ok, detail))
        mark = "ok" if ok else "FAIL"
        extra = f" — {detail}" if detail else ""
        print(f"[{mark}] {name}{extra}")

    def ok(self) -> bool:
        return all(item.ok for item in self.results)


def http_json(
    url: str, method: str = "GET", body: dict[str, Any] | None = None, timeout: float = 12
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            payload: Any = json.loads(raw) if raw else {}
            return res.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else {"detail": str(exc)}
        except json.JSONDecodeError:
            payload = {"detail": raw.decode("utf-8", "replace")}
        return exc.code, payload
    except Exception as exc:
        return 0, {"detail": str(exc)}


def voice_binary() -> str:
    proc = subprocess.run(["ps", "-ax", "-o", "command="], capture_output=True, text=True)
    lines = [
        line.strip()
        for line in (proc.stdout or "").splitlines()
        if "/Voice.app/Contents/MacOS/Voice" in line and "grep" not in line
    ]
    return "\n".join(lines)


def describe_code(info: dict[str, Any]) -> tuple[bool, str]:
    """Whether a health payload says the process runs this checkout's current code."""
    fingerprint = info.get("fingerprint")
    if not fingerprint:
        return False, "no fingerprint: this process predates the current code and must be restarted"
    source = info.get("source") or ""
    if Path(source).resolve() != ROOT.resolve():
        return False, f"runs from {source}, not {ROOT}"
    if info.get("stale"):
        return False, f"fingerprint {fingerprint} no longer matches disk; restart to pick up the change"
    return True, f"fingerprint {fingerprint} pid {info.get('pid')}"


def verify_api(report: Report) -> None:
    code, health = http_json(f"{VOICE_HTTP}/health")
    if code == 200 and isinstance(health, dict) and "ready" in health:
        report.add("voice /health", bool(health.get("ready")), json.dumps(health))
        current, detail = describe_code(health)
        report.add("voice runs current code", current, detail)
        report.add(
            "voice health reports tool flag",
            "server_tools" in health,
            f"server_tools={health.get('server_tools')}",
        )
    else:
        report.add("voice /health", False, f"status {code} {health}")
        report.add("voice runs current code", False, "no health contract: legacy server")


@dataclass
class Turn:
    """What one text turn over the realtime socket produced."""

    transcript: str = ""
    audio_seconds: float = 0.0
    # (name, arguments) of tools the server ran inside the response.
    server_tools: list[tuple[str, str]] = field(default_factory=list)
    server_tool_outputs: list[str] = field(default_factory=list)
    # Tools the server handed to the client to run (none remain).
    client_tools: list[str] = field(default_factory=list)
    error: str = ""


def run_turn(instructions: str, text: str, timeout: float = 60, tools: list[dict[str, Any]] | None = None) -> Turn:
    """Send one user text turn and collect the reply until the response ends."""
    import asyncio

    import websockets

    async def connect() -> Any:
        """Open a session, waiting out the moment the previous turn's slot is released.

        The local backend runs one pipeline; after a disconnect the slot frees
        once SESSION_END drains through the handlers, which can take a second.
        """
        deadline = time.monotonic() + 20
        while True:
            ws = await websockets.connect(VOICE_WS, open_timeout=8, close_timeout=3, max_size=None)
            created = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            if created.get("type") == "session.created":
                return ws
            await ws.close()
            error = created.get("error") or {}
            busy = error.get("type") in {"session_limit_reached", "server_starting"}
            if not busy or time.monotonic() > deadline:
                raise RuntimeError(f"expected session.created, got {created.get('type')}: {error.get('message', '')}")
            await asyncio.sleep(0.5)

    async def once() -> Turn:
        turn = Turn()
        pcm_bytes = 0
        session: dict[str, Any] = {"type": "realtime", "instructions": instructions}
        if tools is not None:
            session["tools"] = tools
            session["tool_choice"] = "auto"
        ws = await connect()
        try:
            await ws.send(json.dumps({"type": "session.update", "session": session}))
            await ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
                    }
                )
            )
            await ws.send(json.dumps({"type": "response.create"}))
            deadline = time.monotonic() + timeout
            transcript: list[str] = []
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.monotonic()))
                event = json.loads(raw)
                kind = event.get("type") or ""
                if kind in {
                    "response.audio_transcript.delta",
                    "response.output_audio_transcript.delta",
                    "response.text.delta",
                    "response.output_text.delta",
                }:
                    transcript.append(event.get("delta") or "")
                elif kind in {"response.audio.delta", "response.output_audio.delta"}:
                    pcm_bytes += len(event.get("delta") or "") * 3 // 4
                elif kind == "conversation.item.created":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call":
                        turn.server_tools.append((item.get("name") or "", item.get("arguments") or ""))
                    elif item.get("type") == "function_call_output":
                        turn.server_tool_outputs.append(item.get("output") or "")
                elif kind == "response.output_item.done":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call":
                        turn.client_tools.append(item.get("name") or "")
                elif kind == "response.done":
                    break
                elif kind == "error":
                    turn.error = str(event.get("error") or event)
                    break
            turn.transcript = "".join(transcript).strip()
            # 24 kHz 16-bit mono.
            turn.audio_seconds = pcm_bytes / (24000 * 2)
        finally:
            await ws.close()
        return turn

    return asyncio.run(once())


def spoken_pcm16(text: str = "hello there, can you hear me") -> bytes:
    """Real macOS speech, 16 kHz PCM16 mono — the same format Voice.app sends."""
    import tempfile
    import wave

    work = Path(tempfile.mkdtemp(prefix="voice-mic-"))
    aiff = work / "spoken.aiff"
    wav = work / "spoken.wav"
    subprocess.run(
        ["say", "-o", str(aiff), text],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", str(aiff), str(wav)],
        check=True,
        capture_output=True,
    )
    with wave.open(str(wav), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise RuntimeError(f"unexpected spoken wav format: {handle.getparams()}")
        frames = handle.readframes(handle.getnframes())
    return frames


def gate_pcm16(pcm: bytes, threshold_dbfs: float = -48.0, min_brightness: float = 0.035) -> tuple[bytes, int, int]:
    """Mirror the Mac close-talk gate so verify covers the path Voice.app sends."""
    import array
    import math

    samples = array.array("h")
    samples.frombytes(pcm)
    open_floor = (10 ** (threshold_dbfs / 20.0)) * 32768.0
    hold_floor = (10 ** ((threshold_dbfs - 10.0) / 20.0)) * 32768.0
    hold = 0
    out = array.array("h")
    chunk = 512
    open_chunks = 0
    total = 0
    for start in range(0, len(samples), chunk):
        part = samples[start : start + chunk]
        if not part:
            break
        total += 1
        rms = math.sqrt(sum(int(sample) * int(sample) for sample in part) / len(part))
        deltas = [abs(int(part[i]) - int(part[i - 1])) for i in range(1, len(part))]
        brightness = (sum(deltas) / max(len(deltas), 1)) / max(rms, 1.0)
        close_talk = rms >= open_floor and brightness >= min_brightness
        if close_talk or (hold > 0 and rms >= hold_floor):
            hold = int(0.250 * 16_000)
            out.extend(part)
            open_chunks += 1
        elif hold > 0:
            hold = max(0, hold - len(part))
            if hold > 0:
                out.extend(part)
                open_chunks += 1
            else:
                out.extend([0] * len(part))
        else:
            out.extend([0] * len(part))
    return out.tobytes(), open_chunks, total


def run_mic_probe(timeout: float = 25) -> dict[str, Any]:
    """Send spoken PCM through the Mac gate, then the live VAD."""
    import asyncio
    import base64

    import websockets

    async def connect() -> Any:
        deadline = time.monotonic() + 20
        while True:
            ws = await websockets.connect(VOICE_WS, open_timeout=8, close_timeout=3, max_size=None)
            created = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            if created.get("type") == "session.created":
                return ws
            await ws.close()
            error = created.get("error") or {}
            busy = error.get("type") in {"session_limit_reached", "server_starting"}
            if not busy or time.monotonic() > deadline:
                raise RuntimeError(f"expected session.created, got {created.get('type')}: {error.get('message', '')}")
            await asyncio.sleep(0.5)

    async def once() -> dict[str, Any]:
        result: dict[str, Any] = {"speech_started": False, "speech_stopped": False, "error": "", "events": []}
        raw = spoken_pcm16()
        pcm, open_chunks, total_chunks = gate_pcm16(raw)
        result["gate_open_frac"] = open_chunks / max(total_chunks, 1)
        if result["gate_open_frac"] < 0.4:
            result["error"] = f"close-talk gate muted spoken audio ({open_chunks}/{total_chunks} chunks)"
            return result
        ws = await connect()
        try:
            await ws.send(json.dumps({"type": "session.update", "session": {"type": "realtime"}}))
            # Match the Mac client: 40ms of 16 kHz PCM16 mono per append.
            chunk = 1280
            for offset in range(0, len(pcm), chunk):
                await ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm[offset : offset + chunk]).decode(),
                        }
                    )
                )
            silence = bytes(chunk * 25)
            for offset in range(0, len(silence), chunk):
                await ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(silence[offset : offset + chunk]).decode(),
                        }
                    )
                )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    incoming = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except TimeoutError:
                    if result["speech_started"]:
                        break
                    continue
                event = json.loads(incoming)
                kind = event.get("type") or ""
                result["events"].append(kind)
                if kind == "input_audio_buffer.speech_started":
                    result["speech_started"] = True
                elif kind == "input_audio_buffer.speech_stopped":
                    result["speech_stopped"] = True
                    break
                elif kind == "error":
                    result["error"] = str(event.get("error") or event)
                    break
        finally:
            await ws.close()
        return result

    return asyncio.run(once())


def verify_talk(report: Report) -> None:
    try:
        import websockets  # noqa: F401
    except ImportError:
        report.add("live model reply", False, "websockets package missing")
        return
    try:
        turn = run_turn("Reply with the single word pong and nothing else.", "ping", timeout=25)
        report.add("live model reply", bool(turn.transcript) and not turn.error, turn.transcript[:180] or turn.error)
    except Exception as exc:
        report.add("live model reply", False, str(exc) or exc.__class__.__name__)
    time.sleep(1.2)
    try:
        probe = run_mic_probe()
        started = probe.get("speech_started") is True and not probe.get("error")
        report.add(
            "live mic speech_started",
            started,
            (
                f"VAD accepted spoken PCM; gate open {probe.get('gate_open_frac', 0):.0%}"
                if started
                else f"no speech_started: {probe}"
            ),
        )
    except Exception as exc:
        report.add("live mic speech_started", False, str(exc) or exc.__class__.__name__)


# Same schemas this branch publishes. Without these on session.update the model
# cannot call bash even when the server is willing to run it.
RESEARCH_TOOLS = [
    {
        "type": "function",
        "name": "bash",
        "description": "Run one short research shell command. Use curl to search or fetch a public page.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "A curl-based command."},
                "timeout": {"type": "number", "description": "Seconds to wait."},
            },
            "required": ["command"],
        },
    },
]


def verify_research(report: Report) -> None:
    """The model searches and reads a page inside its own reply, on the server."""
    try:
        import websockets  # noqa: F401
    except ImportError:
        report.add("research turn", False, "websockets package missing")
        return
    instructions = (
        "You are being tested. First say one short line such as 'Let me check that.' and in the same response "
        "call bash with command: curl -sL https://www.iana.org/help/example-domains | head -c 4000. "
        "Then answer in one short spoken sentence based on what the page says. Never read URLs aloud."
    )
    try:
        turn = run_turn(instructions, "What is the IANA example domains page for?", timeout=90, tools=RESEARCH_TOOLS)
    except Exception as exc:
        report.add("research turn", False, str(exc))
        return
    names = [name for name, _ in turn.server_tools]
    report.add(
        "bash ran on the server",
        "bash" in names and not turn.error,
        f"server tools: {names}; client tools: {turn.client_tools}" + (f"; error: {turn.error}" if turn.error else ""),
    )
    outputs = " ".join(turn.server_tool_outputs)
    report.add(
        "curl returned page text",
        "bash" in names and ("example" in outputs.casefold() or "iana" in outputs.casefold()),
        outputs[:200].replace("\n", " ") if outputs else "no tool output seen",
    )
    report.add(
        "model answered after researching",
        bool(turn.transcript) and turn.audio_seconds > 0.5,
        f"{turn.audio_seconds:.1f}s audio: {turn.transcript[:160]}",
    )
    report.add(
        "no tool was pushed to the client",
        not turn.client_tools,
        "search and page reading stayed server-side" if not turn.client_tools else f"client tools: {turn.client_tools}",
    )


def verify_research_initiative(report: Report) -> None:
    """A verify-first question must fetch inside the reply without being handed a curl command."""
    try:
        import websockets  # noqa: F401
    except ImportError:
        report.add("research initiative", False, "websockets package missing")
        return
    instructions = (
        "You are an AI conversation partner: perceptive, relaxed, warm, and quietly playful. "
        "You enjoy exploring ideas and have something thoughtful to contribute."
    )
    try:
        turn = run_turn(
            instructions,
            "Who is the current president of the United States?",
            timeout=90,
            tools=RESEARCH_TOOLS,
        )
    except Exception as exc:
        report.add("research initiative", False, str(exc))
        return
    names = [name for name, _ in turn.server_tools]
    report.add(
        "verify-first called bash",
        "bash" in names and not turn.error,
        f"server tools: {names}; client tools: {turn.client_tools}; "
        f"{turn.transcript[:160]!r}" + (f"; error: {turn.error}" if turn.error else ""),
    )


def verify_news_research(report: Report) -> None:
    """Dated RSS must be stamped, and an undated Google News URL must be flagged."""
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from chatbot.LLM.curl_bash import finalize_research_output, run_research_command
    except Exception as exc:
        report.add("news research import", False, str(exc))
        return

    today = datetime.now().astimezone()
    undated = finalize_research_output(
        "Old follow-up\nTue, 01 Sep 2025 12:00:00 GMT\nNewer item\n"
        + today.strftime("%a, %d %b %Y 18:00:00 GMT")
        + "\n",
        command="curl -sL 'https://news.google.com/rss/search?q=world+news'",
        now=today,
    )
    report.add(
        "undated google news is flagged",
        "no when:1d window" in undated and "Checked at" in undated,
        undated.split("\n", 1)[0],
    )

    query = today.strftime("%B+%d+%Y")
    command = (
        "curl -sL "
        f"'https://news.google.com/rss/search?q=world+news+when:1d+{query}&hl=en-US&gl=US&ceid=US:en' "
        '| python3 -c "import sys,re; t=sys.stdin.read(); '
        "print('items', len(re.findall(r'<item>', t))); print(t[:3000])\""
    )
    try:
        output = run_research_command(command, timeout=20, now=today)
    except Exception as exc:
        report.add("live news rss", False, str(exc))
        return
    report.add(
        "live news rss stamped",
        "Checked at" in output and str(today.year) in output,
        output[:220].replace("\n", " "),
    )
    report.add(
        "live dated rss skips window hint",
        "no when:1d window" not in output,
        output[:160].replace("\n", " "),
    )
    usable = "almost no usable text" not in output and "items 0" not in output
    report.add("live news rss returned headlines", usable, output[:220].replace("\n", " "))


def verify_han_turn(report: Report) -> None:
    """No Chinese voice exists, but a stray Han character must not break the turn.

    The Kokoro-era run splitter that used to drop Han is gone with that backend
    (there is no espeak-ng fallback left to guard against), so Han now reaches
    the English Siri voice as written. This check only confirms the turn still
    completes with audio and no error.
    """
    try:
        import websockets  # noqa: F401
    except ImportError:
        report.add("han turn completes", False, "websockets package missing")
        return
    sentence = "Huawei, or \u534e\u4e3a, is a company."
    try:
        turn = run_turn(
            f"Reply with exactly this sentence and nothing else: {sentence}", "Say the test sentence.", timeout=45
        )
    except Exception as exc:
        report.add("han turn completes", False, str(exc))
        return
    report.add(
        "han turn completes",
        turn.audio_seconds > 1.0 and not turn.error,
        f"{turn.audio_seconds:.1f}s audio: {turn.transcript[:120]}" + (f"; error: {turn.error}" if turn.error else ""),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the local voice backend over its real WebSocket")
    # Voice is headless: there is no panel to tour. Still accepted so existing
    # commands and docs that pass it keep working.
    parser.add_argument("--skip-ui", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-talk", action="store_true")
    parser.add_argument(
        "--research",
        action="store_true",
        help="Also run a turn that must bash/curl a page on the server, a dated news RSS check, and a Chinese-name TTS turn",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out or Path(f"/tmp/voice-verify-{stamp}")
    out.mkdir(parents=True, exist_ok=True)
    report = Report(out_dir=out)
    print(f"verify root={ROOT}")
    print(f"verify out={out}")

    verify_api(report)
    if args.research:
        verify_news_research(report)
    if not args.skip_talk:
        verify_talk(report)
        if args.research:
            verify_research(report)
            verify_research_initiative(report)
            verify_han_turn(report)

    failed = [item for item in report.results if not item.ok]
    summary = {
        "ok": not failed,
        "passed": sum(1 for item in report.results if item.ok),
        "failed": [item.name for item in failed],
        "out": str(out),
        "binary": voice_binary(),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print()
    print(json.dumps(summary, indent=2))
    if failed:
        print(f"\n{len(failed)} check(s) failed. Summary in {out}/summary.json")
        return 1
    print(f"\nAll {len(report.results)} checks passed. Results in {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
