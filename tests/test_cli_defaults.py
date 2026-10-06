import pytest

from chatbot.cli import parse_command
from chatbot.s2s_pipeline import parse_arguments


def test_cli_has_browser_server_only():
    command, remaining = parse_command(["serve", "--port", "9876"])
    assert command == "serve"
    assert remaining == ["--port", "9876"]


def test_current_defaults_are_mac_voice_profile():
    args = parse_arguments([])
    assert args.realtime_server_kwargs.port == 8766
    assert args.stt_backend.name == "native-stt"
    assert args.llm_backend.name == "responses-api"
    assert args.tts_backend.name == "siri"
    assert args.llm_backend.config["model_name"] == "z-ai/glm-5.3-flash"
    assert args.tts_backend.config["voice"] == "en-US-F"


def test_removed_commands_and_backends_are_rejected():
    with pytest.raises(SystemExit):
        parse_command(["local"])
    with pytest.raises(SystemExit):
        parse_arguments(["--stt", "whisper"])
    with pytest.raises(SystemExit):
        parse_arguments(["--stt", "grok-stt"])
    with pytest.raises(SystemExit):
        parse_arguments(["--tts", "kokoro"])
    with pytest.raises(SystemExit):
        parse_arguments(["--tts", "vibevoice"])
