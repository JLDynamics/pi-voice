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
    # Raised from 5200 when the "Speaking for Pi" section landed: relaying
    # another agent's work out loud is a real capability and costs about 800
    # chars. The guard exists to catch unnoticed drift, so move it only with a
    # reason, and trim elsewhere before raising it again.
    prompt = build_voice_system_prompt(PERSONA, now=NOW)
    assert len(prompt) < 5500, f"voice prompt grew to {len(prompt)} chars; every turn pays for it"


def test_personality_is_opening_and_rules_follow():
    assert "perceptive, relaxed, warm, and quietly playful" in VOICE_SYSTEM_PROMPT_LEAD
    assert "Match the depth of your reply to the user's intent" in VOICE_SYSTEM_PROMPT_LEAD
    assert VOICE_SYSTEM_PROMPT_TAIL.strip().startswith("## Voice Rules")
    for tools in (None, ["bash"], ["ask_pi"], []):
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
    """Luna relays Pi's work now; without these she reads the terminal aloud."""
    prompt = build_voice_system_prompt(PERSONA, now=NOW)
    # She must know whose words a [PI] message carries.
    assert "[PI]" in prompt
    assert "Pi's words, not theirs" in prompt
    assert "never read the tag aloud" in prompt
    # ...and that the screen already holds the detail, so she gives the outcome.
    assert "you are the spoken one" in prompt
    assert "Do not recite code, paths, commands, tables, diffs, URLs or long numbers" in prompt
    assert "Detail only if asked" in prompt
    # ...and must not improve on or soften what Pi found.
    assert "never overrule or quietly improve them" in prompt
    assert "Say failures plainly" in prompt
    # ...and must not narrate the handover.
    assert "Do not narrate the handover" in prompt


def test_voice_prompt_says_pi_can_be_stopped():
    """She refused to stop Pi because nothing told her she could."""
    prompt = build_voice_system_prompt(PERSONA, now=NOW)
    assert "stop_pi" in prompt
    assert "never say you cannot" in prompt


FIXED_NOW = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)


def test_research_section_follows_the_published_tools():
    with_bash = build_voice_system_prompt("P", now=FIXED_NOW, tool_names=["bash"])
    assert "bash (curl)" in with_bash

    via_pi = build_voice_system_prompt("P", now=FIXED_NOW, tool_names=["ask_pi", "stop_pi"])
    assert "curl" not in via_pi
    assert "Let me have Pi check that" in via_pi
    assert "## Speaking for Pi" in via_pi
    assert "creates PDF/Markdown files" in via_pi
    assert "Pi owns permissions/approvals" in via_pi
    assert "including outside paths" in via_pi
    assert "missing save destination" in via_pi
    assert "not a new research question" in via_pi
    assert "Steer corrections with ask_pi" in via_pi

    none = build_voice_system_prompt("P", now=FIXED_NOW, tool_names=[])
    assert "curl" not in none
    assert "ask_pi in a short brief" not in none
    assert "cannot check it" in none


def test_unknown_tools_default_to_bash_guidance():
    assert build_voice_system_prompt("P", now=FIXED_NOW) == build_voice_system_prompt(
        "P", now=FIXED_NOW, tool_names=["bash"]
    )


def test_assistant_is_named_agent_and_knows_its_old_name() -> None:
    """The persona is Agent. Saved history can still call it Luna, so the prompt says that was its old name."""
    for names in (None, [], ["ask_pi", "stop_pi"], ["ask_claude", "stop_claude"]):
        prompt = build_voice_system_prompt("P", tool_names=names)
        assert prompt.startswith("You are Agent, an AI conversation partner"), names
        assert "You are Agent (earlier turns may say Luna, your old name; do not use it)." in prompt, names
