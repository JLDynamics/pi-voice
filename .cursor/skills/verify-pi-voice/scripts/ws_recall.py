#!/usr/bin/env python3
"""Inject a startup pack the way Voice.app's replayHistory does, then ask one question.

usage: ws_recall.py <ws_url> <pack.json> <question> <out.json>
Runs with the repo's venv python (needs `websockets`).
"""

import asyncio
import json
import sys
import time

import websockets


async def connect(url: str):
    deadline = time.monotonic() + 20
    while True:
        ws = await websockets.connect(url, open_timeout=8, close_timeout=3, max_size=None)
        created = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
        if created.get("type") == "session.created":
            return ws
        await ws.close()
        busy = (created.get("error") or {}).get("type") in {"session_limit_reached", "server_starting"}
        if not busy or time.monotonic() > deadline:
            raise RuntimeError(f"expected session.created, got {created}")
        await asyncio.sleep(0.5)


def message(role: str, text: str) -> dict:
    kind = "output_text" if role == "assistant" else "input_text"
    return {
        "type": "conversation.item.create",
        "item": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]},
    }


async def main(url: str, pack_path: str, question: str, out_path: str) -> None:
    pack = json.loads(open(pack_path).read())
    result = {"events": [], "transcript": "", "tool_calls": [], "responses_before_question": 0, "error": ""}
    ws = await connect(url)
    try:
        await ws.send(json.dumps({"type": "session.update", "session": {"type": "realtime"}}))
        for turn in pack:
            await ws.send(json.dumps(message(turn["role"], turn["text"])))
        settle = time.monotonic() + 3
        while time.monotonic() < settle:
            try:
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.5))
            except asyncio.TimeoutError:
                continue
            result["events"].append(event.get("type"))
            if event.get("type") == "response.created":
                result["responses_before_question"] += 1
        await ws.send(json.dumps(message("user", question)))
        await ws.send(json.dumps({"type": "response.create"}))
        deadline = time.monotonic() + 60
        parts = []
        while time.monotonic() < deadline:
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.monotonic())))
            kind = event.get("type") or ""
            result["events"].append(kind)
            if kind in {
                "response.output_audio_transcript.delta",
                "response.audio_transcript.delta",
                "response.output_text.delta",
                "response.text.delta",
            }:
                parts.append(event.get("delta") or "")
            elif kind == "response.output_item.done" and (event.get("item") or {}).get("type") == "function_call":
                result["tool_calls"].append((event.get("item") or {}).get("name"))
            elif kind == "response.done":
                break
            elif kind == "error":
                result["error"] = str(event.get("error") or event)
                break
        result["transcript"] = "".join(parts).strip()
    finally:
        await ws.close()
        open(out_path, "w").write(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    asyncio.run(main(*sys.argv[1:5]))
