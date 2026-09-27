from __future__ import annotations

from queue import Empty, Queue
from typing import Any, Callable, TypeVar

from chatbot.pipeline.control import SESSION_END, is_control_message
from chatbot.pipeline.events import (
    SpeechStoppedEvent,
    TokenUsageEvent,
    TranscriptionCompletedEvent,
)
from chatbot.pipeline.messages import AUDIO_RESPONSE_DONE, AudioOutput

QItem = TypeVar("QItem")


def audio_payload(item: Any) -> Any:
    return item.audio if isinstance(item, AudioOutput) else item


def is_audio_done(item: Any) -> bool:
    payload = audio_payload(item)
    return isinstance(payload, bytes) and payload == AUDIO_RESPONSE_DONE


def keep_audio_sentinel(item: Any) -> bool:
    # SESSION_END must survive barge-in flushes of output_queue: dropping it
    # would leave the release path waiting forever for the drain signal.
    return is_audio_done(item) or is_control_message(item, SESSION_END.kind)


def keep_session_end(item: Any) -> bool:
    # SESSION_END must survive barge-in flushes of the LLM input queue so the
    # release path still receives its drain signal.
    return is_control_message(item, SESSION_END.kind)


def keep_user_text_event(item: Any) -> bool:
    return isinstance(
        item,
        (
            SpeechStoppedEvent,
            TranscriptionCompletedEvent,
            TokenUsageEvent,
        ),
    )


def flush_queue(q: Queue[QItem], *, preserve: Callable[[QItem], bool] | None = None) -> None:
    """Drain a queue, optionally preserving items matching *preserve*.

    Preserved items are re-inserted at the **front** of the queue
    (atomically under the queue's mutex) so they are processed before
    anything a pipeline thread may have enqueued during the drain.
    """
    preserved: list[QItem] = []
    while True:
        try:
            item = q.get_nowait()
            if preserve and preserve(item):
                preserved.append(item)
        except Empty:
            break
    if preserved:
        with q.mutex:
            for item in reversed(preserved):
                q.queue.appendleft(item)
            q.not_empty.notify(len(preserved))
