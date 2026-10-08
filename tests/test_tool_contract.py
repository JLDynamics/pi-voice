"""The two halves of tool execution must agree on who runs what.

The Swift app publishes the tool definitions (``session.update``) and runs the
tools the server forwards to it; the Python LLM handler runs the research
tools itself. If a name is added on one side and not the other, a call either
never executes (server forwards it, client has no case) or executes with no
progress label. Parse the Swift sources rather than grep prose, so wording can
change freely while the contract stays checked.
"""

from __future__ import annotations

import re
from pathlib import Path

from chatbot.LLM.server_tools import SERVER_TOOL_NAMES

ROOT = Path(__file__).resolve().parents[1]
SESSION_SOURCES = ROOT / "macos" / "Voice" / "Sources" / "Session"

# No tools stay in the app process any more: `screenshot` was removed, so
# every tool the model can call runs on the server.
CLIENT_TOOL_NAMES = frozenset()


def _swift_string_set(source: str, name: str) -> set[str]:
    match = re.search(rf"static let {name}: Set<String> = \[(.*?)\]", source, re.S)
    assert match, f"{name} not found"
    return set(re.findall(r'"([a-z_]+)"', match.group(1)))


def _published_tool_names(source: str) -> set[str]:
    body = source.split("func activeToolDefinitions()", 1)[1].split("private static func tool(", 1)[0]
    return set(re.findall(r'Self\.tool\(\s*"([a-z_]+)"', body))


def _dispatched_tool_names(source: str) -> set[str]:
    body = source.split("public func run(name: String, argsJson: String)", 1)[1]
    body = body.split("// ── Tool Implementations ──", 1)[0]
    return set(re.findall(r'case "([a-z_]+)":', body))


def test_swift_and_python_agree_on_server_side_tools():
    tools = (SESSION_SOURCES / "VoiceTools.swift").read_text()
    assert _swift_string_set(tools, "serverSideTools") == set(SERVER_TOOL_NAMES)


def test_every_published_tool_has_exactly_one_owner():
    tools = (SESSION_SOURCES / "VoiceTools.swift").read_text()
    published = _published_tool_names(tools)
    assert published, "no tool definitions found; the definition shape changed"
    owned = set(SERVER_TOOL_NAMES) | CLIENT_TOOL_NAMES
    assert published <= owned, f"tools nobody runs: {sorted(published - owned)}"
    assert not (set(SERVER_TOOL_NAMES) & CLIENT_TOOL_NAMES)


def test_client_dispatch_covers_the_client_tools_and_nothing_else():
    tools = (SESSION_SOURCES / "VoiceTools.swift").read_text()
    assert _dispatched_tool_names(tools) == CLIENT_TOOL_NAMES


def test_headless_voice_publishes_only_the_handoff():
    """Pi voice is one handoff plus a spoken cancel. The old poll tools are gone."""
    source = (SESSION_SOURCES / "LiveVoiceBackend.swift").read_text()
    tools = source.split("func headlessTalkerTools()", 1)[1].split("private static func json(", 1)[0]
    published = set(re.findall(r'"name": "([a-z_]+)"', tools))
    assert published == {"spawn_thinking", "stop_thinking"}
    dispatch = source.split("private func executeTool(", 1)[1].split("private func sendToolOutput(", 1)[0]
    assert set(re.findall(r'if name == "([a-z_]+)"', dispatch)) == published
    for retired in ("ask_pi", "stop_pi", "pi_status", "pi_results"):
        assert f'"name": "{retired}"' not in source
    assert '"name": "bash"' not in tools
    assert '"name": "screenshot"' not in source
    description = source.split('"name": "spawn_thinking"', 1)[1].split('"name": "stop_thinking"', 1)[0]
    assert "what is on screen" in description
    assert "click, type, fill forms, navigate" in description
    assert "You cannot see the screen or click yourself" in description
    assert "screenshot" not in description


def test_every_tool_has_a_progress_label():
    """The panel shows a label for tool calls from either side while they run."""
    session = (SESSION_SOURCES / "VoiceSession.swift").read_text()
    tools = (SESSION_SOURCES / "VoiceTools.swift").read_text()
    labelled = set(re.findall(r'case "([a-z_]+)": desc =', session))
    missing = _published_tool_names(tools) - labelled
    assert not missing, f"tools with no progress label: {sorted(missing)}"
