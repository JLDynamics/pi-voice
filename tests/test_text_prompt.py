from datetime import datetime, timezone

from chatbot.LLM.text_prompt import build_text_system_prompt

NOW = datetime(2026, 9, 8, 21, 35, tzinfo=timezone.utc)


def test_text_prompt_keeps_persona_in_session_prompt():
    prompt = build_text_system_prompt("Be helpful.", now=NOW)

    assert "Be helpful." in prompt
    assert "You are a helpful assistant in a text conversation." in prompt
    assert "Current date and time: Tuesday, September 8, 2026, 9:35 PM (UTC)." in prompt


def test_text_prompt_allows_markdown_and_drops_voice_rules():
    prompt = build_text_system_prompt("Be helpful.", now=NOW)

    assert "Use markdown when it helps" in prompt
    assert "You drive research yourself with bash (curl)" in prompt
    assert "Do not wait for the user to tell you that you were wrong." in prompt
    assert "when:1d" in prompt
    assert "last 24 hours" in prompt
    assert "There is no read_page tool and no Chrome bridge." in prompt
    assert "say so plainly rather than guessing at its contents" in prompt
    assert "For the page open in Chrome, call read_page" not in prompt
    # No spoken-channel rules leak into the text prompt.
    assert "Speech is the default" not in prompt
    assert "Treat speech transcripts as imperfect" not in prompt
    assert "Say one short natural line before a slow tool" not in prompt


def test_prompt_assembly_byte_identical():
    text_prompt = build_text_system_prompt("Be helpful.", tool_section="TOOL DEFINITIONS", now=NOW)
    expected_text = (
        "You are a helpful assistant in a text conversation.\n\n"
        "## Context\n"
        "Current date and time: Tuesday, September 8, 2026, 9:35 PM (UTC).\n\n"
        "## Session Prompt\n"
        "Be helpful.\n\n"
        "TOOL DEFINITIONS\n\n"
        "## Knowledge and research\n"
        "- Your training data has a cutoff; the current date is given above. You drive research yourself with bash (curl) in this same reply. On every question, if the fact may have changed since your cutoff (who holds a role, versions, scores, prices, news, schedules), fetch before you answer. Stable knowledge can be answered immediately. Do not wait for the user to tell you that you were wrong. Office-holders: curl Wikipedia or an official page. Latest news: dated RSS (Google News with when:1d plus today's date), last 24 hours only. There is no web_search tool. There is no read_page tool and no Chrome bridge. A page that needs a login, or that curl cannot open, cannot be read here: say so plainly rather than guessing at its contents. Several tool calls in one turn are fine.\n"
        "- Base the answer on what the tools returned and say where it came from when that matters. Never claim to have searched or read something unless the tool call actually returned it.\n\n"
        "## Text Rules\n"
        "- Write clearly and directly. Match length to the request: concise for simple questions, fuller when the task genuinely needs it.\n"
        "- Use markdown when it helps (lists, code blocks, tables, emphasis); don't over-format simple answers.\n"
        "- This is a written channel: no spoken-style filler and no action/emote text like *laughs*.\n"
        "- Use tools when they help fulfill the request. No preamble sentence is required before a tool call; just call it and use the result.\n"
    )
    assert text_prompt == expected_text
