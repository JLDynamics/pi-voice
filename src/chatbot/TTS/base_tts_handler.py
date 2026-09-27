"""Shared skeleton for the streaming TTS backend.

A backend runs this streaming pipeline: generate audio -> per-backend mapping
-> fixed-size block chunking with
leftover carry. Backends differ only in how they generate (`_generate`), map
native-rate chunks (`_map_chunk`), flush tail audio (`_model_tail`), and pick
voices (`_apply_voice`).

Siri is the only backend now. The MLX lock this used to hold went with the MLX
backends: nothing here loads a model into GPU memory any more.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from queue import Queue
from threading import Event
from time import perf_counter
from typing import Any

import numpy as np
from rich.console import Console

from chatbot.baseHandler import BaseHandler
from chatbot.pipeline.cancel_scope import CancelScope
from chatbot.pipeline.control import SESSION_END, is_control_message
from chatbot.pipeline.events import ResponseFailedEvent
from chatbot.pipeline.handler_types import TTSIn, TTSOut
from chatbot.pipeline.messages import AUDIO_RESPONSE_DONE, PIPELINE_END, EndOfResponse, TTSInput
from chatbot.pipeline.queue_types import TextEventItem
from chatbot.pipeline.speculative_turns import SpeculativeTurnTracker
from chatbot.TTS.tts_common import drop_queued_tts_inputs, strip_denoise_kwargs

logger = logging.getLogger(__name__)
console = Console()

WARMUP_TEXT = "Hello, this is a warmup."


class BaseTTSHandler(BaseHandler[TTSIn, TTSOut]):
    """Streaming MLX TTS with turn gating, coalescing, and failure guards."""

    backend_name = "TTS"
    PIPELINE_SR = 24000
    BLOCK_SIZE = 512
    model: Any

    def _common_setup(
        self,
        should_listen: Event,
        gen_kwargs: dict[str, Any] | None,
        cancel_scope: CancelScope | None,
        speculative_turns: SpeculativeTurnTracker | None,
        text_output_queue: Queue[TextEventItem] | None,
        model_name: str,
        blocksize: int = BLOCK_SIZE,
    ) -> None:
        self.should_listen = should_listen
        self.gen_kwargs = gen_kwargs or {}
        self.cancel_scope = cancel_scope
        self.speculative_turns = speculative_turns
        self.text_output_queue = text_output_queue
        self._failed_turn: tuple[str | None, int | None] | None = None
        self.model_name = model_name
        self.blocksize = int(blocksize)

    def warmup(self) -> None:
        logger.info("Warming up %s", self.backend_name)
        try:
            for _ in self._process(WARMUP_TEXT):
                pass
        except Exception as exc:  # startup remains usable when a warmup-only call fails
            logger.warning("%s warmup failed: %s", self.backend_name, exc)

    # ── per-backend hooks ──────────────────────────────────────────────

    def _generate(self, text: str, gen_kwargs: dict[str, Any]) -> Iterable[Any]:
        """Yield model chunks with an ``audio`` array attribute."""
        raise NotImplementedError

    def _map_chunk(self, audio: np.ndarray) -> np.ndarray:
        """Map one native-rate model chunk to pipeline-rate float32 audio."""
        return audio

    def _model_tail(self) -> np.ndarray:
        """Trailing model audio to flush (e.g. resampler tail), float32."""
        return np.zeros(0, dtype=np.float32)

    def _apply_voice(self, tts_input: TTSInput) -> None:
        """Pick the voice for this turn (no-op unless the backend switches)."""

    # ── shared streaming pipeline ──────────────────────────────────────

    def _process(self, text: str) -> Iterator[np.ndarray]:
        name = self.backend_name
        blocksize = self.blocksize
        gen_kwargs = dict(self.gen_kwargs)
        gen_kwargs = strip_denoise_kwargs(gen_kwargs)
        generation = self._generate(text, gen_kwargs)
        cancel_generation = self.cancel_scope.generation if self.cancel_scope else None
        leftover = np.array([], dtype=np.int16)
        total_samples = 0
        start = perf_counter()
        first = True
        for item in generation:
            if cancel_generation is not None and self.cancel_scope and self.cancel_scope.is_stale(cancel_generation):
                logger.info("%s generation cancelled", name)
                return
            audio = np.asarray(item.audio, dtype=np.float32)
            if first:
                logger.info("%s TTFA %.2fs", name, perf_counter() - start)
                first = False
            if not audio.size:
                continue
            pcm = self._map_chunk(audio)
            pcm = np.clip(pcm * 32768, -32768, 32767).astype(np.int16)
            pcm = np.concatenate((leftover, pcm))
            complete = len(pcm) // blocksize * blocksize
            for offset in range(0, complete, blocksize):
                yield pcm[offset : offset + blocksize]
                total_samples += blocksize
            leftover = pcm[complete:]
        tail_float = self._model_tail()
        tail = np.clip(tail_float * 32768, -32768, 32767).astype(np.int16)
        pcm = np.concatenate((leftover, tail))
        complete = len(pcm) // blocksize * blocksize
        for offset in range(0, complete, blocksize):
            yield pcm[offset : offset + blocksize]
            total_samples += blocksize
        leftover = pcm[complete:]
        if leftover.size:
            yield np.pad(leftover, (0, blocksize - len(leftover)))
            total_samples += len(leftover)
        audio_s = total_samples / self.PIPELINE_SR
        elapsed = perf_counter() - start
        rtf = elapsed / audio_s if audio_s > 0 else 0.0
        logger.info("%s generated %.2fs of audio in %.2fs (RTF %.2f)", name, audio_s, elapsed, rtf)

    def _coalesce(self, current: TTSInput) -> str:
        parts = [current.text.strip()] if current.text.strip() else []
        if not hasattr(self.queue_in, "mutex"):
            return " ".join(parts)
        with self.queue_in.mutex:
            while self.queue_in.queue:
                following = self.queue_in.queue[0]
                if (
                    is_control_message(following, SESSION_END.kind)
                    or (isinstance(following, bytes) and following == PIPELINE_END)
                    or isinstance(following, EndOfResponse)
                    or not isinstance(following, TTSInput)
                ):
                    break
                if (current.turn_id, current.turn_revision) != (following.turn_id, following.turn_revision):
                    break
                self.queue_in.queue.popleft()
                if following.text.strip():
                    parts.append(following.text.strip())
        return " ".join(parts)

    def process(self, tts_input: TTSIn) -> Iterator[TTSOut]:
        name = self.backend_name
        tracker = self.speculative_turns
        if isinstance(tts_input, EndOfResponse):
            self._failed_turn = None
            if not tracker or tracker.is_latest(tts_input.turn_id, tts_input.turn_revision):
                yield AUDIO_RESPONSE_DONE
            return
        turn_key = (tts_input.turn_id, tts_input.turn_revision)
        if getattr(self, "_failed_turn", None) == turn_key:
            return
        if tracker and not tracker.is_latest(tts_input.turn_id, tts_input.turn_revision):
            return
        if tracker:
            tracker.commit(tts_input.turn_id, tts_input.turn_revision)
        self._apply_voice(tts_input)
        text = self._coalesce(tts_input)
        if not text:
            return
        console.print(f"[green]ASSISTANT: {text}")
        try:
            first = True
            produced = 0
            for audio in self._process(text):
                if first and tts_input.speech_stopped_at_s is not None:
                    logger.info("Speech stopped to first audio: %.3fs", perf_counter() - tts_input.speech_stopped_at_s)
                    first = False
                produced += int(getattr(audio, "size", len(audio)))
                yield audio
            if produced <= self.blocksize and len(text) > 80:
                logger.warning(
                    "%s produced almost no audio for a long prompt (%d chars, %d samples); possible early EOS",
                    name,
                    len(text),
                    produced,
                )
        except Exception as exc:
            logger.error("%s generation failed: %s", name, exc, exc_info=True)
            self._failed_turn = turn_key
            drop_queued_tts_inputs(self.queue_in, tts_input.turn_id, tts_input.turn_revision)
            if self.text_output_queue is not None:
                self.text_output_queue.put(
                    ResponseFailedEvent(
                        message=f"{name} generation failed: {exc}",
                        turn_id=tts_input.turn_id,
                        turn_revision=tts_input.turn_revision,
                    )
                )

    def cleanup(self) -> None:
        """Nothing resident to release; backends that hold something override this."""
