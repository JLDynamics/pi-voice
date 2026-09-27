"""The LLM handler's in-response tool loop.

A response may take several model calls: text streams out, the model calls a
research tool, the handler runs it against the sidecar, appends the output and
asks the model again — all inside one response. No client-side tools remain,
so every tool call runs inside the response.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from openai import Stream
from openai.types.realtime import RealtimeSessionCreateRequest
from openai.types.realtime.conversation_item import (
    RealtimeConversationItemAssistantMessage,
    RealtimeConversationItemFunctionCall,
    RealtimeConversationItemFunctionCallOutput,
)
from openai.types.realtime.realtime_response_create_params import RealtimeResponseCreateParams
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseTextDeltaEvent,
)
from openai.types.responses.response_output_text import ResponseOutputText

from chatbot.api.openai_realtime.runtime_config import RuntimeConfig
from chatbot.LLM import server_tools
from chatbot.LLM.chat import Chat
from chatbot.LLM.chat_factories import make_user_message
from chatbot.LLM.responses_api_language_model import ResponsesApiModelHandler
from chatbot.pipeline.cancel_scope import CancelScope
from chatbot.pipeline.messages import EndOfResponse, GenerateResponseRequest, LLMResponseChunk, TokenUsage, ToolActivity

# ── stream building ──────────────────────────────────────────────────────────


def _delta(text: str):
    event = MagicMock(spec=ResponseTextDeltaEvent)
    event.type = "response.output_text.delta"
    event.delta = text
    return event


def _message_done(text: str, item_id: str = "msg"):
    return ResponseOutputItemDoneEvent(
        type="response.output_item.done",
        output_index=0,
        sequence_number=1,
        item=ResponseOutputMessage(
            id=item_id,
            type="message",
            role="assistant",
            status="completed",
            content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
        ),
    )


def _tool_done(name: str, call_id: str = "call_x", **arguments):
    return ResponseOutputItemDoneEvent(
        type="response.output_item.done",
        output_index=1,
        sequence_number=2,
        item=ResponseFunctionToolCall(
            type="function_call", call_id=call_id, name=name, arguments=json.dumps(arguments)
        ),
    )


def _completed(input_tokens: int = 10, output_tokens: int = 5):
    event = MagicMock(spec=ResponseCompletedEvent)
    event.type = "response.completed"
    event.response = SimpleNamespace(usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens))
    return event


def _stream(events):
    stream = MagicMock(spec=Stream)
    stream.__iter__.return_value = iter(events)
    return stream


def _speak(text: str, *tools, item_id: str = "msg"):
    """A model round: spoken text, then the given tool calls."""
    return [_delta(text), _message_done(text, item_id), *tools]


# ── handler / executor fakes ─────────────────────────────────────────────────


class FakeExecutor:
    """Stands in for ServerToolExecutor with canned outputs and a call log."""

    def __init__(self, outputs: dict[str, str] | None = None, *, cancel_on_run: CancelScope | None = None):
        self.outputs = outputs or {}
        self.calls: list[tuple[str, dict]] = []
        self.cancel_on_run = cancel_on_run
        self.memory = "- Avery likes tea"

    # Names this double claims. These tests are about the loop — rounds, the
    # time budget, cancellation — not about which tools exist, so the double
    # keeps claiming the research names the scenarios use even though the real
    # executor now runs only `bash`.
    HANDLED = frozenset(
        server_tools.SERVER_TOOL_NAMES | {"web_search", "read_page", "web_fetch", "search_chat_history"}
    )

    @staticmethod
    def handles(name: str) -> bool:
        return name in FakeExecutor.HANDLED

    def memory_profile(self) -> str:
        return self.memory

    def run_many(self, calls, *, is_cancelled=None):
        results = []
        for call in calls:
            self.calls.append((call.name, json.loads(call.arguments or "{}")))
            if self.cancel_on_run is not None:
                self.cancel_on_run.cancel()
            if is_cancelled is not None and is_cancelled():
                results.append(None)
                continue
            results.append(self.outputs.get(call.name, f"{call.name} output"))
        return results


def _handler(rounds, *, executor=None, cancel_scope=None, stream=True):
    handler = object.__new__(ResponsesApiModelHandler)
    handler.model_name = "test-model"
    handler.stream = stream
    handler.stream_batch_sentences = 1
    handler.gen_kwargs = {}
    handler.request_timeout_s = 20.0
    handler.request_timeout = 20.0
    handler.user_role = "user"
    handler.cancel_scope = cancel_scope
    handler.speculative_turns = None
    handler.enable_lang_prompt = False
    handler.compactor = None
    handler.server_tools = executor
    requests: list[dict] = []
    rounds = list(rounds)

    def create(**kwargs):
        requests.append(kwargs)
        if not rounds:
            raise AssertionError("the model was asked for more rounds than the test scripted")
        return _stream(rounds.pop(0))

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return handler, requests


def _request(text="What's the weather in Paris?", *, tools=True, response=None):
    tool_defs = (
        [
            {"type": "function", "name": "web_search", "parameters": {"type": "object", "properties": {}}},
        ]
        if tools
        else None
    )
    cfg = RuntimeConfig(
        chat=Chat(5),
        session=RealtimeSessionCreateRequest(type="realtime", instructions="Be brief.", tools=tool_defs),
    )
    cfg.chat.add_item(make_user_message(text))
    return GenerateResponseRequest(runtime_config=cfg, response=response)


def _types(outputs):
    return [
        (type(o).__name__, getattr(o, "status", None) or getattr(o, "text", None) or "")
        if not isinstance(o, LLMResponseChunk)
        else ("chunk", o.text, [t.name for t in o.tools])
        for o in outputs
    ]


def _input_types(request):
    return [item["type"] for item in request["input"]]


# ── tests ────────────────────────────────────────────────────────────────────


def test_bash_research_runs_inside_the_response_like_pi():
    """The voice model writes curl; the server runs it and continues the same reply."""
    executor = FakeExecutor({"bash": "Python 3.13.7 is the latest stable release."})
    handler, requests = _handler(
        [
            [
                *_speak("Let me check that.", _tool_done("bash", command="curl -sL https://www.python.org/downloads/")),
                _completed(10, 5),
            ],
            [*_speak("The latest stable Python is 3.13.7.", item_id="msg2"), _completed(20, 7)],
        ],
        executor=executor,
    )
    request = _request()

    outputs = list(handler.process(request))

    assert executor.calls == [("bash", {"command": "curl -sL https://www.python.org/downloads/"})]
    assert len(requests) == 2
    chunks = [o for o in outputs if isinstance(o, LLMResponseChunk)]
    assert [c.text for c in chunks] == ["Let me check that.", "The latest stable Python is 3.13.7."]
    assert all(c.tools == [] for c in chunks)
    activity = [o for o in outputs if isinstance(o, ToolActivity)]
    assert [(a.status, a.name) for a in activity] == [("started", "bash"), ("finished", "bash")]


def test_search_runs_inside_the_response_and_the_model_continues():
    executor = FakeExecutor({"web_search": "[1] Paris weather\n18°C and sunny\nURL: https://w.example"})
    handler, requests = _handler(
        [
            [*_speak("Let me check.", _tool_done("web_search", query="Paris weather")), _completed(10, 5)],
            [*_speak("It's eighteen degrees and sunny in Paris.", item_id="msg2"), _completed(20, 7)],
        ],
        executor=executor,
    )
    request = _request()

    outputs = list(handler.process(request))

    assert executor.calls == [("web_search", {"query": "Paris weather"})]
    assert len(requests) == 2, "the follow-up must be a second model call in the same response"

    chunks = [o for o in outputs if isinstance(o, LLMResponseChunk)]
    assert [c.text for c in chunks] == ["Let me check.", "It's eighteen degrees and sunny in Paris."]
    assert all(c.tools == [] for c in chunks), "server-side calls are never forwarded to the client"

    activity = [o for o in outputs if isinstance(o, ToolActivity)]
    assert [(a.status, a.name) for a in activity] == [("started", "web_search"), ("finished", "web_search")]
    assert activity[0].call_id == activity[1].call_id
    assert activity[1].output.startswith("[1] Paris weather")
    assert activity[0].item_id != activity[1].item_id

    # Order on the wire: speech, tool started, tool finished, speech, usage, end.
    assert [type(o).__name__ for o in outputs] == [
        "LLMResponseChunk",
        "ToolActivity",
        "ToolActivity",
        "LLMResponseChunk",
        "TokenUsage",
        "EndOfResponse",
    ]
    assert isinstance(outputs[-1], EndOfResponse) and outputs[-1].error is None

    # The second call saw the call and its output.
    second = requests[1]["input"]
    assert _input_types(requests[1]) == ["message", "message", "message", "function_call", "function_call_output"]
    assert second[-2]["name"] == "web_search"
    assert second[-1]["output"].startswith("[1] Paris weather")
    assert second[-1]["call_id"] == second[-2]["call_id"]

    # Usage is summed over both model calls.
    usage = next(o for o in outputs if isinstance(o, TokenUsage))
    assert (usage.input_tokens, usage.output_tokens) == (30, 12)

    # History holds the whole exchange in order.
    history = request.runtime_config.chat.buffer
    assert [type(item).__name__ for item in history] == [
        "RealtimeConversationItemUserMessage",
        "RealtimeConversationItemAssistantMessage",
        "RealtimeConversationItemFunctionCall",
        "RealtimeConversationItemFunctionCallOutput",
        "RealtimeConversationItemAssistantMessage",
    ]
    assert isinstance(history[2], RealtimeConversationItemFunctionCall) and history[2].status == "completed"
    assert isinstance(history[3], RealtimeConversationItemFunctionCallOutput)
    assert isinstance(history[4], RealtimeConversationItemAssistantMessage)
    assert history[4].content[0].text == "It's eighteen degrees and sunny in Paris."


def test_chained_tools_run_round_after_round():
    executor = FakeExecutor(
        {"web_search": "[1] Story\nsnippet\nURL: https://news.example/a", "read_page": "status: read\n\nFull story."}
    )
    handler, requests = _handler(
        [
            [*_speak("Searching.", _tool_done("web_search", query="news")), _completed()],
            [
                *_speak("Found it, reading.", _tool_done("read_page", url="https://news.example/a"), item_id="m2"),
                _completed(),
            ],
            [*_speak("Here is the story.", item_id="m3"), _completed()],
        ],
        executor=executor,
    )

    outputs = list(handler.process(_request()))

    assert [name for name, _ in executor.calls] == ["web_search", "read_page"]
    assert len(requests) == 3
    assert _input_types(requests[2]) == [
        "message",  # system
        "message",  # user
        "message",  # "Searching."
        "function_call",
        "function_call_output",
        "message",  # "Found it, reading."
        "function_call",
        "function_call_output",
    ]
    spoken = [o.text for o in outputs if isinstance(o, LLMResponseChunk)]
    assert spoken == ["Searching.", "Found it, reading.", "Here is the story."]


def test_parallel_calls_in_one_round_all_run_before_the_next_call():
    executor = FakeExecutor()
    two_calls = [
        ResponseOutputItemDoneEvent(
            type="response.output_item.done",
            output_index=i,
            sequence_number=i,
            item=ResponseFunctionToolCall(
                type="function_call", call_id=f"c{i}", name="web_search", arguments=json.dumps({"query": q})
            ),
        )
        for i, q in enumerate(["a", "b"], start=1)
    ]
    handler, requests = _handler(
        [[*_speak("Two searches.", *two_calls), _completed()], [*_speak("Done.", item_id="m2"), _completed()]],
        executor=executor,
    )

    outputs = list(handler.process(_request()))

    assert [args["query"] for _, args in executor.calls] == ["a", "b"]
    finished = [o for o in outputs if isinstance(o, ToolActivity) and o.status == "finished"]
    assert len(finished) == 2
    assert len(requests) == 2
    assert _input_types(requests[1])[-4:] == [
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
    ]


def test_unhandled_tool_ends_the_response_for_the_client_to_run():
    executor = FakeExecutor()
    handler, requests = _handler(
        [[*_speak("Let me look.", _tool_done("page_reader")), _completed()]],
        executor=executor,
    )

    outputs = list(handler.process(_request()))

    assert executor.calls == []
    assert len(requests) == 1
    tool_chunk = next(o for o in outputs if isinstance(o, LLMResponseChunk) and o.tools)
    assert [t.name for t in tool_chunk.tools] == ["page_reader"]
    assert not any(isinstance(o, ToolActivity) for o in outputs)
    assert isinstance(outputs[-1], EndOfResponse)


def test_search_then_unhandled_tool_in_one_round_still_stops_for_the_client():
    """A mixed round must run research here, then forward the unknown tool."""
    executor = FakeExecutor({"web_search": "found it"})
    handler, requests = _handler(
        [
            [
                *_speak(
                    "Checking.",
                    _tool_done("web_search", query="x"),
                    _tool_done("page_reader", call_id="call_shot"),
                ),
                _completed(),
            ],
            [*_speak("should not run", item_id="m2"), _completed()],
        ],
        executor=executor,
    )

    outputs = list(handler.process(_request()))

    assert executor.calls == [("web_search", {"query": "x"})]
    assert len(requests) == 1
    tool_chunk = next(o for o in outputs if isinstance(o, LLMResponseChunk) and o.tools)
    assert [t.name for t in tool_chunk.tools] == ["page_reader"]
    assert [o.name for o in outputs if isinstance(o, ToolActivity) and o.status == "finished"] == ["web_search"]
    assert not any(isinstance(o, LLMResponseChunk) and o.text == "should not run" for o in outputs)


def test_without_a_sidecar_every_tool_goes_to_the_client():
    handler, requests = _handler(
        [[*_speak("Let me check.", _tool_done("web_search", query="x")), _completed()]], executor=None
    )

    outputs = list(handler.process(_request()))

    assert len(requests) == 1
    tool_chunk = next(o for o in outputs if isinstance(o, LLMResponseChunk) and o.tools)
    assert [t.name for t in tool_chunk.tools] == ["web_search"]


def test_barge_in_during_a_tool_drops_its_output_and_makes_no_second_call():
    scope = CancelScope()
    executor = FakeExecutor(cancel_on_run=scope)
    handler, requests = _handler(
        [[*_speak("Let me check.", _tool_done("web_search", query="x")), _completed()]],
        executor=executor,
        cancel_scope=scope,
    )
    request = _request()

    outputs = list(handler.process(request))

    assert len(requests) == 1
    assert [o.status for o in outputs if isinstance(o, ToolActivity)] == ["started"]
    assert isinstance(outputs[-1], EndOfResponse)
    assert not any(isinstance(o, TokenUsage) for o in outputs), "an interrupted response is not committed"
    # The call is recorded but unanswered; it stays pending and is never serialised.
    history_types = [type(item).__name__ for item in request.runtime_config.chat.buffer]
    assert "RealtimeConversationItemFunctionCallOutput" not in history_types


def test_round_cap_forces_a_final_answer_without_tools(monkeypatch):
    monkeypatch.setattr("chatbot.LLM.base_openai_compatible_language_model.MAX_TOOL_ROUNDS", 2)
    executor = FakeExecutor()
    handler, requests = _handler(
        [
            [*_speak("One.", _tool_done("web_search", query="1")), _completed()],
            [*_speak("Two.", _tool_done("web_search", query="2"), item_id="m2"), _completed()],
            [*_speak("Final answer.", item_id="m3"), _completed()],
        ],
        executor=executor,
    )

    outputs = list(handler.process(_request()))

    assert len(requests) == 3
    assert requests[0].get("tool_choice") is None
    assert requests[1].get("tool_choice") is None
    assert requests[2]["tool_choice"] == "none"
    assert [o.text for o in outputs if isinstance(o, LLMResponseChunk)] == ["One.", "Two.", "Final answer."]


def test_time_budget_forces_a_final_answer_after_the_first_round(monkeypatch):
    """A spoken answer cannot research for a minute: once the budget is spent
    the next model call runs with tools disabled, however many rounds remain."""
    monkeypatch.setattr("chatbot.LLM.base_openai_compatible_language_model.TOOL_TIME_BUDGET_S", 0.0)
    executor = FakeExecutor()
    handler, requests = _handler(
        [
            [*_speak("Let me check.", _tool_done("web_search", query="1")), _completed()],
            [*_speak("Here is what I found.", item_id="m2"), _completed()],
        ],
        executor=executor,
    )

    outputs = list(handler.process(_request()))

    assert len(requests) == 2
    assert requests[0].get("tool_choice") is None, "the first call is never constrained by the budget"
    assert requests[1]["tool_choice"] == "none"
    assert [o.text for o in outputs if isinstance(o, LLMResponseChunk)] == ["Let me check.", "Here is what I found."]


def test_out_of_band_tool_rounds_never_touch_the_default_conversation():
    executor = FakeExecutor()
    handler, requests = _handler(
        [
            [*_speak("Checking.", _tool_done("web_search", query="x")), _completed()],
            [*_speak("Answer.", item_id="m2"), _completed()],
        ],
        executor=executor,
    )
    request = _request(
        response=RealtimeResponseCreateParams(
            conversation="none",
            input=[{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "side question"}]}],
        )
    )
    before = list(request.runtime_config.chat.buffer)

    outputs = list(handler.process(request))

    assert len(requests) == 2
    assert _input_types(requests[1])[-2:] == ["function_call", "function_call_output"]
    assert [o.text for o in outputs if isinstance(o, LLMResponseChunk)] == ["Checking.", "Answer."]
    assert request.runtime_config.chat.buffer == before


def test_no_personal_memory_is_injected_now_the_sidecar_is_gone():
    """Memory used to be read from the sidecar and folded into the prompt.

    Nothing serves it any more — Pi owns memory — so the system prompt must
    carry no profile, and an executor offering one must not reintroduce it.
    """
    executor = FakeExecutor()
    executor.memory = "- Avery works in retail"
    handler, requests = _handler([[*_speak("Hi."), _completed()]], executor=executor)

    list(handler.process(_request("Hello")))

    system = next(item for item in requests[0]["input"] if item.get("role") == "system")
    assert "Avery works in retail" not in system["content"][0]["text"]


def test_nonstreaming_rounds_also_loop():
    executor = FakeExecutor({"web_search": "result"})
    handler, requests = _handler([], executor=executor, stream=False)
    responses = [
        SimpleNamespace(
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            output=[
                ResponseOutputMessage(
                    id="m1",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[ResponseOutputText(type="output_text", text="Checking.", annotations=[])],
                ),
                ResponseFunctionToolCall(
                    type="function_call", call_id="c", name="web_search", arguments='{"query":"q"}'
                ),
            ],
        ),
        SimpleNamespace(
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            output=[
                ResponseOutputMessage(
                    id="m2",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[ResponseOutputText(type="output_text", text="Answer.", annotations=[])],
                )
            ],
        ),
    ]

    def create(**kwargs):
        requests.append(kwargs)
        return responses.pop(0)

    handler.client = SimpleNamespace(responses=SimpleNamespace(create=create))

    outputs = list(handler.process(_request()))

    assert len(requests) == 2
    assert [o.text for o in outputs if isinstance(o, LLMResponseChunk)] == ["Checking.", "Answer."]
    assert [o.status for o in outputs if isinstance(o, ToolActivity)] == ["started", "finished"]
