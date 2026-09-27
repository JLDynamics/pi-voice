"""Admitted turns are the only interruption authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TurnAdmission:
    admission_id: str
    interrupt_output: bool


class TurnAdmissionSink(Protocol):
    def on_turn_admitted(self, admission: TurnAdmission) -> None:
        """Retire stale output. Duplicate admission ids are no-ops."""
