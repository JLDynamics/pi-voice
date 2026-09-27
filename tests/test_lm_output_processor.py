from queue import Queue

from openai.types.realtime.realtime_response_create_params import RealtimeResponseCreateParams

from chatbot.LLM.lm_output_processor import LMOutputProcessor
from chatbot.pipeline.events import ToolActivityEvent
from chatbot.pipeline.messages import EndOfResponse, LLMResponseChunk, ToolActivity, TTSInput
from chatbot.pipeline.speculative_turns import SpeculativeTurnTracker


def _processor(tracker: SpeculativeTurnTracker) -> LMOutputProcessor:
    processor = LMOutputProcessor.__new__(LMOutputProcessor)
    processor.setup(text_output_queue=Queue(), speculative_turns=tracker)
    return processor


def test_stale_end_of_response_is_not_forwarded_to_tts():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 1)
    processor = _processor(tracker)

    outputs = list(processor.process(EndOfResponse(turn_id="turn_1", turn_revision=0)))

    assert outputs == []


def test_latest_end_of_response_is_forwarded_to_tts():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 1)
    processor = _processor(tracker)

    outputs = list(processor.process(EndOfResponse(turn_id="turn_1", turn_revision=1)))

    assert len(outputs) == 1
    assert outputs[0].turn_id == "turn_1"
    assert outputs[0].turn_revision == 1


def test_cancel_generation_is_forwarded_to_tts():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    processor = _processor(tracker)

    outputs = list(
        processor.process(
            LLMResponseChunk(
                text="hello",
                turn_id="turn_1",
                turn_revision=0,
                cancel_generation=7,
            )
        )
    )

    assert len(outputs) == 1
    assert outputs[0].cancel_generation == 7


def test_text_only_chunk_is_not_forwarded_to_tts():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    processor = _processor(tracker)

    outputs = list(
        processor.process(
            LLMResponseChunk(
                text="hello",
                turn_id="turn_1",
                turn_revision=0,
                response=RealtimeResponseCreateParams(output_modalities=["text"]),
            )
        )
    )

    assert outputs == []
    # The assistant text still reaches clients even when TTS is skipped.
    event = processor.text_output_queue.get_nowait()
    assert event.text == "hello"


def test_audio_chunk_is_forwarded_to_tts():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    processor = _processor(tracker)

    outputs = list(
        processor.process(
            LLMResponseChunk(
                text="hello",
                turn_id="turn_1",
                turn_revision=0,
                response=RealtimeResponseCreateParams(output_modalities=["audio"]),
            )
        )
    )

    assert len(outputs) == 1
    assert isinstance(outputs[0], TTSInput)
    assert outputs[0].text == "hello"


def test_empty_modalities_is_forwarded_to_tts():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    processor = _processor(tracker)

    outputs = list(
        processor.process(
            LLMResponseChunk(
                text="hello",
                turn_id="turn_1",
                turn_revision=0,
                response=RealtimeResponseCreateParams(output_modalities=[]),
            )
        )
    )

    assert len(outputs) == 1
    assert isinstance(outputs[0], TTSInput)


def test_pending_reopen_does_not_block_assistant_chunk():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    tracker.begin_reopen_candidate("turn_1", 0)
    processor = _processor(tracker)

    outputs = list(
        processor.process(
            LLMResponseChunk(
                text="hello",
                turn_id="turn_1",
                turn_revision=0,
            )
        )
    )

    assert len(outputs) == 1
    assert outputs[0].text == "hello"
    event = processor.text_output_queue.get_nowait()
    assert event.text == "hello"


def test_reopen_grace_does_not_block_assistant_chunk():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    tracker.start_reopen_grace("turn_1", 0, grace_s=2.0)
    processor = _processor(tracker)

    outputs = list(
        processor.process(
            LLMResponseChunk(
                text="hello",
                turn_id="turn_1",
                turn_revision=0,
            )
        )
    )

    assert len(outputs) == 1
    assert outputs[0].text == "hello"
    event = processor.text_output_queue.get_nowait()
    assert event.text == "hello"


def test_confirmed_reopen_drops_stale_assistant_chunk():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    candidate_revision = tracker.begin_reopen_candidate("turn_1", 0)
    processor = _processor(tracker)
    assert tracker.confirm_reopen_candidate("turn_1", 0, candidate_revision)

    outputs = list(
        processor.process(
            LLMResponseChunk(
                text="hello",
                turn_id="turn_1",
                turn_revision=0,
            )
        )
    )

    assert outputs == []
    assert processor.text_output_queue.empty()


def test_tool_activity_is_a_side_channel_event_and_never_reaches_tts():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 1)
    processor = _processor(tracker)

    outputs = list(
        processor.process(
            ToolActivity(
                status="finished",
                call_id="call_1",
                item_id="fco_1",
                name="web_search",
                arguments='{"query": "x"}',
                output="[1] result",
                turn_id="turn_1",
                turn_revision=1,
                cancel_generation=3,
            )
        )
    )

    assert outputs == []
    event = processor.text_output_queue.get_nowait()
    assert isinstance(event, ToolActivityEvent)
    assert (event.status, event.name, event.call_id, event.output) == ("finished", "web_search", "call_1", "[1] result")
    assert event.cancel_generation == 3


def test_stale_tool_activity_is_dropped():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 1)
    processor = _processor(tracker)

    outputs = list(
        processor.process(
            ToolActivity(
                status="started", call_id="c", item_id="fc", name="web_search", turn_id="turn_1", turn_revision=0
            )
        )
    )

    assert outputs == []
    assert processor.text_output_queue.empty()
