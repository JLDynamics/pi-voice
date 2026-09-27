"""Usage counters for the realtime voice service."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, Field

_UsageMetricsT = TypeVar("_UsageMetricsT", bound="UsageMetrics")


class UsageMetrics(BaseModel):
    """Per-response usage counters.

    Supports ``+=`` for rolling per-response metrics into a global total
    and ``reset()`` for clearing per-response state after rollup.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    audio_duration_s: float = 0.0
    responses_completed: int = 0
    responses_cancelled: int = 0
    tool_calls: int = 0
    turns: int = 0

    def __iadd__(self: _UsageMetricsT, other: "UsageMetrics") -> _UsageMetricsT:
        for field in UsageMetrics.model_fields:
            setattr(self, field, getattr(self, field) + getattr(other, field))
        return self

    def reset(self) -> None:
        for field, info in UsageMetrics.model_fields.items():
            setattr(self, field, info.default)


class GlobalUsageMetrics(UsageMetrics):
    """Server-wide metrics that extend per-response counters with
    connection and error tracking."""

    connections: int = 0
    # connection duration in seconds.
    # latency tts, llm, vad, stt (mean, max, p90)
    errors_by_type: dict[str, int] = Field(default_factory=dict)

    def record_error(self, error_type: str) -> None:
        self.errors_by_type[error_type] = self.errors_by_type.get(error_type, 0) + 1

    @property
    def total_errors(self) -> int:
        return sum(self.errors_by_type.values())
