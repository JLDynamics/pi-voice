"""Shared TTS turn helpers."""

from __future__ import annotations

from collections import deque
from typing import Any

from chatbot.pipeline.messages import TTSInput


def drop_queued_tts_inputs(queue_in: Any, turn_id: str | None, turn_revision: int | None) -> int:
    """Remove queued ``TTSInput``s for the failed turn so they cannot restart it."""
    if not hasattr(queue_in, "mutex") or not hasattr(queue_in, "queue"):
        return 0
    dropped = 0
    with queue_in.mutex:
        kept: deque[Any] = deque()
        for item in queue_in.queue:
            if isinstance(item, TTSInput) and (item.turn_id, item.turn_revision) == (turn_id, turn_revision):
                dropped += 1
                continue
            kept.append(item)
        queue_in.queue = kept
    return dropped


def strip_denoise_kwargs(gen_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop the retired denoise flags so backends never see them.

    Generated speech used to pass through a spectral denoiser and a noise gate
    on the way to the speakers. Upstream has neither, Siri's output does not
    need cleaning, and both were shaping audio that was already clean. The
    flags are still accepted and ignored so an older launch script or a saved
    session config does not fail on them.
    """
    gen_kwargs = dict(gen_kwargs)
    for retired in (
        "spectral_denoise",
        "spectral_denoise_floor",
        "noise_gate",
        "noise_gate_threshold",
    ):
        gen_kwargs.pop(retired, None)
    return gen_kwargs
