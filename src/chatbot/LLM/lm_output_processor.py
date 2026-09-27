"""
LLM Output Processor

Intercepts LLM output to:
1. Extract tool calls and send them via text_output_queue
2. Forward clean text to TTS pipeline
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from queue import Queue

from chatbot.baseHandler import BaseHandler
from chatbot.pipeline.events import AssistantTextEvent, ResponseFailedEvent, TokenUsageEvent, ToolActivityEvent
from chatbot.pipeline.handler_types import LLMOut, TTSIn
from chatbot.pipeline.messages import EndOfResponse, LLMResponseChunk, TokenUsage, ToolActivity, TTSInput
from chatbot.pipeline.queue_types import TextEventItem
from chatbot.pipeline.speculative_turns import SpeculativeTurnTracker
from chatbot.utils.utils import response_wants_audio

logger = logging.getLogger(__name__)


class LMOutputProcessor(BaseHandler[LLMOut, TTSIn]):
    """
    Processes LLM output to extract tool calls and forward clean text to TTS.

    Input: :class:`LLMResponseChunk`, :class:`TokenUsage`, or :class:`EndOfResponse` from LLM
    Output: :class:`TTSInput` or :class:`EndOfResponse` to TTS
    Side effect: Sends :class:`AssistantTextEvent` / :class:`TokenUsageEvent` to text_output_queue
    """

    def setup(
        self,
        text_output_queue: Queue[TextEventItem] | None = None,
        speculative_turns: SpeculativeTurnTracker | None = None,
    ) -> None:
        """
        Initialize the processor.

        Args:
            text_output_queue: Queue to send text messages and tool calls
        """
        self.text_output_queue = text_output_queue
        self.speculative_turns = speculative_turns

    def _turn_output_allowed(self, turn_id: str | None, turn_revision: int | None) -> bool:
        if self.speculative_turns is None:
            return True
        # Don't wait out the reopen grace before forwarding to TTS. If the user
        # continues speaking, barge-in cancels this speculative audio.
        return self.speculative_turns.is_latest(turn_id, turn_revision)

    def process(self, lm_output: LLMOut) -> Iterator[TTSIn]:
        """
        Process LLM output: send text/tools to WebSocket, forward clean text to TTS.

        Yields:
            :class:`TTSInput` or :class:`EndOfResponse` for TTS
        """
        if isinstance(lm_output, TokenUsage):
            if not self._turn_output_allowed(
                lm_output.turn_id,
                lm_output.turn_revision,
            ):
                logger.debug(
                    "Dropping stale token usage for turn=%s rev=%s", lm_output.turn_id, lm_output.turn_revision
                )
                return
            if self.text_output_queue is not None:
                self.text_output_queue.put(
                    TokenUsageEvent(
                        input_tokens=lm_output.input_tokens or 0,
                        output_tokens=lm_output.output_tokens or 0,
                        turn_id=lm_output.turn_id,
                        turn_revision=lm_output.turn_revision,
                    )
                )
            return

        if isinstance(lm_output, ToolActivity):
            # Server-side tool progress: side-channel only, never spoken.
            if not self._turn_output_allowed(lm_output.turn_id, lm_output.turn_revision):
                logger.debug(
                    "Dropping stale tool activity for turn=%s rev=%s", lm_output.turn_id, lm_output.turn_revision
                )
                return
            if self.text_output_queue is not None:
                self.text_output_queue.put(
                    ToolActivityEvent(
                        status=lm_output.status,
                        call_id=lm_output.call_id,
                        item_id=lm_output.item_id,
                        name=lm_output.name,
                        arguments=lm_output.arguments,
                        output=lm_output.output,
                        turn_id=lm_output.turn_id,
                        turn_revision=lm_output.turn_revision,
                        cancel_generation=lm_output.cancel_generation,
                    )
                )
            return

        if isinstance(lm_output, EndOfResponse):
            if not self._turn_output_allowed(
                lm_output.turn_id,
                lm_output.turn_revision,
            ):
                logger.debug(
                    "Dropping stale end-of-response for turn=%s rev=%s",
                    lm_output.turn_id,
                    lm_output.turn_revision,
                )
                return
            # A failed generation (e.g. invalid out-of-band input) closes the response as
            # "failed" via the text side-channel, then falls through to emit the normal
            # EndOfResponse so the audio path still re-enables listening / releases the slot.
            if lm_output.error and self.text_output_queue is not None:
                self.text_output_queue.put(
                    ResponseFailedEvent(
                        message=lm_output.error,
                        turn_id=lm_output.turn_id,
                        turn_revision=lm_output.turn_revision,
                    )
                )
            yield EndOfResponse(
                turn_id=lm_output.turn_id,
                turn_revision=lm_output.turn_revision,
                cancel_generation=lm_output.cancel_generation,
            )
            return

        if not isinstance(lm_output, LLMResponseChunk):
            logger.warning("LMOutputProcessor received unexpected type: %s", type(lm_output))
            return

        if not self._turn_output_allowed(
            lm_output.turn_id,
            lm_output.turn_revision,
        ):
            logger.debug("Dropping stale LLM chunk for turn=%s rev=%s", lm_output.turn_id, lm_output.turn_revision)
            return

        logger.debug(f"LM processor: text='{lm_output.text}', tools={lm_output.tools}")

        if self.text_output_queue is not None:
            event = AssistantTextEvent(
                text=lm_output.text,
                turn_id=lm_output.turn_id,
                turn_revision=lm_output.turn_revision,
                cancel_generation=lm_output.cancel_generation,
            )
            if lm_output.tools:
                event.tools = lm_output.tools
                logger.info(f"Sending to clients: text='{lm_output.text}', tools={[t.name for t in lm_output.tools]}")
            else:
                logger.debug(f"Sending to clients: text='{lm_output.text}' (no tools)")
            self.text_output_queue.put(event)

        if lm_output.text and response_wants_audio(lm_output.response):
            logger.debug(f"Forwarding to TTS: '{lm_output.text}'")
            yield TTSInput(
                text=lm_output.text,
                language_code=lm_output.language_code,
                runtime_config=lm_output.runtime_config,
                response=lm_output.response,
                turn_id=lm_output.turn_id,
                turn_revision=lm_output.turn_revision,
                speech_stopped_at_s=lm_output.speech_stopped_at_s,
                cancel_generation=lm_output.cancel_generation,
            )
