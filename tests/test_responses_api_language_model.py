import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from openai import Stream
from openai.types.realtime.conversation_item import (
    RealtimeConversationItemAssistantMessage,
    RealtimeConversationItemFunctionCallOutput,
)
from openai.types.realtime.realtime_response_create_params import RealtimeResponseCreateParams
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseTextDeltaEvent,
)
from openai.types.responses.response_output_text import ResponseOutputText

import chatbot.LLM.base_openai_compatible_language_model as base_openai_compatible_language_model
from chatbot.api.openai_realtime.runtime_config import RuntimeConfig
from chatbot.LLM.base_openai_compatible_language_model import WARMUP_MAX_RETRIES
from chatbot.LLM.chat import Chat
from chatbot.LLM.chat_factories import make_user_message
from chatbot.LLM.responses_api_language_model import ResponsesApiModelHandler
from chatbot.pipeline.cancel_scope import CancelScope
from chatbot.pipeline.messages import EndOfResponse, GenerateResponseRequest, LLMResponseChunk, TokenUsage


def _make_text_delta_event(text):
    evt = MagicMock(spec=ResponseTextDeltaEvent)
    evt.type = "response.output_text.delta"
    evt.delta = text
    return evt


def _make_output_item_done_event(role="assistant", content="Hello.", item_type="message"):
    evt = MagicMock(spec=ResponseOutputItemDoneEvent)
    evt.type = "response.output_item.done"
    if item_type == "function_call":
        evt.item = SimpleNamespace(
            type="function_call",
            model_dump=lambda: {"type": "function_call", "name": "test_fn"},
        )
    else:
        evt.item = SimpleNamespace(
            type="message",
            role=role,
            content=content,
        )
    return evt


def _make_stream(events):
    stream = MagicMock(spec=Stream)
    stream.__iter__.return_value = iter(events)
    return stream


def _make_function_call_done_event(name="camera", arguments="{}"):
    return ResponseOutputItemDoneEvent(
        type="response.output_item.done",
        output_index=1,
        sequence_number=2,
        item=ResponseFunctionToolCall(
            type="function_call",
            call_id="call_original",
            name=name,
            arguments=arguments,
        ),
    )


def _make_response(output, usage=None):
    resp = MagicMock(spec=Response)
    resp.usage = usage
    resp.output = output
    return resp


def _make_runtime_config(chat_size=2, instructions="You are a helpful AI assistant."):
    from openai.types.realtime import RealtimeSessionCreateRequest

    return RuntimeConfig(
        chat=Chat(chat_size),
        session=RealtimeSessionCreateRequest(type="realtime", instructions=instructions),
    )


def _make_request(text="Hi", chat_size=2):
    cfg = _make_runtime_config(chat_size=chat_size)
    cfg.chat.add_item(make_user_message(text))
    return GenerateResponseRequest(runtime_config=cfg)


def _chat_chunk(*, content=None, tool_calls=None, usage=None, refusal=None):
    choices = []
    if content is not None or tool_calls is not None or refusal is not None:
        choices = [
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=tool_calls, refusal=refusal),
                finish_reason=None,
            )
        ]
    return SimpleNamespace(choices=choices, usage=usage)


def _chat_tool_delta(index, *, tool_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=tool_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _make_handler(*, disable_thinking=False, stream=True, cancel_scope=None):
    handler = object.__new__(ResponsesApiModelHandler)
    handler.model_name = "test-model"
    handler.stream = stream
    handler.stream_batch_sentences = 1
    handler.gen_kwargs = {}
    handler.request_timeout_s = 20.0
    handler.request_timeout = 20.0
    handler.disable_thinking = disable_thinking
    handler._extra_body = {"chat_template_kwargs": {"enable_thinking": False}} if disable_thinking else None
    handler.user_role = "user"
    handler.cancel_scope = cancel_scope
    handler.speculative_turns = None
    handler.tools = None
    handler.tools_choice = None
    handler.enable_lang_prompt = False
    handler.compactor = None
    handler.audio_max_tokens = 80
    handler.audio_temperature = 0.0
    handler.audio_content_type = "input_audio"
    handler.audio_history_turns = 1
    return handler


def test_warmup_uses_request_scoped_sdk_retries():
    handler = _make_handler()
    handler.client = MagicMock()
    handler.client.with_options.return_value = handler.client

    handler.warmup()

    handler.client.with_options.assert_called_once_with(max_retries=WARMUP_MAX_RETRIES)
    handler.client.responses.create.assert_called_once()


def test_warmup_failure_propagates_and_prevents_readiness():
    handler = _make_handler()
    handler.client = MagicMock()
    handler.client.with_options.return_value = handler.client
    handler.client.responses.create.side_effect = RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        handler.warmup()


def test_process_streams_text_from_response_events():
    handler = _make_handler()

    streamed_events = [
        _make_text_delta_event("Hello. "),
        _make_text_delta_event("How are you?"),
        _make_output_item_done_event(content="Hello. How are you?"),
    ]

    handler.client = SimpleNamespace(
        responses=SimpleNamespace(
            create=lambda **kwargs: _make_stream(streamed_events),
        )
    )

    outputs = list(handler.process(_make_request("Hi")))

    assert len(outputs) == 3
    assert isinstance(outputs[0], LLMResponseChunk) and outputs[0].text == "Hello."
    assert isinstance(outputs[1], LLMResponseChunk) and outputs[1].text == "How are you?"
    assert isinstance(outputs[2], EndOfResponse)


def test_audio_stream_strips_markdown_asterisks_before_tts():
    handler = _make_handler(stream=True)
    handler.client = SimpleNamespace(
        responses=SimpleNamespace(
            create=lambda **kwargs: _make_stream(
                [
                    _make_text_delta_event("- **OpenAI** acquired **Hugging Face**."),
                    _make_output_item_done_event(content="- **OpenAI** acquired **Hugging Face**."),
                ]
            ),
        )
    )

    outputs = list(handler.process(_make_request("Hi")))
    spoken = "".join(o.text for o in outputs if isinstance(o, LLMResponseChunk))

    assert "*" not in spoken
    assert "OpenAI acquired Hugging Face." in spoken


def test_text_only_streams_raw_deltas_without_sentence_trimming():
    """Text-only streams (so a new speech turn can interrupt it) and forwards each
    delta verbatim — no sent_tokenize (newlines / markdown survive) and no
    remove_unspeechable (emoji / symbols survive)."""
    handler = _make_handler(stream=True)
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _make_stream(
            [
                _make_text_delta_event("# Title 🎉\n"),
                _make_text_delta_event("- one\n- two 😀\n"),
                _make_output_item_done_event(content="# Title 🎉\n- one\n- two 😀\n"),
            ]
        )

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=fake_create))

    cfg = _make_runtime_config()
    cfg.chat.add_item(make_user_message("Hi"))
    req = GenerateResponseRequest(
        runtime_config=cfg,
        response=RealtimeResponseCreateParams(output_modalities=["text"]),
    )

    outputs = list(handler.process(req))

    # Still streamed, not a single buffered chunk.
    assert captured["stream"] is True
    texts = [o.text for o in outputs if isinstance(o, LLMResponseChunk)]
    # Raw deltas: markdown layout AND emoji preserved (no trimming, no unspeechable filter).
    assert texts == ["# Title 🎉\n", "- one\n- two 😀\n"]
    assert "".join(texts) == "# Title 🎉\n- one\n- two 😀\n"


def test_process_flushes_tool_lead_in_before_function_call_with_sentence_batching():
    handler = _make_handler()
    handler.stream_batch_sentences = 3

    streamed_events = [
        _make_text_delta_event("Let me check with my camera."),
        _make_function_call_done_event(name="camera"),
    ]

    handler.client = SimpleNamespace(
        responses=SimpleNamespace(
            create=lambda **kwargs: _make_stream(streamed_events),
        )
    )

    outputs = list(handler.process(_make_request("What do you see?")))

    assert len(outputs) == 3
    assert isinstance(outputs[0], LLMResponseChunk)
    assert outputs[0].text == "Let me check with my camera."
    assert outputs[0].tools == []
    assert isinstance(outputs[1], LLMResponseChunk)
    assert outputs[1].text == ""
    assert [tool.name for tool in outputs[1].tools] == ["camera"]
    assert isinstance(outputs[2], EndOfResponse)


def test_process_preserves_streamed_text_after_function_call_order():
    handler = _make_handler()
    handler.stream_batch_sentences = 3

    streamed_events = [
        _make_text_delta_event("Let me check."),
        _make_function_call_done_event(name="camera"),
        _make_text_delta_event("This may take a second."),
    ]

    handler.client = SimpleNamespace(
        responses=SimpleNamespace(
            create=lambda **kwargs: _make_stream(streamed_events),
        )
    )

    outputs = list(handler.process(_make_request("What do you see?")))

    assert len(outputs) == 4
    assert isinstance(outputs[0], LLMResponseChunk)
    assert outputs[0].text == "Let me check."
    assert outputs[0].tools == []
    assert isinstance(outputs[1], LLMResponseChunk)
    assert outputs[1].text == ""
    assert [tool.name for tool in outputs[1].tools] == ["camera"]
    assert isinstance(outputs[2], LLMResponseChunk)
    assert outputs[2].text == "This may take a second."
    assert outputs[2].tools == []
    assert isinstance(outputs[3], EndOfResponse)


def test_process_preserves_nonstreaming_text_tool_text_order():
    handler = _make_handler(stream=False)

    api_response = _make_response(
        output=[
            ResponseOutputMessage(
                id="msg_1",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(type="output_text", text="Let me check.", annotations=[])],
            ),
            ResponseFunctionToolCall(
                type="function_call",
                call_id="call_original",
                name="camera",
                arguments="{}",
            ),
            ResponseOutputMessage(
                id="msg_2",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(type="output_text", text="This may take a second.", annotations=[])],
            ),
        ],
    )

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: api_response))

    outputs = list(handler.process(_make_request("What do you see?")))

    assert len(outputs) == 4
    assert isinstance(outputs[0], LLMResponseChunk)
    assert outputs[0].text == "Let me check."
    assert outputs[0].tools == []
    assert isinstance(outputs[1], LLMResponseChunk)
    assert outputs[1].text == ""
    assert [tool.name for tool in outputs[1].tools] == ["camera"]
    assert isinstance(outputs[2], LLMResponseChunk)
    assert outputs[2].text == "This may take a second."
    assert outputs[2].tools == []
    assert isinstance(outputs[3], EndOfResponse)


def test_process_handles_cancellation():
    scope = CancelScope()
    handler = _make_handler(cancel_scope=scope)

    def fake_create(**kwargs):
        scope.cancel()
        return _make_stream([_make_text_delta_event("Hello")])

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=fake_create))

    outputs = list(handler.process(_make_request("Hi")))

    assert len(outputs) == 1
    assert isinstance(outputs[0], EndOfResponse)


def test_responses_api_timing_logs_only_text_chunks():
    handler = object.__new__(ResponsesApiModelHandler)
    handler._times = [0.01]

    assert handler.timing_log_level == logging.INFO
    assert handler.should_log_timing(LLMResponseChunk(text="Hello."))
    assert not handler.should_log_timing(TokenUsage(input_tokens=1, output_tokens=1))
    assert not handler.should_log_timing(EndOfResponse())


def test_setup_reads_openrouter_api_key(monkeypatch):
    captured = {}
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-from-environment")

    class FakeOpenAI:
        def __init__(self, *, api_key, base_url):
            captured["api_key"] = api_key
            captured["base_url"] = base_url

    monkeypatch.setattr(base_openai_compatible_language_model, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(ResponsesApiModelHandler, "warmup", lambda self: None)

    handler = object.__new__(ResponsesApiModelHandler)
    handler.setup(api_key=None, compact_history=False)

    assert captured == {
        "api_key": "secret-from-environment",
        "base_url": "https://openrouter.ai/api/v1",
    }


def test_process_read_timeout_ends_response_cleanly():
    handler = _make_handler()

    def make_timeout_stream():
        stream = MagicMock(spec=Stream)
        stream.__iter__.side_effect = httpx.ReadTimeout("timed out")
        return stream

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: make_timeout_stream()))

    outputs = list(handler.process(_make_request("Hi")))

    assert len(outputs) == 2
    assert (
        isinstance(outputs[0], LLMResponseChunk)
        and outputs[0].text == "Wow I'm a bit slow today, could you repeat that?"
    )
    assert isinstance(outputs[1], EndOfResponse)


def test_cancel_during_silent_reasoning_does_not_speak():
    """Reasoning/keep-alive SSE events never reach _consume_streaming, so
    barge-in used to wait until the first spoken token. Check cancel on every
    raw event and drop the rest of the stream."""
    scope = CancelScope()
    handler = _make_handler(cancel_scope=scope)
    closed = {"n": 0}

    def events():
        yield SimpleNamespace(type="response.reasoning.delta")
        scope.cancel()
        yield SimpleNamespace(type="response.reasoning.delta")
        yield _make_text_delta_event("You should not hear this.")
        yield _make_output_item_done_event(content="You should not hear this.")

    def make_stream():
        stream = MagicMock(spec=Stream)
        stream.__iter__.return_value = events()
        stream.close.side_effect = lambda: closed.__setitem__("n", closed["n"] + 1)
        return stream

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: make_stream()))
    outputs = list(handler.process(_make_request("Tell me something interesting")))
    spoken = "".join(o.text for o in outputs if isinstance(o, LLMResponseChunk))
    assert "should not hear" not in spoken.lower()
    assert any(isinstance(o, EndOfResponse) for o in outputs)
    assert closed["n"] >= 1


def test_first_spoken_token_timeout_during_silent_reasoning(monkeypatch):
    """A stream that stays alive but never speaks must be cut off.

    OpenRouter keeps the HTTP read timeout alive with reasoning events while
    the speaker stays silent, so time-to-first-audio is bounded separately.
    The clock starts when the stream does, so the run below is 8.1s of live
    reasoning with no text.
    """
    handler = _make_handler()
    handler.first_spoken_token_s = 8.0
    now = {"t": 0.0}
    monkeypatch.setattr(
        "chatbot.LLM.base_openai_compatible_language_model.time.monotonic",
        lambda: now["t"],
    )

    def events():
        yield SimpleNamespace(type="response.reasoning.delta")  # arms the clock
        now["t"] = 8.1  # still reasoning, still silent
        yield SimpleNamespace(type="response.reasoning.delta")
        yield _make_text_delta_event("Too late.")

    handler.client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **kwargs: _make_stream(events())),
    )
    outputs = list(handler.process(_make_request("Tell me something interesting")))
    spoken = "".join(o.text for o in outputs if isinstance(o, LLMResponseChunk))
    assert spoken == "Wow I'm a bit slow today, could you repeat that?"
    assert isinstance(outputs[-1], EndOfResponse)


def test_a_slow_connection_does_not_spend_the_speaking_budget(monkeypatch):
    """Time to first byte must not count against time to first word.

    The budget used to start when the request was issued, so a slow connection
    spent it before the model had said anything: measured live, median time to
    first byte was 0.9s but the tail reached 10s, and one turn in seven was
    answered with the canned apology instead of the reply.
    """
    handler = _make_handler()
    handler.first_spoken_token_s = 8.0
    now = {"t": 0.0}
    monkeypatch.setattr(
        "chatbot.LLM.base_openai_compatible_language_model.time.monotonic",
        lambda: now["t"],
    )

    def slow_create(**kwargs):
        now["t"] = 9.5  # nine and a half seconds just to connect
        return _make_stream(iter([_make_text_delta_event("Here it is.")]))

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=slow_create))
    outputs = list(handler.process(_make_request("Tell me something interesting")))
    spoken = "".join(o.text for o in outputs if isinstance(o, LLMResponseChunk))
    assert spoken == "Here it is.", "a slow connection must still deliver the reply"


def test_generation_error_emits_failed_end_of_response():
    """A non-timeout failure (e.g. provider rejecting empty input) must still emit a
    terminating EndOfResponse carrying the error, so the response is closed instead
    of escaping process() and locking st.in_response forever."""
    handler = _make_handler()

    def boom(**kwargs):
        raise RuntimeError("input must not be empty")

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=boom))

    outputs = list(handler.process(_make_request("Hi")))

    eors = [o for o in outputs if isinstance(o, EndOfResponse)]
    assert len(eors) == 1
    assert eors[0].error is not None
    assert "input must not be empty" in eors[0].error
    # No partial output committed; the only thing emitted is the failed EndOfResponse.
    assert all(isinstance(o, EndOfResponse) for o in outputs)


def test_empty_context_fails_with_clear_message_without_calling_provider():
    """Out-of-band, text-only, empty `instructions`, input=[] -> empty context. We
    fail fast with a clear, instructions-aware message and never call the provider
    (which would reject the empty input), so the response terminates instead of
    hanging."""
    handler = _make_handler()
    called = False

    def fake_create(**kwargs):
        nonlocal called
        called = True
        return _make_response(output=[])

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=fake_create))

    cfg = _make_runtime_config(instructions="")  # empty instructions -> no system message
    req = GenerateResponseRequest(
        runtime_config=cfg,
        response=RealtimeResponseCreateParams(
            conversation="none",
            output_modalities=["text"],
            input=[],
        ),
    )

    outputs = list(handler.process(req))

    assert not called  # short-circuited before reaching the provider
    eors = [o for o in outputs if isinstance(o, EndOfResponse)]
    assert len(eors) == 1
    assert eors[0].error is not None
    assert "instructions" in eors[0].error
    assert "input" in eors[0].error


def test_voice_persona_is_sent_as_system_message_and_identity_is_last():
    handler = _make_handler()
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _make_stream(
            [
                _make_text_delta_event("Ok"),
                _make_output_item_done_event(content="Ok"),
            ]
        )

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=fake_create))
    cfg = _make_runtime_config(
        instructions="You are an AI conversation partner: perceptive, relaxed, warm, and quietly playful.",
    )
    cfg.chat.add_item(make_user_message("who are you"))
    list(handler.process(GenerateResponseRequest(runtime_config=cfg)))

    system_items = [item for item in captured["input"] if item.get("role") == "system"]
    assert len(system_items) == 1
    text = system_items[0]["content"][0]["text"]
    assert "perceptive, relaxed, warm, and quietly playful" in text
    assert "Tools run inside the spoken reply" in text
    assert "Current date and time:" in text
    assert "Do not take on a branded product name" in text
    assert text.find("perceptive, relaxed, warm") < text.rfind("Do not take on a branded product name")
    assert "who are you" in captured["input"][-1]["content"][0]["text"]


def test_speak_text_skips_the_api():
    handler = _make_handler()
    handler._request = MagicMock(side_effect=AssertionError("HTTP must not run"))
    handler.client = SimpleNamespace(responses=SimpleNamespace(create=handler._request))
    req = GenerateResponseRequest(runtime_config=_make_runtime_config(), speak_text="hello there")
    outputs = list(handler.process(req))
    assert len(outputs) == 2
    assert isinstance(outputs[0], LLMResponseChunk)
    assert outputs[0].text == "hello there"
    assert isinstance(outputs[1], EndOfResponse)
    handler._request.assert_not_called()


def test_disable_thinking_passes_extra_body():
    handler = _make_handler(disable_thinking=True)
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _make_stream(
            [
                _make_text_delta_event("Ok"),
                _make_output_item_done_event(content="Ok"),
            ]
        )

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=fake_create))

    list(handler.process(_make_request("Hi")))

    assert captured["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_no_disable_thinking_omits_extra_body():
    handler = _make_handler(disable_thinking=False)
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _make_stream(
            [
                _make_text_delta_event("Ok"),
                _make_output_item_done_event(content="Ok"),
            ]
        )

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=fake_create))

    list(handler.process(_make_request("Hi")))

    assert captured.get("extra_body") is None
    assert "reasoning" not in captured


def test_reasoning_effort_is_sent_as_responses_reasoning_param():
    """OpenRouter/OpenAI Responses take ``reasoning: {"effort": ...}``; the old
    ``reasoning_effort`` extra_body key was silently ignored, so the model
    deliberated for as long as it liked before the first spoken sentence."""
    handler = _make_handler()
    handler._reasoning_effort = "low"
    handler._extra_body = ResponsesApiModelHandler._build_extra_body(None, True, "low")
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _make_stream([_make_text_delta_event("Ok"), _make_output_item_done_event(content="Ok")])

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=fake_create))

    list(handler.process(_make_request("Hi")))

    assert captured["reasoning"] == {"effort": "low"}
    # The effort parameter supersedes the chat-template flag; do not send both.
    assert captured.get("extra_body") is None


def test_first_sentence_is_flushed_alone_then_batched():
    """Time to first audio is bounded by the first sentence, not by the batch
    size: the opening sentence goes out on its own and the rest are batched."""
    handler = _make_handler()
    handler.stream_batch_sentences = 3
    events = [
        _make_text_delta_event("Sure. "),
        _make_text_delta_event("Here is one. "),
        _make_text_delta_event("Here is two. "),
        _make_text_delta_event("Here is three. "),
        _make_text_delta_event("And the end."),
        _make_output_item_done_event(content="Sure. Here is one. Here is two. Here is three. And the end."),
    ]
    handler.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: _make_stream(events)))

    outputs = list(handler.process(_make_request("Hi")))
    texts = [o.text for o in outputs if isinstance(o, LLMResponseChunk)]

    assert texts == ["Sure.", "Here is one. Here is two. Here is three.", "And the end."]


def test_second_turn_flattens_assistant_history_for_responses():
    handler = _make_handler(stream=False)
    captured = {}
    cfg = _make_runtime_config(chat_size=2)

    first_response = _make_response(
        output=[
            ResponseOutputMessage(
                id="msg_1",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(type="output_text", text="Hello.", annotations=[])],
            )
        ],
    )
    second_response = _make_response(output=[])
    call_count = 0

    def fake_create(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return first_response
        captured.update(kwargs)
        return second_response

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=fake_create))

    cfg.chat.add_item(make_user_message("Hi"))
    list(handler.process(GenerateResponseRequest(runtime_config=cfg)))
    cfg.chat.add_item(make_user_message("Again"))
    list(handler.process(GenerateResponseRequest(runtime_config=cfg)))

    assistant_items = [item for item in captured["input"] if item.get("role") == "assistant"]
    assert len(assistant_items) == 1
    ai = assistant_items[0]
    assert ai["role"] == "assistant"
    assert ai["type"] == "message"
    assert ai["status"] == "completed"
    assert len(ai["content"]) == 1
    assert ai["content"][0]["type"] == "output_text"
    assert ai["content"][0]["text"] == "Hello."


# ── Out-of-band (conversation="none") responses ──────────────────────────


def _make_oob_request(input_items, *, conversation="none", chat_size=2, seed_default="Hi"):
    cfg = _make_runtime_config(chat_size=chat_size)
    if seed_default is not None:
        cfg.chat.add_item(make_user_message(seed_default))
    resp = RealtimeResponseCreateParams(conversation=conversation, input=input_items)
    return GenerateResponseRequest(runtime_config=cfg, response=resp), cfg


def _capture_create(handler, events):
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _make_stream(events)

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=fake_create))
    return captured


def test_out_of_band_emits_output_but_does_not_commit_to_default_conversation():
    handler = _make_handler()
    req, cfg = _make_oob_request([make_user_message("OOB question")])
    events = [_make_text_delta_event("OOB answer."), _make_output_item_done_event(content="OOB answer.")]
    _capture_create(handler, events)

    outputs = list(handler.process(req))

    # The response is still produced and streamed back to the client...
    assert any(isinstance(o, LLMResponseChunk) and o.text == "OOB answer." for o in outputs)
    # ...but the default conversation keeps only the seeded user turn — no assistant commit.
    assert len(cfg.chat.buffer) == 1
    assert not any(isinstance(i, RealtimeConversationItemAssistantMessage) for i in cfg.chat.buffer)


def test_out_of_band_input_builds_fresh_context():
    handler = _make_handler()
    req, _cfg = _make_oob_request([make_user_message("OOB question")])
    captured = _capture_create(handler, [_make_output_item_done_event(content="ok")])

    list(handler.process(req))

    serialized = str(captured["input"])
    assert "OOB question" in serialized
    assert "Hi" not in serialized  # default conversation history is excluded


def test_out_of_band_empty_input_clears_context():
    handler = _make_handler()
    req, _cfg = _make_oob_request([])
    captured = _capture_create(handler, [_make_output_item_done_event(content="ok")])

    list(handler.process(req))

    serialized = str(captured["input"])
    assert "Hi" not in serialized  # default conversation not used
    assert "helpful AI assistant" in serialized  # only the system prompt remains


def test_out_of_band_absent_input_reads_default_conversation():
    handler = _make_handler()
    req, cfg = _make_oob_request(None)
    captured = _capture_create(handler, [_make_output_item_done_event(content="ok")])

    list(handler.process(req))

    serialized = str(captured["input"])
    assert "Hi" in serialized  # default conversation used as read-only context
    # Still read-only: no assistant message committed back.
    assert len(cfg.chat.buffer) == 1


def test_out_of_band_invalid_input_emits_failed_end_of_response():
    handler = _make_handler()
    called = False

    def fake_create(**kwargs):
        nonlocal called
        called = True
        return _make_stream([])

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=fake_create))
    # function_call_output referencing an unknown call_id fails validation.
    orphan = RealtimeConversationItemFunctionCallOutput(
        type="function_call_output", call_id="call_missing", output="{}"
    )
    req, _cfg = _make_oob_request([orphan])

    outputs = list(handler.process(req))

    assert not called  # generation never started
    assert len(outputs) == 1
    assert isinstance(outputs[0], EndOfResponse)
    assert outputs[0].error is not None


def test_voice_prompt_research_follows_the_session_tools():
    """A Pi-driven session publishes spawn_thinking and no bash: the model must not be told to curl."""
    from openai.types.realtime import RealtimeFunctionTool, RealtimeSessionCreateRequest

    handler = _make_handler()
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _make_stream([_make_text_delta_event("Ok"), _make_output_item_done_event(content="Ok")])

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=fake_create))
    tools = [
        RealtimeFunctionTool(type="function", name=name, description=name, parameters={"type": "object"})
        for name in ("spawn_thinking", "stop_thinking")
    ]
    cfg = RuntimeConfig(
        chat=Chat(2),
        session=RealtimeSessionCreateRequest(type="realtime", instructions="You are a partner.", tools=tools),
    )
    cfg.chat.add_item(make_user_message("what's the weather"))
    list(handler.process(GenerateResponseRequest(runtime_config=cfg)))

    system_items = [item for item in captured["input"] if item.get("role") == "system"]
    text = system_items[0]["content"][0]["text"]
    assert "Let me look that up" in text
    assert "spawn_thinking" in text
    assert "ask_pi" not in text
    assert "curl" not in text
