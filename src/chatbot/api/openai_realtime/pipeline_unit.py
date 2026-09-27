import asyncio
from queue import Queue
from threading import Event
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from chatbot.api.openai_realtime.service import RealtimeService
from chatbot.api.openai_realtime.transports import SessionTransport
from chatbot.pipeline.cancel_scope import CancelScope
from chatbot.pipeline.ready import PipelineReady


class SessionState(BaseModel):
    """Per-client ephemeral state.

    Created when the WebSocket route claims a PipelineUnit and dropped when
    the client disconnects. Holding the
    transport reference, the service session id, and any send-loop scratch
    (pending_output_item) here ensures these fields share one lifecycle — a
    stale value can't outlive its session.

    `drained` is set by the send loop when SESSION_END travels through the handler
    chain back to the output queue; the release path awaits it before clearing
    `PipelineUnit.session`, so a new client cannot claim the unit until in-flight
    work from this session has fully reset.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    transport: Optional[SessionTransport] = None
    session_id: str = ""
    pending_output_item: Any = None
    drained: asyncio.Event = Field(default_factory=asyncio.Event)
    # Wall-clock time when the client disconnected (route handler released its
    # claim). `None` while the client is still active.
    released_at: Optional[float] = None
    # Wall-clock time when the drain wait gave up and quarantined the unit
    # (SESSION_END_QUARANTINE_TIMEOUT_S elapsed). The unit stays unclaimable —
    # its handlers may still emit this session's output — until SESSION_END
    # actually drains.
    quarantined_at: Optional[float] = None


class PipelineUnit(BaseModel):
    """One isolated realtime pipeline.

    Each unit owns its queues, events, RealtimeService, and the chain of handler
    instances (VAD, STT, transcription notifier, LM, LM output processor, TTS).
    The WebSocket route claims the unit (`session is None`) on `accept` and releases it on disconnect
    by setting `session` back to None.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    index: int
    service: RealtimeService
    cancel_scope: CancelScope
    should_listen: Event
    response_playing: Event
    input_queue: Queue
    output_queue: Queue
    text_output_queue: Queue
    text_prompt_queue: Queue
    handlers: list[Any]
    ready_gate: PipelineReady = Field(default_factory=PipelineReady)

    session: Optional[SessionState] = None
