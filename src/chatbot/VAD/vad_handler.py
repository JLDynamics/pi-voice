from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from queue import Queue
from threading import Event
from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:
    import torch

import numpy as np

from chatbot.api.openai_realtime.runtime_config import RuntimeConfig
from chatbot.baseHandler import BaseHandler
from chatbot.pipeline.events import SpeechStartedEvent, SpeechStoppedEvent
from chatbot.pipeline.handler_types import VADIn, VADOut
from chatbot.pipeline.messages import VADAudio
from chatbot.pipeline.queue_types import TextEventItem
from chatbot.pipeline.speculative_turns import SpeculativeTurnTracker
from chatbot.utils.utils import int2float

logger = logging.getLogger(__name__)

VADInput: TypeAlias = bytes | tuple[bytes, RuntimeConfig]


@dataclass
class _PendingShortSegment:
    audio: np.ndarray
    active_ms: float
    start_ms: int
    end_ms: int


# Fragments with less active speech than this are treated as noise and never
# held for stitching, so sub-threshold bursts cannot sum past min_speech_ms
# and fire a false barge-in.
_SHORT_SEGMENT_MIN_FRAGMENT_MS = 100
# min_speech_ms (384 in the Mac launcher) blocks echo barge-in. Applying that
# same bar to an idle greeting dropped real turns: Silero reported ~550ms of
# active speech inside a padded ~2.4s segment, then the merge window expired
# and the utterance was discarded. Idle turns use this lower floor; barge-in
# still requires min_speech_ms before it cancels the assistant.
# 280ms let a TV blip start a turn (live log: active=288ms, min=280ms). 400ms
# still accepts a ~544ms "hello" and the 448ms greeting that 480ms dropped.
_IDLE_TURN_MIN_SPEECH_MS = 400


class VADHandler(BaseHandler[VADIn, VADOut]):
    """Voice activity detection for a full-duplex speech pipeline.

    Incoming audio is always inspected: ``should_listen`` is kept set so the
    user can barge in while the assistant is speaking. Echo suppression is
    therefore the client's job (browser AEC) plus Silero's speech threshold;
    this handler does not mute the mic when TTS is playing.
    """

    def setup(
        self,
        should_listen: Event,
        speculative_turns: SpeculativeTurnTracker,
        thresh: float = 0.55,
        sample_rate: int = 16000,
        min_silence_ms: int = 350,
        min_speech_ms: int = 400,
        min_speech_continuation_ms: int = 192,
        max_speech_ms: float = float("inf"),
        speech_pad_ms: int = 500,
        text_output_queue: Queue[TextEventItem] | None = None,
        speculative_reopen_ms: int = 800,
        unanswered_reopen_ms: int = 7000,
        reopen_complete_window_ms: int = -1,
        reopen_complete_min_speech_ms: int = 0,
        reopen_require_complete: bool = False,
        short_segment_merge_ms: int = 0,
        smart_turn: bool = True,
        smart_turn_model_path: str | None = None,
        smart_turn_threshold: float = 0.5,
        smart_turn_max_wait_ms: int = 2000,
        smart_turn_incomplete_delay_ms: int = 600,
        smart_turn_cpu_count: int = 1,
        response_playing: Event | None = None,
    ) -> None:
        self.should_listen = should_listen
        self.response_playing = response_playing
        self.sample_rate = sample_rate
        self.min_silence_ms = min_silence_ms
        self.min_speech_ms = min_speech_ms
        if reopen_complete_window_ms < -1:
            raise ValueError(
                f"reopen_complete_window_ms must be -1 (follow speculative_reopen_ms), 0 (disable), or positive, got {reopen_complete_window_ms}"
            )
        if reopen_complete_min_speech_ms < 0:
            raise ValueError(f"reopen_complete_min_speech_ms must be at least 0, got {reopen_complete_min_speech_ms}")
        self.reopen_complete_window_ms = reopen_complete_window_ms
        self.reopen_complete_min_speech_ms = reopen_complete_min_speech_ms
        self.reopen_require_complete = reopen_require_complete
        # Smart Turn verdict per soft-ended (turn_id, revision): (complete, probability).
        # Drives the post-complete noise gate: trailing audio after a complete
        # turn must qualify as a new utterance, and a confident-incomplete
        # reopen must not supersede a confident-complete turn.
        self._smart_turn_complete: dict[tuple[str, int], tuple[bool, float]] = {}
        self.min_speech_continuation_ms = self._resolve_min_speech_continuation_ms(
            self.min_speech_ms,
            min_speech_continuation_ms,
        )
        self.max_speech_ms = max_speech_ms
        self.text_output_queue = text_output_queue
        self.speculative_turns = speculative_turns
        self.speculative_reopen_ms = speculative_reopen_ms
        self.short_segment_merge_ms = max(0, short_segment_merge_ms)
        self._last_turn_detection: dict | None = None
        self.smart_turn_analyzer = None
        self.smart_turn_max_wait_ms = smart_turn_max_wait_ms
        self.smart_turn_incomplete_delay_ms = smart_turn_incomplete_delay_ms
        if smart_turn:
            if smart_turn_max_wait_ms <= 0:
                raise ValueError(f"smart_turn_max_wait_ms must be greater than 0, got {smart_turn_max_wait_ms}")
            if smart_turn_incomplete_delay_ms < 0:
                raise ValueError(
                    f"smart_turn_incomplete_delay_ms must be at least 0, got {smart_turn_incomplete_delay_ms}"
                )
            from chatbot.VAD.smart_turn import SmartTurnAnalyzer

            self.smart_turn_analyzer = SmartTurnAnalyzer(
                model_path=smart_turn_model_path,
                threshold=smart_turn_threshold,
                cpu_count=smart_turn_cpu_count,
            )
        self._smart_turn_pool = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="smart-turn")
            if self.smart_turn_analyzer is not None
            else None
        )
        self.unanswered_reopen_ms = max(
            self.speculative_reopen_ms,
            unanswered_reopen_ms,
            self.smart_turn_max_wait_ms if smart_turn else 0,
        )
        import torch

        from chatbot.VAD.vad_iterator import VADIterator

        self.model, _ = torch.hub.load(
            "snakers4/silero-vad:master",
            "silero_vad",
            trust_repo=True,
            skip_validation=True,
        )
        self.iterator = VADIterator(
            self.model,
            threshold=thresh,
            sampling_rate=sample_rate,
            min_silence_duration_ms=min_silence_ms,
            speech_pad_ms=speech_pad_ms,
        )

        # Cumulative sample counter for audio_start_ms / audio_end_ms
        self._total_samples: int = 0

        # Throttled logging state (summary once per second)
        self._last_log_time = 0.0
        self._log_chunks = 0
        self._log_speech_starts = 0
        self._log_speech_ends = 0
        self._speech_started_emitted = False
        self._turn_counter = 0
        self._current_turn_id: str | None = None
        self._current_turn_revision: int | None = None
        self._speculative_audio_prefix: np.ndarray | None = None
        self._speculative_raw_audio_prefix: np.ndarray | None = None
        self._speculative_active_speech_ms: float = 0.0
        self._last_final_wall_time: float | None = None
        self._last_final_audio_ms: int | None = None
        self._pending_reopen_candidate: tuple[str, int, int] | None = None
        self._pending_short_segment: _PendingShortSegment | None = None

    @property
    def _audio_ms(self) -> int:
        """Cumulative audio received so far, in milliseconds."""
        return int(self._total_samples / self.sample_rate * 1000)

    def _apply_runtime_turn_detection(self, runtime_config: RuntimeConfig | None = None) -> None:
        """Check RuntimeConfig for turn_detection changes and apply them."""
        audio = runtime_config.session.audio if runtime_config else None
        audio_input = audio.input if audio is not None else None
        if not runtime_config or not audio_input or not audio_input.turn_detection:
            return
        td_raw = audio_input.turn_detection

        # Convert Pydantic models (e.g. OpenAI SDK ServerVad) to dict;
        # plain dicts pass through unchanged.
        if hasattr(td_raw, "model_dump"):
            td = td_raw.model_dump(exclude_none=True)
        elif isinstance(td_raw, dict):
            td = td_raw
        else:
            logger.warning(f"Unexpected turn_detection type: {type(td_raw)}")
            return

        # Compare normalized snapshot (identity on td_raw vs stored dict was wrong after first apply).
        if td == self._last_turn_detection:
            return

        self._last_turn_detection = dict(td)

        if "threshold" in td:
            self.iterator.threshold = td["threshold"]
            logger.info(f"VAD threshold updated to {td['threshold']}")
        if "silence_duration_ms" in td:
            self.iterator.min_silence_samples = self.sample_rate * td["silence_duration_ms"] / 1000
            logger.info(f"VAD silence duration updated to {td['silence_duration_ms']}ms")

    def _start_new_turn(self) -> tuple[str, int]:
        self._cancel_pending_reopen()
        self._turn_counter += 1
        self._current_turn_id = f"turn_{self._turn_counter}"
        self._current_turn_revision = 0
        self._speculative_audio_prefix = None
        self._speculative_raw_audio_prefix = None
        self._speculative_active_speech_ms = 0.0
        self._last_final_wall_time = None
        self._last_final_audio_ms = None
        self.speculative_turns.observe(self._current_turn_id, self._current_turn_revision)
        return self._current_turn_id, self._current_turn_revision

    def _speech_buffer_duration_ms(self) -> float:
        # This is polled once per audio chunk, so prefer the iterator's running
        # total over materialising and re-summing the whole chunk list.
        counter = getattr(self.iterator, "speech_buffer_samples", None)
        if counter is not None:
            return counter() / self.sample_rate * 1000
        if not hasattr(self.iterator, "speech_buffer"):
            return 0.0
        buffer_samples = sum(len(t) for t in self.iterator.speech_buffer())
        return buffer_samples / self.sample_rate * 1000

    def _segment_buffer_duration_ms(self) -> float:
        """Duration of the active speech buffer, excluding the pre-speech prefix."""
        counter = getattr(self.iterator, "buffer_samples", None)
        if counter is not None:
            return counter() / self.sample_rate * 1000
        return sum(len(t) for t in self.iterator.buffer) / self.sample_rate * 1000

    def _current_active_speech_duration_ms(self) -> float:
        active_speech_samples = getattr(self.iterator, "active_speech_samples", 0)
        return active_speech_samples / self.sample_rate * 1000

    def _last_utterance_active_speech_duration_ms(self) -> float:
        active_speech_samples = getattr(self.iterator, "last_utterance_active_speech_samples", 0)
        return active_speech_samples / self.sample_rate * 1000

    @staticmethod
    def _resolve_min_speech_continuation_ms(min_speech_ms: int, min_speech_continuation_ms: int) -> int:
        if min_speech_continuation_ms <= 0:
            return min_speech_ms
        return min(
            min_speech_ms,
            max(_SHORT_SEGMENT_MIN_FRAGMENT_MS, min_speech_continuation_ms),
        )

    def _active_speech_min_ms(self, start_ms: int) -> float:
        """Duration hysteresis for speech that continues a reopenable turn."""
        reopenable = self._pending_reopen_candidate is not None or self._should_reopen_current_turn(start_ms)
        if reopenable and not self._reopening_completed_turn():
            return self.min_speech_continuation_ms
        if reopenable:
            # Trailing audio after a Smart-Turn-complete turn is a new
            # utterance, not a continuation: it must clear the fresh-turn bar
            # (or the explicit post-complete override) instead of the weak
            # hysteresis, which only bridges pauses inside an unfinished
            # utterance. Ambient noise that could never start a turn must not
            # resurrect one that just ended.
            override_ms = int(getattr(self, "reopen_complete_min_speech_ms", 0) or 0)
            if override_ms > 0:
                return max(_SHORT_SEGMENT_MIN_FRAGMENT_MS, override_ms)
        playing = getattr(self, "response_playing", None)
        # Tests that omit response_playing keep the historical min_speech_ms bar.
        # Production always passes the Event: idle greetings use a lower floor;
        # echo while the assistant is talking still needs min_speech_ms.
        if playing is None or playing.is_set():
            return self.min_speech_ms
        return min(int(self.min_speech_ms), _IDLE_TURN_MIN_SPEECH_MS)

    def _smart_turn_records(self) -> dict[tuple[str, int], tuple[bool, float]]:
        records = getattr(self, "_smart_turn_complete", None)
        if records is None:
            records = {}
            self._smart_turn_complete = records
        return records

    def _reopening_completed_turn(self) -> bool:
        """Whether the turn a reopen would extend has a Smart Turn complete endpoint.

        Walks back past suppressed noise revisions: once a turn scores
        complete, later audio stays trailing audio until some revision scores
        complete again, so one noise blip cannot launder the next one back
        onto the weak hysteresis path.
        """
        return self._latest_complete_endpoint(self._current_turn_id, self._current_turn_revision) is not None

    def _latest_complete_endpoint(
        self,
        turn_id: str | None,
        at_or_below_rev: int | None,
    ) -> tuple[int, float] | None:
        if turn_id is None or at_or_below_rev is None:
            return None
        best: tuple[int, float] | None = None
        for (recorded_turn, recorded_rev), (complete, probability) in self._smart_turn_records().items():
            if recorded_turn != turn_id or recorded_rev > at_or_below_rev or not complete:
                continue
            if best is None or recorded_rev > best[0]:
                best = (recorded_rev, probability)
        return best

    def _post_complete_reopen_window_ms(self) -> int:
        configured = int(getattr(self, "reopen_complete_window_ms", -1))
        if configured < 0:
            return int(self.speculative_reopen_ms)
        return configured

    def _should_suppress_reopen_emission(
        self,
        turn_id: str | None,
        turn_revision: int | None,
    ) -> bool:
        """Whether a reopened revision must not supersede a complete turn.

        When the soft-ended base turn scored Smart Turn complete and the
        reopened audio scores incomplete, emitting it would re-transcribe the
        whole turn over noise and replace the committed transcript with a
        worse one. The audio stays in the turn prefix (so a genuine
        continuation that completes later still carries it) but nothing is
        sent to STT. Fail-open: without Smart Turn, or when it errors, emit
        exactly as before.
        """
        if not getattr(self, "reopen_require_complete", False):
            return False
        if turn_id is None or turn_revision is None or turn_revision <= 0:
            return False
        base = self._latest_complete_endpoint(turn_id, turn_revision - 1)
        if base is None:
            return False
        reopened = self._smart_turn_records().get((turn_id, turn_revision))
        if reopened is None or reopened[0]:
            return False
        base_rev, base_p = base
        logger.info(
            "VAD: suppressing reopen emission for turn=%s rev=%s "
            "(turn scored complete at rev=%s p=%.3f; reopened audio scores incomplete p=%.3f); "
            "audio retained, nothing sent to STT",
            turn_id,
            turn_revision,
            base_rev,
            base_p,
            reopened[1],
        )
        return True

    def _should_reopen_current_turn(self, audio_start_ms: int) -> bool:
        if self._current_turn_id is None or self._current_turn_revision is None or self._last_final_audio_ms is None:
            return False

        is_committed = self.speculative_turns.is_committed(
            self._current_turn_id,
            self._current_turn_revision,
        )
        if is_committed:
            return False

        # Elapsed is measured on the audio clock, so the window only advances
        # while the client streams audio (continuous capture behaves like wall
        # time; push-to-talk style gaps freeze it).
        elapsed_ms = max(0, audio_start_ms - self._last_final_audio_ms)

        # Within the short grace window, any uncommitted turn may reopen.
        # Beyond it, an unanswered turn (no assistant output committed yet)
        # remains reopenable up to the unanswered_reopen_ms sanity cap, so a
        # user pause longer than speculative_reopen_ms does not orphan a turn
        # the assistant has not replied to. The cap also bounds the
        # empty-transcript case, where no request is queued and the turn would
        # otherwise never commit.
        if self._reopening_completed_turn():
            # A Smart-Turn-complete turn is semantically closed: only the
            # short speculative grace covers its acoustic tail, so late noise
            # cannot resurrect it. Anything later is a new turn. Incomplete
            # turns keep the long unanswered window above.
            window_ms = self._post_complete_reopen_window_ms()
            if window_ms <= 0:
                return False
            return elapsed_ms <= window_ms
        return elapsed_ms <= self.unanswered_reopen_ms

    def _begin_pending_reopen_if_needed(self, audio_start_ms: int) -> None:
        if self._pending_reopen_candidate is not None or not self._should_reopen_current_turn(audio_start_ms):
            return
        candidate_revision = self.speculative_turns.begin_reopen_candidate(
            self._current_turn_id,
            self._current_turn_revision,
        )
        if candidate_revision is None or self._current_turn_id is None or self._current_turn_revision is None:
            return
        self._pending_reopen_candidate = (
            self._current_turn_id,
            self._current_turn_revision,
            candidate_revision,
        )
        logger.info(
            "VAD: pending reopen candidate for speculative turn %s revision %d",
            self._current_turn_id,
            candidate_revision,
        )

    def _cancel_pending_reopen(self) -> None:
        if self._pending_reopen_candidate is None:
            return
        turn_id, _base_revision, candidate_revision = self._pending_reopen_candidate
        self.speculative_turns.cancel_reopen_candidate(turn_id, candidate_revision)
        self._pending_reopen_candidate = None

    def _confirm_pending_reopen(self) -> tuple[str, int, bool] | None:
        if self._pending_reopen_candidate is None:
            return None
        turn_id, base_revision, candidate_revision = self._pending_reopen_candidate
        self._pending_reopen_candidate = None
        if not self.speculative_turns.confirm_reopen_candidate(
            turn_id,
            base_revision,
            candidate_revision,
        ):
            return None
        self._current_turn_id = turn_id
        self._current_turn_revision = candidate_revision
        logger.info("VAD: reopened speculative turn %s revision %d", turn_id, candidate_revision)
        return turn_id, candidate_revision, True

    def _reopen_current_turn(self) -> tuple[str, int, bool] | None:
        if self._current_turn_id is None or self._current_turn_revision is None:
            return None

        turn_id = self._current_turn_id
        base_revision = self._current_turn_revision
        candidate_revision = self.speculative_turns.begin_reopen_candidate(turn_id, base_revision)
        if candidate_revision is None or not self.speculative_turns.confirm_reopen_candidate(
            turn_id,
            base_revision,
            candidate_revision,
        ):
            return None

        self._current_turn_id = turn_id
        self._current_turn_revision = candidate_revision
        logger.info("VAD: reopened speculative turn %s revision %d", turn_id, candidate_revision)
        return turn_id, candidate_revision, True

    def _ensure_turn_for_speech_start(self, audio_start_ms: int) -> tuple[str, int, bool]:
        if (
            self._speech_started_emitted
            and self._current_turn_id is not None
            and self._current_turn_revision is not None
        ):
            return self._current_turn_id, self._current_turn_revision, False

        confirmed_reopen = self._confirm_pending_reopen()
        if confirmed_reopen is not None:
            return confirmed_reopen

        reopened = False
        if self._should_reopen_current_turn(audio_start_ms):
            reopened_turn = self._reopen_current_turn()
            if reopened_turn is not None:
                return reopened_turn

        self._start_new_turn()

        if self._current_turn_id is None or self._current_turn_revision is None:
            raise RuntimeError("VAD failed to allocate turn metadata")
        return self._current_turn_id, self._current_turn_revision, reopened

    def _current_turn_metadata(self) -> tuple[str | None, int | None]:
        return self._current_turn_id, self._current_turn_revision

    def _combined_turn_audio(self, current_segment: np.ndarray) -> np.ndarray:
        if self._speculative_audio_prefix is None:
            return current_segment
        return np.concatenate((self._speculative_audio_prefix, current_segment))

    def _combined_raw_turn_audio(self, current_segment: np.ndarray) -> np.ndarray:
        if self._speculative_raw_audio_prefix is None:
            return current_segment.copy()
        return np.concatenate((self._speculative_raw_audio_prefix, current_segment))

    def _combined_turn_active_speech_ms(self, fragment_ms: float) -> float:
        return float(getattr(self, "_speculative_active_speech_ms", 0.0) or 0.0) + fragment_ms

    def _short_segment_merge_window_ms(self) -> int:
        return int(getattr(self, "short_segment_merge_ms", 0))

    def _segment_duration_ms(self, segment: np.ndarray) -> float:
        return len(segment) / self.sample_rate * 1000

    def _segment_start_ms(self, segment: np.ndarray, end_ms: int) -> int:
        return max(0, end_ms - int(self._segment_duration_ms(segment)))

    def _short_segment_gap_ms(self, start_ms: int) -> float:
        if self._pending_short_segment is None:
            return float("inf")
        return max(0, start_ms - self._pending_short_segment.end_ms)

    def _can_merge_pending_short_segment(self, start_ms: int) -> bool:
        return (
            self._pending_short_segment is not None
            and self._short_segment_merge_window_ms() > 0
            and self._short_segment_gap_ms(start_ms) <= self._short_segment_merge_window_ms()
        )

    def _effective_active_speech_for_start(self, start_ms: int, active_ms: float) -> tuple[int, float]:
        # A live fragment below the noise floor never counts the held segment
        # toward the speech-start threshold, mirroring the finalization path.
        if active_ms < _SHORT_SEGMENT_MIN_FRAGMENT_MS:
            return start_ms, active_ms
        if not self._can_merge_pending_short_segment(start_ms):
            return start_ms, active_ms
        assert self._pending_short_segment is not None
        return self._pending_short_segment.start_ms, self._pending_short_segment.active_ms + active_ms

    def _merge_pending_short_segment(
        self,
        segment: np.ndarray,
        active_ms: float,
        end_ms: int,
    ) -> tuple[np.ndarray, float, int, bool]:
        start_ms = self._segment_start_ms(segment, end_ms)
        if not self._can_merge_pending_short_segment(start_ms):
            self._discard_expired_pending_short_segment(start_ms)
            return segment, active_ms, start_ms, False

        pending = self._pending_short_segment
        assert pending is not None
        # Reinsert the silence between the two segments so the stitched audio
        # keeps its acoustic gap and its length matches the audio-clock span.
        gap_samples = int(self._short_segment_gap_ms(start_ms) * self.sample_rate / 1000)
        self._pending_short_segment = None
        parts = [pending.audio]
        if gap_samples > 0:
            parts.append(np.zeros(gap_samples, dtype=segment.dtype))
        parts.append(segment)
        merged = np.concatenate(parts)
        return merged, pending.active_ms + active_ms, pending.start_ms, True

    def _hold_short_segment(
        self,
        segment: np.ndarray,
        active_ms: float,
        start_ms: int,
        end_ms: int,
        active_min_ms: float | None = None,
    ) -> None:
        self._pending_short_segment = _PendingShortSegment(
            audio=segment,
            active_ms=active_ms,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        logger.info(
            "VAD: holding short segment=%.0fms active=%.0fms (active_min=%sms, merge_max=%sms)",
            self._segment_duration_ms(segment),
            active_ms,
            self.min_speech_ms if active_min_ms is None else active_min_ms,
            self._short_segment_merge_window_ms(),
        )

    def _discard_pending_short_segment(self, reason: str = "expired") -> None:
        pending = self._pending_short_segment
        if pending is None:
            return
        self._pending_short_segment = None
        logger.info(
            "VAD: discarding held short segment=%.0fms active=%.0fms (%s, active_min=%sms)",
            self._segment_duration_ms(pending.audio),
            pending.active_ms,
            reason,
            self.min_speech_ms,
        )

    def _discard_expired_pending_short_segment(self, next_start_ms: int | None = None) -> None:
        pending = self._pending_short_segment
        if pending is None or self._short_segment_merge_window_ms() <= 0:
            return
        reference_ms = self._audio_ms if next_start_ms is None else next_start_ms
        gap_ms = max(0, reference_ms - pending.end_ms)
        if gap_ms > self._short_segment_merge_window_ms():
            self._discard_pending_short_segment("merge window elapsed")

    def before_emit_output(self, output: VADOut) -> None:
        if isinstance(output, VADAudio):
            self._drop_superseded_vad_audio(output)

    def _drop_superseded_vad_audio(self, latest: VADAudio) -> int:
        if not hasattr(self.queue_out, "mutex") or not hasattr(self.queue_out, "queue"):
            return 0

        dropped = 0
        with self.queue_out.mutex:
            kept: list[Any] = []
            while self.queue_out.queue:
                queued_item = self.queue_out.queue.popleft()
                if isinstance(queued_item, VADAudio) and self._vad_audio_is_superseded(
                    queued_item,
                    latest,
                ):
                    dropped += 1
                else:
                    kept.append(queued_item)
            self.queue_out.queue.extend(kept)
            if dropped:
                self.queue_out.not_full.notify_all()

        if dropped:
            logger.debug(
                "VAD: dropped %d superseded audio chunk(s) before enqueueing turn=%s rev=%s",
                dropped,
                latest.turn_id,
                latest.turn_revision,
            )
        return dropped

    def _vad_audio_is_superseded(self, queued_item: VADAudio, latest: VADAudio) -> bool:
        if queued_item.turn_id is None or queued_item.turn_revision is None:
            return False
        return not self.speculative_turns.is_latest(
            queued_item.turn_id,
            queued_item.turn_revision,
        )

    def _smart_turn_timing_ms(
        self,
        audio: np.ndarray,
        *,
        turn_id: str | None = None,
        turn_revision: int | None = None,
    ) -> tuple[int, int]:
        """Return the response grace and pre-processing delay for this endpoint.

        When the soft-ended turn identity is given, the verdict is also
        recorded for the post-complete noise gate. Failures record nothing,
        so the gate fails open toward emitting (historical behavior).
        """
        analyzer = getattr(self, "smart_turn_analyzer", None)
        if analyzer is None:
            return self.speculative_reopen_ms, 0

        try:
            pool = getattr(self, "_smart_turn_pool", None)
            if pool is None:
                result = analyzer.predict(audio, sample_rate=self.sample_rate)
            else:
                future = pool.submit(analyzer.predict, audio, sample_rate=self.sample_rate)
                # Bound how long the VAD thread waits so incoming chunks keep moving.
                result = future.result(timeout=min(0.08, max(0.01, self.smart_turn_max_wait_ms / 1000.0)))
        except FuturesTimeout:
            logger.warning("Smart Turn still running; using the default speculative reopen grace")
            return self.speculative_reopen_ms, 0
        except Exception:
            # A transient classifier failure falls back to the ordinary short
            # speculative window instead of delaying the response for seconds.
            logger.exception("Smart Turn inference failed; using the default speculative reopen grace")
            return self.speculative_reopen_ms, 0

        if turn_id is not None and turn_revision is not None:
            self._smart_turn_records()[(turn_id, turn_revision)] = (result.complete, result.probability)

        if result.complete:
            logger.info(
                "Smart Turn: complete (p=%.3f, %.1fms); using %dms speculative reopen grace",
                result.probability,
                result.inference_ms,
                self.speculative_reopen_ms,
            )
            return self.speculative_reopen_ms, 0

        processing_delay_ms = min(self.smart_turn_incomplete_delay_ms, self.smart_turn_max_wait_ms)
        logger.info(
            "Smart Turn: incomplete (p=%.3f, %.1fms); using %dms speculative reopen grace "
            "and delaying processing by %dms",
            result.probability,
            result.inference_ms,
            self.smart_turn_max_wait_ms,
            processing_delay_ms,
        )
        return self.smart_turn_max_wait_ms, processing_delay_ms

    def process(self, audio_chunk: VADIn) -> Iterator[VADOut]:
        runtime_config = None
        if isinstance(audio_chunk, tuple):
            audio_chunk, runtime_config = audio_chunk
        self._apply_runtime_turn_detection(runtime_config)

        # Full-duplex: never drop chunks while the assistant is speaking.
        # ``should_listen`` stays set; call sites that ``.set()`` it are
        # leftovers from a half-duplex mute and are intentional no-ops.
        self._log_chunks += 1
        audio_int16 = np.frombuffer(audio_chunk, dtype=np.int16)
        self._total_samples += len(audio_int16)
        audio_float32 = int2float(audio_int16)

        import torch

        vad_output = self.iterator(torch.from_numpy(audio_float32))

        # Deferred speech_started: only emit once active VAD speech reaches the valid speech threshold.
        is_triggered_now = self.iterator.triggered
        if is_triggered_now and not self._speech_started_emitted:
            active_speech_duration_ms = self._current_active_speech_duration_ms()
            speech_buffer_duration_ms = self._speech_buffer_duration_ms()
            start_ms = max(0, self._audio_ms - int(speech_buffer_duration_ms))
            effective_start_ms, effective_active_speech_duration_ms = self._effective_active_speech_for_start(
                start_ms,
                active_speech_duration_ms,
            )
            self._begin_pending_reopen_if_needed(effective_start_ms)
            active_speech_min_ms = self._active_speech_min_ms(effective_start_ms)
            if effective_active_speech_duration_ms >= active_speech_min_ms:
                turn_id, turn_revision, reopened = self._ensure_turn_for_speech_start(effective_start_ms)
                self._speech_started_emitted = True
                self._log_speech_starts += 1
                logger.info(
                    "Speech started (confirmed, active=%.0fms, min=%.0fms, segment=%.0fms, turn=%s rev=%s)",
                    effective_active_speech_duration_ms,
                    active_speech_min_ms,
                    self._segment_buffer_duration_ms(),
                    turn_id,
                    turn_revision,
                )
                if self.text_output_queue:
                    # Continuation hysteresis may reopen a soft-ended turn for
                    # STT, but must not cancel TTS. A real barge-in needs a
                    # full min_speech_ms of active speech (echo / "um" was
                    # cutting replies mid-sentence). Reopens of a
                    # Smart-Turn-complete turn already cleared the fresh-turn
                    # bar instead of the hysteresis (see _active_speech_min_ms).
                    self.text_output_queue.put(
                        SpeechStartedEvent(
                            audio_start_ms=effective_start_ms,
                            turn_id=turn_id,
                            turn_revision=turn_revision,
                            reopened=reopened,
                            interrupt_response=effective_active_speech_duration_ms >= self.min_speech_ms,
                        )
                    )
        elif not is_triggered_now and vad_output is None:
            self._discard_expired_pending_short_segment()

        # Log a summary once per second instead of every chunk
        now = time.time()
        if now - self._last_log_time >= 1.0:
            state = "SPEAKING" if is_triggered_now else "silent"
            logger.debug(
                f"VAD: {self._log_chunks} chunks/s | {state} | "
                f"starts={self._log_speech_starts} ends={self._log_speech_ends}"
            )
            self._log_chunks = 0
            self._log_speech_starts = 0
            self._log_speech_ends = 0
            self._last_log_time = now

        yield from self._process_realtime(vad_output, runtime_config)

    def _process_realtime(
        self,
        vad_output: list[torch.Tensor] | None,
        runtime_config: RuntimeConfig | None = None,
    ) -> Iterator[VADOut]:
        """Emit the finalized utterance when speech ends."""
        # Handle end of speech
        if vad_output is not None:
            if len(vad_output) == 0:
                logger.info("VAD: phantom trigger (empty buffer), closing speech pair")
                if self._speech_started_emitted and self.text_output_queue:
                    turn_id, turn_revision = self._current_turn_metadata()
                    self.text_output_queue.put(
                        SpeechStoppedEvent(
                            audio_end_ms=self._audio_ms,
                            turn_id=turn_id,
                            turn_revision=turn_revision,
                        )
                    )
                if not self._speech_started_emitted:
                    self._cancel_pending_reopen()
                self._speech_started_emitted = False
                self._discard_expired_pending_short_segment()
                return

            import torch

            array = torch.cat(vad_output).cpu().numpy()
            end_ms = self._audio_ms
            raw_active_ms = self._last_utterance_active_speech_duration_ms()
            active_speech_duration_ms = raw_active_ms
            stitched_short_segment = False
            # Fragments below the noise floor never merge with or replace a
            # held segment; the pending segment's own expiry handles it.
            if raw_active_ms >= _SHORT_SEGMENT_MIN_FRAGMENT_MS:
                array, active_speech_duration_ms, start_ms, stitched_short_segment = self._merge_pending_short_segment(
                    array,
                    active_speech_duration_ms,
                    end_ms,
                )
            else:
                start_ms = self._segment_start_ms(array, end_ms)
            duration_ms = self._segment_duration_ms(array)
            min_active_ms = 0.0 if self._speech_started_emitted else self._active_speech_min_ms(start_ms)

            duration_exceeds_limit = duration_ms > self.max_speech_ms
            if active_speech_duration_ms < min_active_ms or duration_exceeds_limit:
                if (
                    self._short_segment_merge_window_ms() > 0
                    and raw_active_ms >= _SHORT_SEGMENT_MIN_FRAGMENT_MS
                    and active_speech_duration_ms < min_active_ms
                    and duration_ms <= self.max_speech_ms
                ):
                    self._hold_short_segment(array, active_speech_duration_ms, start_ms, end_ms, min_active_ms)
                else:
                    logger.info(
                        "VAD: discarding segment=%.0fms active=%.0fms (active_min=%sms, segment_max=%sms)",
                        duration_ms,
                        active_speech_duration_ms,
                        min_active_ms,
                        self.max_speech_ms,
                    )
                if self._speech_started_emitted and self.text_output_queue:
                    turn_id, turn_revision = self._current_turn_metadata()
                    self.text_output_queue.put(
                        SpeechStoppedEvent(
                            audio_end_ms=self._audio_ms,
                            turn_id=turn_id,
                            turn_revision=turn_revision,
                        )
                    )
                if not self._speech_started_emitted:
                    self._cancel_pending_reopen()
                self._speech_started_emitted = False
            else:
                if stitched_short_segment:
                    logger.info(
                        "VAD: stitched short segment(s) into segment=%.0fms active=%.0fms",
                        duration_ms,
                        active_speech_duration_ms,
                    )
                if not self._speech_started_emitted:
                    turn_id, turn_revision, reopened = self._ensure_turn_for_speech_start(start_ms)
                    if self.text_output_queue:
                        # This path is a final segment that never crossed the
                        # live speech-start bar. Noise below the fragment floor
                        # was already dropped; anything that survived stitching
                        # and the min-active check is a real short command
                        # ("stop"/"wait") and should barge in.
                        self.text_output_queue.put(
                            SpeechStartedEvent(
                                audio_start_ms=start_ms,
                                turn_id=turn_id,
                                turn_revision=turn_revision,
                                reopened=reopened,
                                interrupt_response=active_speech_duration_ms >= 200,
                            )
                        )
                else:
                    turn_id, turn_revision = self._current_turn_metadata()
                self._log_speech_ends += 1
                analysis_audio = self._combined_raw_turn_audio(array)
                output_array = self._combined_turn_audio(array)
                combined_duration_s = len(output_array) / self.sample_rate
                turn_active_ms = self._combined_turn_active_speech_ms(active_speech_duration_ms)
                logger.info(
                    "Speech soft-ended (segment=%.0fms, active=%.0fms, turn_active=%.0fms, turn=%s rev=%s)",
                    duration_ms,
                    active_speech_duration_ms,
                    turn_active_ms,
                    turn_id,
                    turn_revision,
                )
                reopen_grace_ms, processing_delay_ms = self._smart_turn_timing_ms(
                    analysis_audio,
                    turn_id=turn_id,
                    turn_revision=turn_revision,
                )
                if self.text_output_queue:
                    self.text_output_queue.put(
                        SpeechStoppedEvent(
                            duration_s=combined_duration_s,
                            audio_end_ms=end_ms,
                            turn_id=turn_id,
                            turn_revision=turn_revision,
                        )
                    )
                self._speculative_audio_prefix = output_array
                self._speculative_raw_audio_prefix = analysis_audio
                self._speculative_active_speech_ms = turn_active_ms
                self._last_final_wall_time = time.time()
                self._last_final_audio_ms = end_ms
                if self._should_suppress_reopen_emission(turn_id, turn_revision):
                    # SpeechStopped above still closes the client-side pair;
                    # only the STT trigger is withheld. No reopen grace is
                    # started: nothing downstream waits on this revision.
                    self._speech_started_emitted = False
                    return
                # The grace only delays response commits. Resumed speech
                # follows the existing candidate/revision flow and makes
                # this revision stale before assistant output is released.
                self.speculative_turns.start_reopen_grace(
                    turn_id,
                    turn_revision,
                    reopen_grace_ms / 1000.0,
                )
                yield VADAudio(
                    audio=output_array,
                    runtime_config=runtime_config,
                    turn_id=turn_id,
                    turn_revision=turn_revision,
                    processing_delay_s=processing_delay_ms / 1000.0,
                    active_speech_ms=turn_active_ms,
                )
                self._speech_started_emitted = False

    def on_session_end(self):
        self.iterator.reset_states()
        self._pending_short_segment = None
        self.iterator.buffer = []
        self._total_samples = 0
        self._speech_started_emitted = False
        self._turn_counter = 0
        self._current_turn_id = None
        self._current_turn_revision = None
        self._speculative_audio_prefix = None
        self._speculative_raw_audio_prefix = None
        self._speculative_active_speech_ms = 0.0
        self._last_final_wall_time = None
        self._last_final_audio_ms = None
        self._pending_reopen_candidate = None
        self._smart_turn_records().clear()
        self.speculative_turns.reset()
        self.should_listen.set()
        logger.debug("VAD session state reset")

    @property
    def min_time_to_debug(self) -> float:
        return 0.00001
