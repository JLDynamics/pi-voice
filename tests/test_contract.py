"""Conformance tests for contracts/pi-voice.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chatbot.LLM.voice_prompt import (
    VOICE_PI_HANDOFF,
    build_voice_system_prompt,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "contracts" / "pi-voice.json"


def _load_contract() -> dict[str, Any]:
    with open(CONTRACT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_contract_json_is_well_formed() -> None:
    contract = _load_contract()
    assert contract.get("version") == 1

    enums = contract.get("enums")
    assert isinstance(enums, dict) and enums, "contract must have non-empty enums"
    for enum_name, enum_values in enums.items():
        assert not enum_name.startswith("_")
        assert isinstance(enum_values, list), f"enum {enum_name} must be a list"
        assert len(enum_values) > 0, f"enum {enum_name} must not be empty"
        assert all(isinstance(v, str) and v for v in enum_values), f"enum {enum_name} values must be non-empty strings"

    known_enums = set(enums.keys())
    valid_base_types = {"string", "boolean"} | known_enums

    def validate_field_type(path: str, type_def: Any) -> None:
        assert isinstance(type_def, str), f"{path} type must be a string, got {type(type_def)}"
        base = type_def[:-1] if type_def.endswith("?") else type_def
        assert base in valid_base_types, (
            f"{path} has invalid type '{type_def}'. "
            f"Must be string, boolean, or an enum from {sorted(known_enums)}, optionally ending with '?'"
        )

    def validate_message_schema(section_name: str, messages: dict[str, Any]) -> None:
        for msg_name, fields in messages.items():
            if msg_name.startswith("_"):
                continue
            assert isinstance(fields, dict), f"{section_name}.{msg_name} must be an object"
            for field_name, type_def in fields.items():
                if field_name.startswith("_"):
                    continue
                validate_field_type(f"{section_name}.{msg_name}.{field_name}", type_def)

    # stdio toVoice & fromVoice
    stdio = contract.get("stdio", {})
    assert isinstance(stdio, dict) and stdio, "contract must have stdio section"
    to_voice = stdio.get("toVoice", {})
    assert isinstance(to_voice, dict) and to_voice, "stdio.toVoice must be non-empty"
    validate_message_schema("stdio.toVoice", to_voice)

    from_voice = stdio.get("fromVoice", {})
    assert isinstance(from_voice, dict) and from_voice, "stdio.fromVoice must be non-empty"
    validate_message_schema("stdio.fromVoice", from_voice)

    # voiceHistory
    vh = contract.get("voiceHistory", {})
    assert isinstance(vh, dict) and vh, "contract must have voiceHistory section"
    assert isinstance(vh.get("env"), str) and vh["env"], "voiceHistory.env must be non-empty string"
    turn = vh.get("turn", {})
    assert isinstance(turn, dict) and turn, "voiceHistory.turn must be an object"
    for field_name, type_def in turn.items():
        if not field_name.startswith("_"):
            validate_field_type(f"voiceHistory.turn.{field_name}", type_def)

    # tools
    tools = contract.get("tools", {})
    assert isinstance(tools, dict) and tools, "contract must have tools section"
    for tool_name, fields in tools.items():
        if tool_name.startswith("_"):
            continue
        assert isinstance(fields, dict), f"tools.{tool_name} must be an object"
        for field_name, type_def in fields.items():
            if not field_name.startswith("_"):
                validate_field_type(f"tools.{tool_name}.{field_name}", type_def)

    # channels
    channels = contract.get("channels", {})
    assert isinstance(channels, dict) and channels, "contract must have channels section"
    for chan_name, tag in channels.items():
        if not chan_name.startswith("_"):
            assert isinstance(tag, str) and tag, f"channels.{chan_name} must be a non-empty string"


def test_pi_voice_prompt_names_contract_tools_and_channels() -> None:
    contract = _load_contract()
    tools = contract.get("tools", {})
    tool_names = [k for k in tools if not k.startswith("_")]
    channels = contract.get("channels", {})
    channel_tags = [v for k, v in channels.items() if not k.startswith("_")]

    prompt = build_voice_system_prompt("Be concise.", tool_names=tool_names)

    for tool in tool_names:
        assert tool in prompt, f"Tool '{tool}' not found in assembled Pi voice prompt"
        assert tool in VOICE_PI_HANDOFF, f"Tool '{tool}' not found in VOICE_PI_HANDOFF"

    for tag in channel_tags:
        assert tag in prompt, f"Channel tag '{tag}' not found in assembled Pi voice prompt"
        assert tag in VOICE_PI_HANDOFF, f"Channel tag '{tag}' not found in VOICE_PI_HANDOFF"

    status_tag = channels["status"]
    assert f"{status_tag} is silent background progress" in prompt
    assert f"{status_tag} is silent background progress" in VOICE_PI_HANDOFF
