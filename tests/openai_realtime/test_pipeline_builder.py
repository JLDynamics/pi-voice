from threading import Event
from types import SimpleNamespace

from chatbot.api.openai_realtime.server import RealtimeServer
from chatbot.s2s_pipeline import build_pipeline, parse_arguments


def test_default_profile_is_the_single_retained_pipeline():
    args = parse_arguments([])

    assert args.stt_backend.name == "native-stt"
    assert args.llm_backend.name == "responses-api"
    assert args.tts_backend.name == "siri"


def test_serve_builds_one_browser_pipeline(monkeypatch):
    args = parse_arguments([])
    handler = object()
    unit = SimpleNamespace(handlers=[handler])
    monkeypatch.setattr("chatbot.s2s_pipeline._build_pipeline_unit", lambda **_kwargs: unit)
    stop_event = Event()

    manager = build_pipeline(args, stop_event)

    server = manager.handlers[0]
    assert isinstance(server, RealtimeServer)
    assert server.unit is unit
    assert server.stop_event is stop_event
    assert manager.handlers[1] is handler
