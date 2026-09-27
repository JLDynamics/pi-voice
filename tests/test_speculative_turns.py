import time
from queue import Queue
from threading import Event, Thread

import numpy as np
import pytest
import torch

from chatbot.pipeline.events import SpeechStartedEvent, SpeechStoppedEvent
from chatbot.pipeline.messages import VADAudio
from chatbot.pipeline.speculative_turns import SpeculativeTurnTracker
from chatbot.VAD.smart_turn import SmartTurnResult
from chatbot.VAD.vad_handler import VADHandler


def test_pending_reopen_defers_commit_until_cancelled():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    candidate_revision = tracker.begin_reopen_candidate("turn_1", 0)

    tracker.commit("turn_1", 0)

    assert candidate_revision == 1
    assert not tracker.is_committed("turn_1", 0)

    tracker.cancel_reopen_candidate("turn_1", candidate_revision)
    tracker.commit("turn_1", 0)

    assert tracker.is_committed("turn_1", 0)


def test_confirmed_reopen_makes_previous_revision_stale():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    candidate_revision = tracker.begin_reopen_candidate("turn_1", 0)

    assert tracker.confirm_reopen_candidate("turn_1", 0, candidate_revision)

    assert not tracker.is_latest("turn_1", 0)
    assert tracker.is_latest("turn_1", 1)


def test_tracker_prunes_old_turn_revisions():
    tracker = SpeculativeTurnTracker(max_tracked_turns=2)
    tracker.observe("turn_1", 0)
    tracker.commit("turn_1", 0)
    tracker.observe("turn_2", 0)
    tracker.observe("turn_3", 0)

    assert list(tracker._latest_revision) == ["turn_2", "turn_3"]
    assert "turn_1" not in tracker._committed_revision


def test_tracker_keeps_pending_reopen_while_pruning():
    tracker = SpeculativeTurnTracker(max_tracked_turns=1)
    tracker.observe("turn_1", 0)
    candidate_revision = tracker.begin_reopen_candidate("turn_1", 0)

    tracker.observe("turn_2", 0)

    assert candidate_revision == 1
    assert "turn_1" in tracker._latest_revision
    assert "turn_1" in tracker._pending_reopen
    assert "turn_2" in tracker._latest_revision


def test_pending_reopen_wait_timeout_keeps_candidate():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    candidate_revision = tracker.begin_reopen_candidate("turn_1", 0)

    tracker.wait_for_pending_reopen("turn_1", 0, timeout_s=0)

    assert candidate_revision == 1
    assert tracker._pending_reopen["turn_1"].candidate_revision == 1
    assert tracker.confirm_reopen_candidate("turn_1", 0, candidate_revision)


def test_commit_if_latest_waits_for_pending_reopen_and_drops_confirmed_reopen():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    candidate_revision = tracker.begin_reopen_candidate("turn_1", 0)

    assert tracker.confirm_reopen_candidate("turn_1", 0, candidate_revision)
    assert not tracker.commit_if_latest_after_pending_reopen("turn_1", 0)
    assert not tracker.is_committed("turn_1", 0)


def test_commit_if_latest_commits_after_pending_reopen_is_cancelled():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    candidate_revision = tracker.begin_reopen_candidate("turn_1", 0)

    tracker.cancel_reopen_candidate("turn_1", candidate_revision)

    assert tracker.commit_if_latest_after_pending_reopen("turn_1", 0)
    assert tracker.is_committed("turn_1", 0)


def test_try_is_latest_after_pending_reopen_reports_pending_without_blocking():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    candidate_revision = tracker.begin_reopen_candidate("turn_1", 0)

    assert tracker.has_pending_reopen("turn_1", 0)
    assert tracker.try_is_latest_after_pending_reopen("turn_1", 0) is None
    assert tracker.try_commit_if_latest_after_pending_reopen("turn_1", 0) is None

    tracker.cancel_reopen_candidate("turn_1", candidate_revision)

    assert tracker.try_is_latest_after_pending_reopen("turn_1", 0) is True
    assert tracker.try_commit_if_latest_after_pending_reopen("turn_1", 0) is True
    assert tracker.is_committed("turn_1", 0)


def test_try_is_latest_after_reopen_grace_reports_pending_without_blocking():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    tracker.start_reopen_grace("turn_1", 0, grace_s=0.05)

    assert tracker.try_is_latest_after_reopen_grace("turn_1", 0) is None
    assert tracker.try_commit_if_latest_after_reopen_grace("turn_1", 0) is None

    time.sleep(0.06)

    assert tracker.try_is_latest_after_reopen_grace("turn_1", 0) is True
    assert tracker.try_commit_if_latest_after_reopen_grace("turn_1", 0) is True
    assert tracker.is_committed("turn_1", 0)


def test_reopen_grace_wait_drops_confirmed_reopen():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    tracker.start_reopen_grace("turn_1", 0, grace_s=0.2)
    result: dict[str, bool] = {}

    def wait_for_grace():
        result["is_latest"] = tracker.is_latest_after_reopen_grace("turn_1", 0)

    thread = Thread(target=wait_for_grace)
    thread.start()

    time.sleep(0.02)
    candidate_revision = tracker.begin_reopen_candidate("turn_1", 0)
    assert tracker.confirm_reopen_candidate("turn_1", 0, candidate_revision)
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert result == {"is_latest": False}


def test_is_latest_after_stability_window_catches_reopen_started_during_wait():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)

    def reopen_turn():
        time.sleep(0.02)
        candidate_revision = tracker.begin_reopen_candidate("turn_1", 0)
        assert tracker.confirm_reopen_candidate("turn_1", 0, candidate_revision)

    thread = Thread(target=reopen_turn)
    thread.start()

    assert not tracker.is_latest_after_stability_window("turn_1", 0, settle_s=0.2)
    thread.join(timeout=1.0)


def test_is_latest_after_stability_window_survives_cancelled_reopen_candidate(monkeypatch):
    """Cancelled reopen during settle must resume the window, not return early.

    The previous version raced short wall-clock sleeps (0.02/0.03) against a
    0.2s settle deadline. Under CI load those sleeps overshoot, the original
    deadline expires before ``assert thread.is_alive()``, and the test flakes
    even though ``SpeculativeTurnTracker`` behaved correctly.

    Synchronize on the waiter actually parking on the pending reopen instead,
    then cancel immediately so plenty of settle time remains.
    """
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    pending_wait_started = Event()
    result: list[bool] = []
    settle_s = 0.5
    original_wait_for_pending = tracker._wait_for_pending_reopen_locked

    def wait_for_pending_and_signal(turn_id: str, revision: int, timeout_s: float) -> None:
        pending_wait_started.set()
        original_wait_for_pending(turn_id, revision, timeout_s)

    monkeypatch.setattr(tracker, "_wait_for_pending_reopen_locked", wait_for_pending_and_signal)

    def wait_for_stability():
        result.append(tracker.is_latest_after_stability_window("turn_1", 0, settle_s=settle_s))

    thread = Thread(target=wait_for_stability)
    thread.start()

    # Keep a reopen candidate outstanding until the stability waiter observes it.
    # begin_reopen_candidate is idempotent for the same base revision.
    candidate_revision = None
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if candidate_revision is None:
            candidate_revision = tracker.begin_reopen_candidate("turn_1", 0)
            assert candidate_revision is not None
        if pending_wait_started.wait(timeout=0.05):
            break
    else:
        pytest.fail("stability waiter never observed the reopen candidate")

    tracker.cancel_reopen_candidate("turn_1", candidate_revision)

    # Give a buggy early-return after cancel a chance to finish, then confirm
    # the settle window is still in progress.
    time.sleep(0.05)
    assert thread.is_alive()
    assert result == []

    thread.join(timeout=settle_s + 1.0)
    assert not thread.is_alive()
    assert result == [True]


def test_commit_after_reset_does_not_resurrect_untracked_turn():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    tracker.reset()

    tracker.commit("turn_1", 0)

    assert tracker._committed_revision == {}
    assert not tracker.is_committed("turn_1", 0)


def test_commit_after_prune_does_not_resurrect_untracked_turn():
    tracker = SpeculativeTurnTracker(max_tracked_turns=1)
    tracker.observe("turn_1", 0)
    tracker.observe("turn_2", 0)

    tracker.commit("turn_1", 0)

    assert list(tracker._latest_revision) == ["turn_2"]
    assert tracker._committed_revision == {}


@pytest.mark.parametrize(
    "commit_method",
    [
        "commit_if_latest_after_pending_reopen",
        "commit_if_latest_after_reopen_grace",
        "try_commit_if_latest_after_pending_reopen",
        "try_commit_if_latest_after_reopen_grace",
    ],
)
def test_commit_if_latest_variants_keep_untracked_turn_out_of_committed_state(commit_method):
    """An untracked turn still reports success -- callers treat `False` as "drop this
    output" -- but it must not be written back into `_committed_revision`."""
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    tracker.reset()

    assert getattr(tracker, commit_method)("turn_1", 0) is True
    assert tracker._committed_revision == {}


def test_reused_turn_id_after_reset_is_not_reported_as_committed():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    tracker.reset()
    tracker.commit("turn_1", 0)

    tracker.observe("turn_1", 0)

    assert not tracker.is_committed("turn_1", 0)
    assert tracker.begin_reopen_candidate("turn_1", 0) == 1


def test_vad_direct_reopen_path_uses_tracker_candidate_protocol():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    handler = object.__new__(VADHandler)
    handler.enable_realtime_transcription = True
    handler._speech_started_emitted = False
    handler._current_turn_id = "turn_1"
    handler._current_turn_revision = 0
    handler._last_final_audio_ms = 1000
    handler.speculative_reopen_ms = 1200
    handler.unanswered_reopen_ms = 1200
    handler.speculative_turns = tracker
    handler._pending_reopen_candidate = None

    turn_id, revision, reopened = handler._ensure_turn_for_speech_start(1100)

    assert (turn_id, revision, reopened) == ("turn_1", 1, True)
    assert not tracker.is_latest("turn_1", 0)
    assert tracker.is_latest("turn_1", 1)
    assert tracker._pending_reopen == {}


def test_vad_reopens_speculative_turn_after_a_short_pause():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    handler = object.__new__(VADHandler)
    handler.enable_realtime_transcription = False
    handler._speech_started_emitted = False
    handler._current_turn_id = "turn_1"
    handler._current_turn_revision = 0
    handler._last_final_audio_ms = 1000
    handler.speculative_reopen_ms = 1200
    handler.unanswered_reopen_ms = 1200
    handler.speculative_turns = tracker
    handler._pending_reopen_candidate = None

    turn_id, revision, reopened = handler._ensure_turn_for_speech_start(1100)

    assert (turn_id, revision, reopened) == ("turn_1", 1, True)
    assert not tracker.is_latest("turn_1", 0)
    assert tracker.is_latest("turn_1", 1)


def test_vad_starts_new_turn_after_committed_turn_would_have_reopened():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 0)
    tracker.commit("turn_1", 0)
    handler = object.__new__(VADHandler)
    handler.enable_realtime_transcription = False
    handler._speech_started_emitted = False
    handler._current_turn_id = "turn_1"
    handler._current_turn_revision = 0
    handler._turn_counter = 1
    handler._last_final_audio_ms = 1000
    handler.speculative_reopen_ms = 1200
    handler.speculative_turns = tracker
    handler._pending_reopen_candidate = None

    turn_id, revision, reopened = handler._ensure_turn_for_speech_start(1100)

    assert (turn_id, revision, reopened) == ("turn_2", 0, False)
    assert tracker.is_committed("turn_1", 0)
    assert tracker.is_latest("turn_2", 0)


class _StaticVADIterator:
    def __init__(
        self,
        *,
        triggered: bool,
        vad_output: list[torch.Tensor] | None,
        buffer_chunks: list[torch.Tensor] | None = None,
        speech_chunks: list[torch.Tensor] | None = None,
        active_speech_samples: int = 0,
        last_utterance_active_speech_samples: int = 0,
    ) -> None:
        self.triggered = triggered
        self._vad_output = vad_output
        self.buffer = buffer_chunks or []
        self._speech_chunks = speech_chunks or self.buffer
        self.active_speech_samples = active_speech_samples
        self.last_utterance_active_speech_samples = last_utterance_active_speech_samples

    def __call__(self, _chunk: torch.Tensor) -> list[torch.Tensor] | None:
        return self._vad_output

    def speech_buffer(self) -> list[torch.Tensor]:
        return self._speech_chunks


class _StaticSmartTurnAnalyzer:
    def __init__(self, *results: SmartTurnResult) -> None:
        self._results = iter(results)
        self.calls: list[np.ndarray] = []

    def predict(self, audio: np.ndarray, *, sample_rate: int) -> SmartTurnResult:
        assert sample_rate == 16000
        self.calls.append(audio.copy())
        return next(self._results)


def _vad_handler_for_iterator(iterator: _StaticVADIterator) -> VADHandler:
    handler = object.__new__(VADHandler)
    handler.should_listen = Event()
    handler.should_listen.set()
    handler.sample_rate = 16000
    handler.min_silence_ms = 300
    handler.min_speech_ms = 384
    handler.min_speech_continuation_ms = handler.min_speech_ms
    handler.max_speech_ms = float("inf")
    handler.enable_realtime_transcription = False
    handler.realtime_processing_pause = 0.5
    handler.text_output_queue = Queue()
    handler.speculative_turns = SpeculativeTurnTracker()
    handler.speculative_reopen_ms = 800
    handler.unanswered_reopen_ms = 7000
    handler._last_turn_detection = None
    handler.smart_turn_analyzer = None
    handler.smart_turn_max_wait_ms = 2000
    handler.smart_turn_incomplete_delay_ms = 600
    handler.iterator = iterator
    handler.audio_enhancement = False
    handler._total_samples = 0
    handler._last_log_time = time.time()
    handler._log_chunks = 0
    handler._log_speech_starts = 0
    handler._log_speech_ends = 0
    handler._speech_started_emitted = False
    handler._turn_counter = 0
    handler._current_turn_id = None
    handler._current_turn_revision = None
    handler._speculative_audio_prefix = None
    handler._speculative_raw_audio_prefix = None
    handler._speculative_active_speech_ms = 0.0
    handler._last_final_wall_time = None
    handler._last_final_audio_ms = None
    handler._pending_reopen_candidate = None
    handler.short_segment_merge_ms = 0
    handler._pending_short_segment = None
    return handler


def _audio_bytes(samples: int = 512) -> bytes:
    return np.zeros(samples, dtype=np.int16).tobytes()


def _drain_text_events(handler: VADHandler) -> None:
    while not handler.text_output_queue.empty():
        handler.text_output_queue.get_nowait()


def test_vad_interruption_uses_active_speech_duration_not_padded_segment():
    chunks = [torch.zeros(512) for _ in range(20)]
    iterator = _StaticVADIterator(
        triggered=True,
        vad_output=None,
        buffer_chunks=chunks,
        speech_chunks=chunks,
        active_speech_samples=10 * 512,
    )
    handler = _vad_handler_for_iterator(iterator)
    handler.enable_realtime_transcription = True

    assert list(handler.process(_audio_bytes())) == []

    assert handler.text_output_queue.empty()
    assert handler._speech_started_emitted is False


def test_vad_pending_reopen_starts_before_active_speech_threshold():
    chunks = [torch.zeros(512) for _ in range(12)]
    iterator = _StaticVADIterator(
        triggered=True,
        vad_output=None,
        buffer_chunks=chunks,
        speech_chunks=chunks,
        active_speech_samples=8 * 512,
    )
    handler = _vad_handler_for_iterator(iterator)
    tracker = handler.speculative_turns
    tracker.observe("turn_1", 0)
    handler._current_turn_id = "turn_1"
    handler._current_turn_revision = 0
    handler._last_final_audio_ms = 0

    assert list(handler.process(_audio_bytes())) == []

    assert tracker.has_pending_reopen("turn_1", 0)
    tracker.commit("turn_1", 0)
    assert not tracker.is_committed("turn_1", 0)
    assert handler.text_output_queue.empty()
    assert handler._speech_started_emitted is False


def test_vad_processes_audio_when_should_listen_is_unset():
    """Full-duplex: the VAD gate no longer drops chunks while TTS plays."""
    chunks = [torch.zeros(512) for _ in range(20)]
    iterator = _StaticVADIterator(
        triggered=True,
        vad_output=None,
        buffer_chunks=chunks,
        speech_chunks=chunks,
        active_speech_samples=12 * 512,
    )
    handler = _vad_handler_for_iterator(iterator)
    handler.should_listen.clear()

    assert list(handler.process(_audio_bytes())) == []

    event = handler.text_output_queue.get_nowait()
    assert event.interrupt_response is True
    assert handler._speech_started_emitted is True


def test_vad_interruption_emits_after_active_speech_threshold():
    chunks = [torch.zeros(512) for _ in range(20)]
    iterator = _StaticVADIterator(
        triggered=True,
        vad_output=None,
        buffer_chunks=chunks,
        speech_chunks=chunks,
        active_speech_samples=12 * 512,
    )
    handler = _vad_handler_for_iterator(iterator)

    assert list(handler.process(_audio_bytes())) == []

    event = handler.text_output_queue.get_nowait()
    assert isinstance(event, SpeechStartedEvent)
    assert event.interrupt_response is True
    assert handler._speech_started_emitted is True


def test_vad_discards_final_segment_when_active_speech_is_short():
    final_chunks = [torch.zeros(512) for _ in range(31)]
    iterator = _StaticVADIterator(
        triggered=False,
        vad_output=final_chunks,
        last_utterance_active_speech_samples=11 * 512,
    )
    handler = _vad_handler_for_iterator(iterator)

    outputs = list(handler.process(_audio_bytes()))

    assert outputs == []
    assert handler.text_output_queue.empty()


def _drive_final_segment(handler: VADHandler, active_chunks: int = 12, segment_chunks: int = 31) -> list:
    handler.iterator = _StaticVADIterator(
        triggered=False,
        vad_output=[torch.zeros(512) for _ in range(segment_chunks)],
        last_utterance_active_speech_samples=active_chunks * 512,
    )
    return list(handler.process(_audio_bytes()))


def test_vad_complete_smart_turn_selects_shorter_speculative_grace():
    handler = _vad_handler_for_iterator(_StaticVADIterator(triggered=False, vad_output=None))
    handler.smart_turn_analyzer = _StaticSmartTurnAnalyzer(
        SmartTurnResult(complete=True, probability=0.98, inference_ms=12.5)
    )

    outputs = _drive_final_segment(handler)

    assert len(outputs) == 1
    assert outputs[0].processing_delay_s == 0.0
    assert outputs[0].active_speech_ms == pytest.approx(12 * 512 / 16000 * 1000)
    grace = handler.speculative_turns._reopen_grace["turn_1"]
    assert grace.revision == 0
    assert 0.6 < grace.deadline - time.monotonic() <= 0.8


def test_vad_incomplete_smart_turn_selects_longer_speculative_grace():
    handler = _vad_handler_for_iterator(_StaticVADIterator(triggered=False, vad_output=None))
    analyzer = _StaticSmartTurnAnalyzer(SmartTurnResult(complete=False, probability=0.2, inference_ms=12.5))
    handler.smart_turn_analyzer = analyzer

    outputs = _drive_final_segment(handler)

    assert len(outputs) == 1
    assert outputs[0].processing_delay_s == 0.6
    grace = handler.speculative_turns._reopen_grace["turn_1"]
    assert grace.revision == 0
    assert 1.8 < grace.deadline - time.monotonic() <= 2.0
    np.testing.assert_array_equal(analyzer.calls[0], outputs[0].audio)


def test_vad_incomplete_smart_turn_commits_after_longer_grace_without_resumed_speech():
    handler = _vad_handler_for_iterator(_StaticVADIterator(triggered=False, vad_output=None))
    handler.smart_turn_max_wait_ms = 50
    handler.smart_turn_analyzer = _StaticSmartTurnAnalyzer(
        SmartTurnResult(complete=False, probability=0.2, inference_ms=12.5)
    )

    outputs = _drive_final_segment(handler)

    assert len(outputs) == 1
    assert handler.speculative_turns.try_commit_if_latest_after_reopen_grace("turn_1", 0) is None
    time.sleep(0.06)
    assert handler.speculative_turns.try_commit_if_latest_after_reopen_grace("turn_1", 0) is True
    assert handler.speculative_turns.is_committed("turn_1", 0)


def test_vad_resumed_speech_during_smart_turn_grace_creates_new_revision():
    handler = _vad_handler_for_iterator(_StaticVADIterator(triggered=False, vad_output=None))
    analyzer = _StaticSmartTurnAnalyzer(
        SmartTurnResult(complete=False, probability=0.2, inference_ms=12.5),
        SmartTurnResult(complete=True, probability=0.9, inference_ms=12.5),
    )
    handler.smart_turn_analyzer = analyzer

    first_outputs = _drive_final_segment(handler)
    assert len(first_outputs) == 1
    assert (first_outputs[0].turn_id, first_outputs[0].turn_revision) == ("turn_1", 0)
    _drain_text_events(handler)

    # Resume after the normal 800ms grace but before Smart Turn's 2000ms
    # incomplete-turn grace has elapsed.
    handler._total_samples = int(1.5 * handler.sample_rate)
    resumed_outputs = _drive_final_segment(handler, active_chunks=12, segment_chunks=12)

    assert len(resumed_outputs) == 1
    assert (resumed_outputs[0].turn_id, resumed_outputs[0].turn_revision) == ("turn_1", 1)
    assert not handler.speculative_turns.is_latest("turn_1", 0)
    assert handler.speculative_turns.is_latest("turn_1", 1)
    assert len(analyzer.calls) == 2
    assert len(analyzer.calls[1]) == len(first_outputs[0].audio) + 12 * 512


def test_vad_max_speech_is_enforced_before_smart_turn():
    handler = _vad_handler_for_iterator(_StaticVADIterator(triggered=False, vad_output=None))
    analyzer = _StaticSmartTurnAnalyzer(SmartTurnResult(complete=False, probability=0.2, inference_ms=12.5))
    handler.smart_turn_analyzer = analyzer
    handler.max_speech_ms = 1500

    assert _drive_final_segment(handler, segment_chunks=63) == []
    assert analyzer.calls == []


def _handler_after_soft_ended_turn() -> VADHandler:
    handler = _vad_handler_for_iterator(_StaticVADIterator(triggered=False, vad_output=None))
    outputs = _drive_final_segment(handler, active_chunks=12, segment_chunks=12)
    assert len(outputs) == 1
    assert (outputs[0].turn_id, outputs[0].turn_revision) == ("turn_1", 0)
    _drain_text_events(handler)
    return handler


def test_soft_ended_direct_audio_turn_reopens_at_revision_one():
    # Keep the turn uncommitted to model generation still being in flight.
    handler = _handler_after_soft_ended_turn()
    handler.min_speech_continuation_ms = 192
    chunks = [torch.zeros(512) for _ in range(8)]
    handler.iterator = _StaticVADIterator(
        triggered=True,
        vad_output=None,
        buffer_chunks=chunks,
        speech_chunks=chunks,
        active_speech_samples=8 * 512,
    )

    assert list(handler.process(_audio_bytes())) == []

    started = handler.text_output_queue.get_nowait()
    assert isinstance(started, SpeechStartedEvent)
    assert (started.turn_id, started.turn_revision, started.reopened) == ("turn_1", 1, True)
    assert started.interrupt_response is False
    assert handler._speech_started_emitted is True


def test_trailing_continuation_fragment_accepted_at_finalization():
    handler = _handler_after_soft_ended_turn()
    handler.min_speech_continuation_ms = 192

    outputs = _drive_final_segment(handler, active_chunks=8, segment_chunks=8)

    assert len(outputs) == 1
    assert (outputs[0].turn_id, outputs[0].turn_revision) == ("turn_1", 1)
    assert outputs[0].active_speech_ms == pytest.approx(12 * 512 / 16000 * 1000 + 8 * 512 / 16000 * 1000)
    started = handler.text_output_queue.get_nowait()
    assert isinstance(started, SpeechStartedEvent)
    assert (started.turn_id, started.turn_revision, started.reopened) == ("turn_1", 1, True)


def test_vad_final_active_speech_accumulates_across_reopen_prefix():
    handler = _handler_after_soft_ended_turn()
    first_ms = 12 * 512 / 16000 * 1000
    assert handler._speculative_active_speech_ms == pytest.approx(first_ms)

    outputs = _drive_final_segment(handler, active_chunks=12, segment_chunks=12)

    assert len(outputs) == 1
    assert outputs[0].active_speech_ms == pytest.approx(first_ms * 2)
    assert handler._speculative_active_speech_ms == pytest.approx(first_ms * 2)


def test_continuation_bar_inactive_when_turn_committed():
    handler = _handler_after_soft_ended_turn()
    handler.min_speech_continuation_ms = 192
    tracker = handler.speculative_turns
    tracker.commit("turn_1", 0)

    outputs = _drive_final_segment(handler, active_chunks=8, segment_chunks=8)

    assert outputs == []
    assert handler.text_output_queue.empty()
    assert handler._current_turn_id == "turn_1"
    assert handler._current_turn_revision == 0
    assert tracker.is_committed("turn_1", 0)


def test_entry_bar_unchanged_for_new_speech():
    handler = _vad_handler_for_iterator(_StaticVADIterator(triggered=False, vad_output=None))
    handler.min_speech_continuation_ms = 192

    outputs = _drive_final_segment(handler, active_chunks=8, segment_chunks=8)

    assert outputs == []
    assert handler.text_output_queue.empty()
    assert handler._current_turn_id is None
    assert handler._turn_counter == 0


def test_confirmed_segment_not_discarded_at_finalization():
    handler = _handler_after_soft_ended_turn()
    handler.min_speech_continuation_ms = 192
    chunks = [torch.zeros(512) for _ in range(8)]
    handler.iterator = _StaticVADIterator(
        triggered=True,
        vad_output=None,
        buffer_chunks=chunks,
        speech_chunks=chunks,
        active_speech_samples=8 * 512,
    )

    assert list(handler.process(_audio_bytes())) == []
    started = handler.text_output_queue.get_nowait()
    assert isinstance(started, SpeechStartedEvent)
    assert (started.turn_id, started.turn_revision, started.reopened) == ("turn_1", 1, True)

    handler.iterator = _StaticVADIterator(
        triggered=False,
        vad_output=chunks,
        last_utterance_active_speech_samples=8 * 512,
    )
    outputs = list(handler.process(_audio_bytes()))

    assert len(outputs) == 1
    assert (outputs[0].turn_id, outputs[0].turn_revision) == ("turn_1", 1)


def test_continuation_threshold_clamping():
    assert VADHandler._resolve_min_speech_continuation_ms(384, 0) == 384
    assert VADHandler._resolve_min_speech_continuation_ms(384, 50) == 100
    assert VADHandler._resolve_min_speech_continuation_ms(384, 500) == 384
    assert VADHandler._resolve_min_speech_continuation_ms(384, 192) == 192


def test_vad_reopens_unanswered_turn_after_grace_window():
    handler = _vad_handler_for_iterator(_StaticVADIterator(triggered=False, vad_output=None))
    handler.unanswered_reopen_ms = 8000
    tracker = handler.speculative_turns

    outputs = _drive_final_segment(handler)
    assert len(outputs) == 1
    assert (outputs[0].turn_id, outputs[0].turn_revision) == ("turn_1", 0)
    assert handler._last_final_audio_ms is not None
    while not handler.text_output_queue.empty():
        handler.text_output_queue.get_nowait()

    # Advance the audio clock so the resumed speech starts well past
    # speculative_reopen_ms (800) but within unanswered_reopen_ms (8000).
    handler._total_samples = 16000 * 3

    outputs = _drive_final_segment(handler)

    assert len(outputs) == 1
    assert (outputs[0].turn_id, outputs[0].turn_revision) == ("turn_1", 1)
    started = handler.text_output_queue.get_nowait()
    assert isinstance(started, SpeechStartedEvent)
    assert (started.turn_id, started.turn_revision, started.reopened) == ("turn_1", 1, True)
    assert not tracker.is_latest("turn_1", 0)


def test_vad_does_not_reopen_committed_turn():
    handler = _vad_handler_for_iterator(_StaticVADIterator(triggered=False, vad_output=None))
    handler.unanswered_reopen_ms = 8000
    tracker = handler.speculative_turns

    outputs = _drive_final_segment(handler)
    assert len(outputs) == 1
    assert (outputs[0].turn_id, outputs[0].turn_revision) == ("turn_1", 0)
    while not handler.text_output_queue.empty():
        handler.text_output_queue.get_nowait()

    tracker.commit("turn_1", 0)
    handler._total_samples = 16000 * 3

    outputs = _drive_final_segment(handler)

    assert len(outputs) == 1
    assert (outputs[0].turn_id, outputs[0].turn_revision) == ("turn_2", 0)
    started = handler.text_output_queue.get_nowait()
    assert isinstance(started, SpeechStartedEvent)
    assert started.reopened is False


def test_vad_new_turn_after_unanswered_cap():
    handler = _vad_handler_for_iterator(_StaticVADIterator(triggered=False, vad_output=None))
    handler.unanswered_reopen_ms = 8000

    outputs = _drive_final_segment(handler)
    assert len(outputs) == 1
    assert (outputs[0].turn_id, outputs[0].turn_revision) == ("turn_1", 0)
    while not handler.text_output_queue.empty():
        handler.text_output_queue.get_nowait()

    # Advance the audio clock so the resumed speech starts past the cap.
    handler._total_samples = 16000 * 12

    outputs = _drive_final_segment(handler)

    assert len(outputs) == 1
    assert (outputs[0].turn_id, outputs[0].turn_revision) == ("turn_2", 0)
    started = handler.text_output_queue.get_nowait()
    assert isinstance(started, SpeechStartedEvent)
    assert started.reopened is False


def test_vad_does_not_hold_sub_floor_fragments():
    handler = _vad_handler_for_iterator(
        _StaticVADIterator(
            triggered=False,
            vad_output=[torch.zeros(512)],
            last_utterance_active_speech_samples=512,
        )
    )
    handler.short_segment_merge_ms = 384

    outputs = list(handler.process(_audio_bytes()))

    assert outputs == []
    assert handler._pending_short_segment is None
    assert handler.text_output_queue.empty()


def test_vad_idle_greeting_below_barge_in_bar_is_still_a_turn():
    # Live logs: ~544ms Silero-active inside a padded ~2.4s segment, discarded
    # because min_speech_ms=600. That is a real idle "hello", not barge-in echo.
    active_samples = 17 * 512  # 544ms at 16kHz
    handler = _vad_handler_for_iterator(
        _StaticVADIterator(
            triggered=False,
            vad_output=[torch.zeros(512) for _ in range(75)],
            last_utterance_active_speech_samples=active_samples,
        )
    )
    handler.min_speech_ms = 600
    handler.min_speech_continuation_ms = 192
    handler.short_segment_merge_ms = 400
    handler.response_playing = Event()

    outputs = list(handler.process(_audio_bytes()))

    assert len(outputs) == 1
    started = handler.text_output_queue.get_nowait()
    assert isinstance(started, SpeechStartedEvent)
    assert handler._pending_short_segment is None


def test_vad_idle_448ms_greeting_is_still_a_turn():
    # Live log 2026-09-09 19:24: segment=2516ms active=448ms discarded
    # because the idle floor had been raised to 480ms.
    active_samples = 14 * 512  # 448ms at 16kHz
    handler = _vad_handler_for_iterator(
        _StaticVADIterator(
            triggered=False,
            vad_output=[torch.zeros(512) for _ in range(75)],
            last_utterance_active_speech_samples=active_samples,
        )
    )
    handler.min_speech_ms = 600
    handler.min_speech_continuation_ms = 192
    handler.short_segment_merge_ms = 400
    handler.response_playing = Event()

    outputs = list(handler.process(_audio_bytes()))

    assert len(outputs) == 1
    started = handler.text_output_queue.get_nowait()
    assert isinstance(started, SpeechStartedEvent)
    assert handler._pending_short_segment is None


def test_vad_idle_tv_burst_below_idle_floor_is_not_a_turn():
    # Live log 2026-09-09: Speech started active=288ms, min=280ms from TV
    # in another room. The idle floor is 400ms.
    active_samples = 9 * 512  # 288ms at 16kHz
    handler = _vad_handler_for_iterator(
        _StaticVADIterator(
            triggered=False,
            vad_output=[torch.zeros(512) for _ in range(75)],
            last_utterance_active_speech_samples=active_samples,
        )
    )
    handler.min_speech_ms = 600
    handler.min_speech_continuation_ms = 192
    handler.short_segment_merge_ms = 400
    handler.response_playing = Event()

    outputs = list(handler.process(_audio_bytes()))

    assert outputs == []
    assert handler.text_output_queue.empty()


def test_vad_keeps_barge_in_bar_while_assistant_is_talking():
    active_samples = 17 * 512
    handler = _vad_handler_for_iterator(
        _StaticVADIterator(
            triggered=False,
            vad_output=[torch.zeros(512) for _ in range(75)],
            last_utterance_active_speech_samples=active_samples,
        )
    )
    handler.min_speech_ms = 600
    handler.min_speech_continuation_ms = 192
    handler.short_segment_merge_ms = 400
    handler.response_playing = Event()
    handler.response_playing.set()

    outputs = list(handler.process(_audio_bytes()))

    assert outputs == []
    assert handler._pending_short_segment is not None
    assert handler.text_output_queue.empty()


def test_vad_stitches_adjacent_short_segments_before_discarding():
    first_chunks = [torch.zeros(512) for _ in range(7)]
    second_chunks = [torch.zeros(512) for _ in range(8)]
    handler = _vad_handler_for_iterator(
        _StaticVADIterator(
            triggered=False,
            vad_output=first_chunks,
            last_utterance_active_speech_samples=4 * 512,
        )
    )
    handler.short_segment_merge_ms = 384

    assert list(handler.process(_audio_bytes())) == []
    assert handler.text_output_queue.empty()
    assert handler._pending_short_segment is not None

    handler.iterator = _StaticVADIterator(
        triggered=False,
        vad_output=second_chunks,
        last_utterance_active_speech_samples=8 * 512,
    )
    outputs = list(handler.process(_audio_bytes()))

    assert len(outputs) == 1
    assert len(outputs[0].audio) == 15 * 512
    started = handler.text_output_queue.get_nowait()
    stopped = handler.text_output_queue.get_nowait()
    assert isinstance(started, SpeechStartedEvent)
    assert started.interrupt_response is True
    assert isinstance(stopped, SpeechStoppedEvent)
    assert handler._pending_short_segment is None


def test_vad_pending_short_segment_contributes_to_early_speech_start():
    first_chunks = [torch.zeros(512) for _ in range(7)]
    current_chunks = [torch.zeros(512) for _ in range(8)]
    handler = _vad_handler_for_iterator(
        _StaticVADIterator(
            triggered=False,
            vad_output=first_chunks,
            last_utterance_active_speech_samples=4 * 512,
        )
    )
    handler.short_segment_merge_ms = 384

    assert list(handler.process(_audio_bytes())) == []

    handler.iterator = _StaticVADIterator(
        triggered=True,
        vad_output=None,
        buffer_chunks=current_chunks,
        speech_chunks=current_chunks,
        active_speech_samples=8 * 512,
    )

    assert list(handler.process(_audio_bytes())) == []
    event = handler.text_output_queue.get_nowait()
    assert isinstance(event, SpeechStartedEvent)
    assert event.interrupt_response is True
    assert handler._speech_started_emitted is True


def test_vad_pending_short_segment_does_not_start_on_sub_floor_current_fragment():
    first_chunks = [torch.zeros(512) for _ in range(10)]
    current_chunks = [torch.zeros(512) for _ in range(3)]
    handler = _vad_handler_for_iterator(
        _StaticVADIterator(
            triggered=False,
            vad_output=first_chunks,
            last_utterance_active_speech_samples=9 * 512,
        )
    )
    handler.short_segment_merge_ms = 384

    assert list(handler.process(_audio_bytes())) == []
    assert handler._pending_short_segment is not None

    # Pending holds 288ms active speech; the live fragment has only 96ms,
    # below the noise floor, so the combined 384ms must not start speech.
    handler.iterator = _StaticVADIterator(
        triggered=True,
        vad_output=None,
        buffer_chunks=current_chunks,
        speech_chunks=current_chunks,
        active_speech_samples=3 * 512,
    )

    assert list(handler.process(_audio_bytes())) == []
    assert handler.text_output_queue.empty()
    assert handler._speech_started_emitted is False


def test_vad_stitching_preserves_silence_gap_between_segments():
    first_chunks = [torch.zeros(512) for _ in range(7)]
    second_chunks = [torch.zeros(512) for _ in range(8)]
    handler = _vad_handler_for_iterator(
        _StaticVADIterator(
            triggered=False,
            vad_output=first_chunks,
            last_utterance_active_speech_samples=4 * 512,
        )
    )
    handler.short_segment_merge_ms = 384

    assert list(handler.process(_audio_bytes())) == []
    assert handler._pending_short_segment is not None
    assert handler._pending_short_segment.end_ms == 32

    # Advance the audio clock so the second segment starts 32ms after the
    # pending one ends; the stitched audio must include that silent gap.
    handler._total_samples = 9 * 512
    handler.iterator = _StaticVADIterator(
        triggered=False,
        vad_output=second_chunks,
        last_utterance_active_speech_samples=8 * 512,
    )
    outputs = list(handler.process(_audio_bytes()))

    assert len(outputs) == 1
    assert len(outputs[0].audio) == 16 * 512


def test_vad_final_synthetic_start_does_not_interrupt_response():
    final_chunks = [torch.zeros(512) for _ in range(31)]
    iterator = _StaticVADIterator(
        triggered=False,
        vad_output=final_chunks,
        last_utterance_active_speech_samples=12 * 512,
    )
    handler = _vad_handler_for_iterator(iterator)

    outputs = list(handler.process(_audio_bytes()))

    assert len(outputs) == 1
    started = handler.text_output_queue.get_nowait()
    stopped = handler.text_output_queue.get_nowait()
    assert isinstance(started, SpeechStartedEvent)
    assert started.interrupt_response is True
    assert isinstance(stopped, SpeechStoppedEvent)


def test_vad_keeps_single_speculative_audio_prefix():
    handler = object.__new__(VADHandler)
    handler._speculative_audio_prefix = None
    first_segment = np.array([1.0, 2.0], dtype=np.float32)
    second_segment = np.array([3.0], dtype=np.float32)
    third_segment = np.array([4.0], dtype=np.float32)

    first_output = handler._combined_turn_audio(first_segment)
    handler._speculative_audio_prefix = first_output
    second_output = handler._combined_turn_audio(second_segment)
    handler._speculative_audio_prefix = second_output
    third_output = handler._combined_turn_audio(third_segment)

    assert first_output is first_segment
    np.testing.assert_array_equal(second_output, np.array([1.0, 2.0, 3.0], dtype=np.float32))
    np.testing.assert_array_equal(third_output, np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32))


def _vad_audio(turn_id: str = "turn_1", revision: int = 0) -> VADAudio:
    return VADAudio(audio=np.zeros(512, dtype=np.float32), turn_id=turn_id, turn_revision=revision)


def test_vad_drops_stale_revisions_from_output_queue():
    tracker = SpeculativeTurnTracker()
    tracker.observe("turn_1", 1)
    handler = object.__new__(VADHandler)
    handler.queue_out = Queue()
    handler.speculative_turns = tracker
    stale = _vad_audio(revision=0)
    current = _vad_audio(revision=1)
    handler.queue_out.put(stale)
    handler.queue_out.put(current)

    handler.before_emit_output(_vad_audio(revision=1))

    # Only the superseded revision goes; the live one is still wanted.
    assert list(handler.queue_out.queue) == [current]


def test_vad_keeps_queued_audio_for_a_different_live_turn():
    handler = object.__new__(VADHandler)
    handler.queue_out = Queue()
    handler.speculative_turns = SpeculativeTurnTracker()
    handler.speculative_turns.observe("turn_1", 0)
    handler.speculative_turns.observe("turn_2", 0)
    other_turn = _vad_audio(turn_id="turn_2")
    handler.queue_out.put(other_turn)

    handler.before_emit_output(_vad_audio())

    assert list(handler.queue_out.queue) == [other_turn]


# ── Noise-gate regression tests ──────────────────────────────────────────
# Live session 2026-09-12 (turn_11): the user said "Well, yeah, there is a lot
# of noise there." The turn soft-ended, Smart Turn scored it complete
# (p=0.982), and ~500ms later a 192ms noise burst reopened it as rev=1. The
# whole 6.0s turn was re-transcribed with an appended "Uh." (reopened Smart
# Turn p=0.005) and the assistant answered the corrupted transcript after a
# 2s incomplete-delay. These tests pin the fixed behavior: trailing audio
# after a *complete* turn must qualify as a new utterance instead of riding
# the weak continuation hysteresis, and a confident-incomplete reopen must
# not supersede a confident-complete turn.


def _noise_gate_handler(*smart_turn_results: SmartTurnResult) -> VADHandler:
    """VAD handler mirroring the live session knobs: fresh turns need 400ms
    of active speech, continuation hysteresis needs 192ms."""
    handler = _vad_handler_for_iterator(_StaticVADIterator(triggered=False, vad_output=None))
    handler.min_speech_ms = 400
    handler.min_speech_continuation_ms = 192
    handler.smart_turn_analyzer = _StaticSmartTurnAnalyzer(*smart_turn_results)
    handler.reopen_complete_window_ms = -1
    handler.reopen_complete_min_speech_ms = 0
    handler.reopen_require_complete = True
    return handler


def _complete_turn(p: float = 0.982) -> SmartTurnResult:
    return SmartTurnResult(complete=True, probability=p, inference_ms=12.5)


def _incomplete_turn(p: float = 0.005) -> SmartTurnResult:
    return SmartTurnResult(complete=False, probability=p, inference_ms=12.5)


def _live_speech(handler: VADHandler, active_chunks: int) -> list:
    chunks = [torch.zeros(512) for _ in range(active_chunks)]
    handler.iterator = _StaticVADIterator(
        triggered=True,
        vad_output=None,
        buffer_chunks=chunks,
        speech_chunks=chunks,
        active_speech_samples=active_chunks * 512,
    )
    return list(handler.process(_audio_bytes()))


def test_completed_turn_is_not_reopened_by_short_noise():
    """turn_11 live path: a 192ms noise burst after a Smart-Turn-complete turn
    must neither start a reopened turn nor finalize into one. The 192ms
    hysteresis only bridges pauses inside an *unfinished* utterance; trailing
    audio after a finished one must clear the fresh-turn bar (400ms)."""
    handler = _noise_gate_handler(_complete_turn(), _incomplete_turn())

    first = _drive_final_segment(handler, active_chunks=13, segment_chunks=31)
    assert len(first) == 1
    assert (first[0].turn_id, first[0].turn_revision) == ("turn_1", 0)
    _drain_text_events(handler)

    # Live 192ms burst: must not start a reopened turn.
    assert _live_speech(handler, active_chunks=6) == []
    assert handler.text_output_queue.empty()

    # Finalized 192ms fragment: must be discarded, not emitted as rev=1.
    revived = _drive_final_segment(handler, active_chunks=6, segment_chunks=6)
    assert revived == []
    assert handler.text_output_queue.empty()
    assert (handler._current_turn_id, handler._current_turn_revision) == ("turn_1", 0)
    assert handler.speculative_turns.is_latest("turn_1", 0)
    assert handler.speculative_turns._pending_reopen == {}
    # The discarded noise never reaches Smart Turn: no wasted inference.
    assert len(handler.smart_turn_analyzer.calls) == 1


def test_completed_turn_low_confidence_reopen_is_not_retranscribed():
    """turn_11 rev=1 soft-end: 704ms of active noise clears any duration bar,
    but Smart Turn scores the reopened audio p=0.005. A confident-incomplete
    reopen must not supersede a confident-complete turn, so no VADAudio is
    emitted: STT never re-transcribes the 6s turn and the committed transcript
    is not corrupted with an appended filler."""
    handler = _noise_gate_handler(_complete_turn(), _incomplete_turn())

    first = _drive_final_segment(handler, active_chunks=13, segment_chunks=31)
    assert (first[0].turn_id, first[0].turn_revision) == ("turn_1", 0)
    _drain_text_events(handler)

    revived = _drive_final_segment(handler, active_chunks=22, segment_chunks=71)
    assert revived == []
    started = handler.text_output_queue.get_nowait()
    assert isinstance(started, SpeechStartedEvent)
    assert (started.turn_id, started.turn_revision, started.reopened) == ("turn_1", 1, True)
    # The suppressed audio is retained in the turn prefix so a genuine
    # continuation that completes later still carries its full audio.
    assert len(handler._speculative_audio_prefix) == (31 + 71) * 512


def test_sustained_noise_after_suppressed_reopen_stays_suppressed():
    """One suppressed noise blip must not launder the next one: after rev=1
    is suppressed, the turn still has its rev=0 complete endpoint, so a
    second 192ms blip faces the fresh-turn bar (not the hysteresis) and a
    second sustained-but-incomplete blip is suppressed again, not emitted."""
    handler = _noise_gate_handler(_complete_turn(), _incomplete_turn(), _incomplete_turn())

    first = _drive_final_segment(handler, active_chunks=13, segment_chunks=31)
    assert (first[0].turn_id, first[0].turn_revision) == ("turn_1", 0)
    _drain_text_events(handler)

    assert _drive_final_segment(handler, active_chunks=22, segment_chunks=71) == []
    _drain_text_events(handler)
    assert (handler._current_turn_id, handler._current_turn_revision) == ("turn_1", 1)

    # Second blip, short: must not even start a turn, let alone emit one.
    assert _live_speech(handler, active_chunks=6) == []
    assert handler.text_output_queue.empty()
    assert _drive_final_segment(handler, active_chunks=6, segment_chunks=6) == []
    assert handler.text_output_queue.empty()
    assert (handler._current_turn_id, handler._current_turn_revision) == ("turn_1", 1)
    assert handler.speculative_turns._pending_reopen == {}
    assert len(handler.smart_turn_analyzer.calls) == 2


def test_completed_turn_reopens_for_genuine_continuation():
    """The noise gate must not break real speakers: sustained resumed speech
    that scores Smart-Turn-complete still reopens the turn and is emitted."""
    handler = _noise_gate_handler(_complete_turn(), _complete_turn(p=0.9))

    first = _drive_final_segment(handler, active_chunks=13, segment_chunks=31)
    assert (first[0].turn_id, first[0].turn_revision) == ("turn_1", 0)
    _drain_text_events(handler)

    revived = _drive_final_segment(handler, active_chunks=22, segment_chunks=71)
    assert len(revived) == 1
    assert (revived[0].turn_id, revived[0].turn_revision) == ("turn_1", 1)


def test_incomplete_turn_keeps_continuation_hysteresis():
    """Mid-thought pauses are why hysteresis exists: when Smart Turn says the
    base turn is incomplete, a 192ms continuation still reopens it."""
    handler = _noise_gate_handler(_incomplete_turn(p=0.2), _complete_turn(p=0.9))

    first = _drive_final_segment(handler, active_chunks=13, segment_chunks=31)
    assert (first[0].turn_id, first[0].turn_revision) == ("turn_1", 0)
    _drain_text_events(handler)

    assert _live_speech(handler, active_chunks=6) == []
    started = handler.text_output_queue.get_nowait()
    assert isinstance(started, SpeechStartedEvent)
    assert (started.turn_id, started.turn_revision, started.reopened) == ("turn_1", 1, True)


def test_completed_turn_closes_reopen_window_after_speculative_grace():
    """turn_8: 'Ah, yeah.' (complete) was re-transcribed three times by noise
    arriving 5-15s later. A complete turn stops being reopenable after the
    short speculative grace; later audio is a new turn instead of a revision."""
    handler = _noise_gate_handler(_complete_turn(), _complete_turn())

    first = _drive_final_segment(handler, active_chunks=13, segment_chunks=31)
    assert (first[0].turn_id, first[0].turn_revision) == ("turn_1", 0)
    _drain_text_events(handler)

    # 6s later: past the 800ms speculative grace but inside the 7s unanswered cap.
    # (The 71-chunk segment itself spans ~2.3s, so the gap must exceed that for
    # the segment *start* to land outside the grace.)
    handler._total_samples = 16000 * 6
    second = _drive_final_segment(handler, active_chunks=22, segment_chunks=71)
    assert len(second) == 1
    assert (second[0].turn_id, second[0].turn_revision) == ("turn_2", 0)
    started = handler.text_output_queue.get_nowait()
    assert isinstance(started, SpeechStartedEvent)
    assert started.reopened is False


def test_incomplete_turn_keeps_unanswered_reopen_window():
    """Companion to the window test above: an incomplete turn still reopens
    inside the long unanswered cap, so slow answerers do not orphan pauses."""
    handler = _noise_gate_handler(_incomplete_turn(p=0.2), _complete_turn(p=0.9))

    first = _drive_final_segment(handler, active_chunks=13, segment_chunks=31)
    assert (first[0].turn_id, first[0].turn_revision) == ("turn_1", 0)
    _drain_text_events(handler)

    handler._total_samples = 16000 * 6
    revived = _drive_final_segment(handler, active_chunks=22, segment_chunks=71)
    assert len(revived) == 1
    assert (revived[0].turn_id, revived[0].turn_revision) == ("turn_1", 1)


def test_reopen_require_complete_false_restores_legacy_emission():
    """Escape hatch: with reopen_require_complete=False a low-confidence
    reopen is emitted exactly as before the noise gate, for rooms that prefer
    speculative churn to suppression."""
    handler = _noise_gate_handler(_complete_turn(), _incomplete_turn())
    handler.reopen_require_complete = False

    first = _drive_final_segment(handler, active_chunks=13, segment_chunks=31)
    assert (first[0].turn_id, first[0].turn_revision) == ("turn_1", 0)
    _drain_text_events(handler)

    revived = _drive_final_segment(handler, active_chunks=22, segment_chunks=71)
    assert len(revived) == 1
    assert (revived[0].turn_id, revived[0].turn_revision) == ("turn_1", 1)


def test_reopen_complete_window_override_keeps_late_reopen():
    """Escape hatch: an explicit reopen_complete_window_ms wider than the
    speculative grace keeps late post-complete reopens for users whose
    afterthoughts arrive seconds later."""
    handler = _noise_gate_handler(_complete_turn(), _complete_turn(p=0.9))
    handler.reopen_complete_window_ms = 5000

    first = _drive_final_segment(handler, active_chunks=13, segment_chunks=31)
    assert (first[0].turn_id, first[0].turn_revision) == ("turn_1", 0)
    _drain_text_events(handler)

    handler._total_samples = 16000 * 6
    revived = _drive_final_segment(handler, active_chunks=22, segment_chunks=71)
    assert len(revived) == 1
    assert (revived[0].turn_id, revived[0].turn_revision) == ("turn_1", 1)


def test_reopen_complete_floor_override_blocks_medium_noise():
    """Escape hatch in the other direction: reopen_complete_min_speech_ms
    replaces the fresh-turn bar for post-complete reopens, so a noisy room
    can demand more than 400ms before a finished turn is touched."""
    handler = _noise_gate_handler(_complete_turn(), _incomplete_turn())
    handler.reopen_complete_min_speech_ms = 600

    first = _drive_final_segment(handler, active_chunks=13, segment_chunks=31)
    assert (first[0].turn_id, first[0].turn_revision) == ("turn_1", 0)
    _drain_text_events(handler)

    # 448ms of active noise clears the default 400ms fresh bar but not the
    # 600ms override.
    revived = _drive_final_segment(handler, active_chunks=14, segment_chunks=45)
    assert revived == []
    assert handler.text_output_queue.empty()


def test_reopen_noise_gate_setup_validation(monkeypatch):
    """Out-of-range noise-gate knobs fail at setup, like the Smart Turn timings."""

    class FakeSileroModel:
        def reset_states(self) -> None:
            pass

    monkeypatch.setattr(torch.hub, "load", lambda *_args, **_kwargs: (FakeSileroModel(), None))

    with pytest.raises(ValueError):
        object.__new__(VADHandler).setup(
            Event(),
            speculative_turns=SpeculativeTurnTracker(),
            smart_turn=False,
            reopen_complete_window_ms=-2,
        )
    with pytest.raises(ValueError):
        object.__new__(VADHandler).setup(
            Event(),
            speculative_turns=SpeculativeTurnTracker(),
            smart_turn=False,
            reopen_complete_min_speech_ms=-1,
        )
