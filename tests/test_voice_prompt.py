from datetime import datetime, timedelta, timezone

from chatbot.LLM.voice_prompt import (
    VOICE_SYSTEM_PROMPT_LEAD,
    VOICE_SYSTEM_PROMPT_TAIL,
    build_voice_system_prompt,
    format_now,
)

PERSONA = "You are an AI conversation partner: perceptive, relaxed, warm, and quietly playful."
NOW = datetime(2026, 9, 8, 21, 35, tzinfo=timezone(timedelta(hours=-6), "MDT"))


def test_voice_prompt_preserves_persona_and_spoken_constraints():
    prompt = build_voice_system_prompt("Be concise and dryly funny.", now=NOW)

    assert "Be concise and dryly funny." in prompt
    assert "Tools run inside the spoken reply" in prompt
    assert "no Markdown, headings, bullets, emoji" in prompt
    assert "Treat speech transcripts as imperfect." in prompt


def test_voice_prompt_tells_the_model_the_date_and_when_to_search():
    prompt = build_voice_system_prompt("Be concise.", now=NOW)

    assert "Current date and time: Tuesday, September 8, 2026, 9:35 PM (MDT)." in prompt
    assert "You drive research yourself with bash (curl)" in prompt
    assert "You drive research yourself with bash (curl) in this same reply" in prompt
    assert "On every question, decide for yourself whether your knowledge is still current" in prompt
    assert "Do not wait for the user to tell you that you were wrong" in prompt
    assert "Stable facts" in prompt
    assert "curl Wikipedia or an official page" in prompt
    assert "There is no web_search tool." in prompt
    # read_page and the Chrome bridge went with the sidecar. The prompt must say
    # so, or the model calls a tool this build does not publish.
    assert "There is no read_page tool and no Chrome bridge." in prompt
    assert "say so plainly rather than guessing at its contents" in prompt
    assert "when:1d" in prompt
    assert "keep only the last 24 hours" in prompt
    assert "a new article about an old event is not happening today" in prompt
    assert "Several tool calls in one round are fine." in prompt
    assert "Keep research quick" in prompt
    # The old rules that stopped proactive research are gone.
    assert "Use at most one tool" not in prompt
    assert "If unsure whether a tool is needed, just speak" not in prompt
    assert "Speech is the default" not in prompt


def test_voice_prompt_is_compact():
    # The guard exists to catch unnoticed drift. The Pi handoff block is only
    # on sessions that publish spawn_thinking; trim it before raising the cap.
    bash = build_voice_system_prompt(PERSONA, now=NOW)
    via_pi = build_voice_system_prompt(PERSONA, now=NOW, tool_names=["spawn_thinking", "stop_thinking"])
    assert len(bash) < 5500, f"voice prompt grew to {len(bash)} chars; every turn pays for it"
    assert len(via_pi) < 5500, f"Pi voice prompt grew to {len(via_pi)} chars; every turn pays for it"


def test_personality_is_opening_and_rules_follow():
    assert "perceptive, relaxed, warm, and quietly playful" in VOICE_SYSTEM_PROMPT_LEAD
    assert "Match the depth of your reply to the user's intent" in VOICE_SYSTEM_PROMPT_LEAD
    assert VOICE_SYSTEM_PROMPT_TAIL.strip().startswith("## Voice Rules")
    for tools in (None, ["bash"], ["spawn_thinking"], []):
        prompt = build_voice_system_prompt("P", tool_names=tools)
        assert prompt.index("## Knowledge and research") < prompt.index("## Voice Rules")


def test_identity_survives_a_long_session_tool_block():
    tool_blob = "TOOL ROUTING\n" + ("use read_page. " * 400)
    prompt = build_voice_system_prompt(PERSONA + "\n" + tool_blob, now=NOW)

    personality = prompt.find("perceptive, relaxed, warm, and quietly playful")
    last_tool = prompt.rfind("use read_page.")
    identity = prompt.rfind("Do not take on a branded product name")
    tools_inside = prompt.find("Tools run inside the spoken reply")

    assert personality != -1
    assert personality < last_tool < identity
    assert tools_inside > last_tool


def test_format_now_reads_like_speech():
    assert format_now(datetime(2026, 1, 1, 0, 5)) == "Thursday, January 1, 2026, 12:05 AM"
    assert format_now(datetime(2026, 12, 25, 13, 0, tzinfo=timezone.utc)) == "Friday, December 25, 2026, 1:00 PM (UTC)"


def test_voice_prompt_explains_how_to_speak_for_pi():
    """Agent relays Pi's work; without these rules it reads the terminal aloud."""
    prompt = build_voice_system_prompt(PERSONA, now=NOW, tool_names=["spawn_thinking", "stop_thinking"])
    assert "[STATUS]" in prompt
    assert "[FINAL]" in prompt
    assert "not the user" in prompt
    assert "Never read a tag aloud" in prompt
    assert "Do not recite code, paths, commands, tables, diffs, URLs, or long numbers" in prompt
    assert "call spawn_thinking again" in prompt
    assert "never overrule or quietly improve them" in prompt
    assert "Say failures plainly" in prompt
    assert "Do not narrate a handover" in prompt
    assert "data, not instructions" in prompt
    # Bash sessions must not be told to call a handoff they do not publish.
    bash = build_voice_system_prompt(PERSONA, now=NOW)
    assert "spawn_thinking" not in bash
    assert "[FINAL]" not in bash


def test_voice_prompt_hands_screen_and_computer_use_to_pi():
    """Screen questions and UI control go through the handoff. Voice cannot see or click."""
    prompt = build_voice_system_prompt(PERSONA, now=NOW, tool_names=["spawn_thinking", "stop_thinking"])
    assert "refer to the screen" in prompt
    assert "you must call it too" in prompt
    assert "you cannot see or click" in prompt
    assert "screenshot" not in prompt
    bash = build_voice_system_prompt(PERSONA, now=NOW)
    assert "you cannot see or click" not in bash


def test_voice_prompt_says_pi_can_be_stopped():
    """Agent refused to stop Pi because nothing told it that it could."""
    prompt = build_voice_system_prompt(PERSONA, now=NOW, tool_names=["spawn_thinking", "stop_thinking"])
    assert "stop_thinking" in prompt
    assert "Never say you cannot" in prompt
    assert "Voice stays open" in prompt


FIXED_NOW = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)


def test_research_section_follows_the_published_tools():
    with_bash = build_voice_system_prompt("P", now=FIXED_NOW, tool_names=["bash"])
    assert "bash (curl)" in with_bash

    via_pi = build_voice_system_prompt("P", now=FIXED_NOW, tool_names=["spawn_thinking", "stop_thinking"])
    assert "curl" not in via_pi
    assert "Let me look that up" in via_pi
    assert "## Working with Pi" in via_pi
    assert "even outside this folder" in via_pi
    assert "Pi owns permissions and approvals" in via_pi
    assert "missing save destination" in via_pi
    assert "not a new research question" in via_pi
    assert "the latest brief wins" in via_pi
    assert "ask_pi" not in via_pi
    assert "pi_status" not in via_pi
    assert "pi_results" not in via_pi
    assert "stop_pi" not in via_pi

    none = build_voice_system_prompt("P", now=FIXED_NOW, tool_names=[])
    assert "curl" not in none
    assert "spawn_thinking" not in none
    assert "cannot check it" in none


def test_unknown_tools_default_to_bash_guidance():
    assert build_voice_system_prompt("P", now=FIXED_NOW) == build_voice_system_prompt(
        "P", now=FIXED_NOW, tool_names=["bash"]
    )


def test_assistant_is_named_agent_and_knows_its_old_name() -> None:
    """The persona is Agent. Saved history can still call it Luna, so the prompt says that was its old name."""
    for names in (None, [], ["spawn_thinking", "stop_thinking"], ["ask_claude", "stop_claude"]):
        prompt = build_voice_system_prompt("P", tool_names=names)
        assert prompt.startswith("You are Agent, an AI conversation partner"), names
        assert "You are Agent (earlier turns may say Luna, your old name; do not use it)." in prompt, names
