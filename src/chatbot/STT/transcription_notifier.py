from __future__ import annotations

import logging
from queue import Queue
from threading import Event
from typing import Iterator

from chatbot.baseHandler import BaseHandler
from chatbot.pipeline.events import TranscriptionCompletedEvent
from chatbot.pipeline.handler_types import LLMIn, STTOut
from chatbot.pipeline.messages import Transcription
from chatbot.pipeline.queue_types import TextEventItem

logger = logging.getLogger(__name__)


class TranscriptionNotifier(BaseHandler[STTOut, LLMIn]):
    """Sits between STT and LLM.

    It emits protocol-neutral transcription events on ``text_output_queue``.
    ``RealtimeService`` consumes those events, updates conversation state, and
    creates LLM requests.
    """

    def setup(
        self,
        text_output_queue: Queue[TextEventItem] | None = None,
        should_listen: Event | None = None,
    ) -> None:
        self.text_output_queue = text_output_queue
        self.should_listen = should_listen

    def process(self, transcription: STTOut) -> Iterator[LLMIn]:
        if isinstance(transcription, Transcription):
            text = transcription.text
            language_code = transcription.language_code
            turn_id = transcription.turn_id
            turn_revision = transcription.turn_revision
            speech_stopped_at_s = transcription.speech_stopped_at_s
            stt_error = transcription.error
            active_speech_ms = transcription.active_speech_ms
        else:
            text = transcription
            language_code = None
            turn_id = None
            turn_revision = None
            speech_stopped_at_s = None
            stt_error = None
            active_speech_ms = None

        transcript = str(text)
        # Always close the client-visible transcription item. Empty final STT
        # results should not trigger the LLM, but clients may already have
        # received partial deltas and still need a completed event.
        if self.text_output_queue is not None:
            self.text_output_queue.put(
                TranscriptionCompletedEvent(
                    transcript=transcript,
                    language_code=language_code,
                    turn_id=turn_id,
                    turn_revision=turn_revision,
                    speech_stopped_at_s=speech_stopped_at_s,
                    error=stt_error,
                    active_speech_ms=active_speech_ms,
                )
            )

        if not transcript:
            logger.debug("Transcription completed with empty transcript")
            if self.should_listen is not None:
                # Listening is already enabled (full-duplex). Keep the Event
                # set so any leftover half-duplex caller still observes True.
                self.should_listen.set()
            return

        if language_code:
            logger.info("Transcription completed (language=%s): %s", language_code, transcript)
        else:
            logger.info("Transcription completed: %s", transcript)

        yield from ()
