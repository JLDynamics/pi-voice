"""Single-writer barge-in flush for typed turn admissions."""

from __future__ import annotations

from chatbot.api.openai_realtime.pipeline_unit import PipelineUnit
from chatbot.api.openai_realtime.queue_flush import (
    flush_queue,
    keep_audio_sentinel,
    keep_session_end,
    keep_user_text_event,
)
from chatbot.pipeline.turn_admission import TurnAdmission

_RECENT_IDS = 32


def flush_turn_queues(unit: PipelineUnit) -> None:
    """Drop stale turn work while keeping drain sentinels and user events."""
    flush_queue(unit.output_queue, preserve=keep_audio_sentinel)
    flush_queue(unit.text_output_queue, preserve=keep_user_text_event)
    flush_queue(unit.text_prompt_queue, preserve=keep_session_end)


class PipelineTurnInterrupter:
    """Flush stale pipeline work after a typed admission (VAD onset or admitted transcript)."""

    def __init__(self, unit: PipelineUnit) -> None:
        self._unit = unit
        self._recent: list[str] = []

    def on_turn_admitted(self, admission: TurnAdmission) -> None:
        if admission.admission_id in self._recent:
            return
        self._recent.append(admission.admission_id)
        if len(self._recent) > _RECENT_IDS:
            self._recent = self._recent[-_RECENT_IDS:]
        if not admission.interrupt_output:
            return
        unit = self._unit
        session = unit.session
        if session is not None and session.transport is not None:
            session.transport.discard_pending_audio()
        unit.cancel_scope.cancel()
        if session is not None and session.session_id:
            unit.service._state(session.session_id).response_pending = False
        flush_turn_queues(unit)
        if unit.response_playing.is_set():
            unit.response_playing.clear()
