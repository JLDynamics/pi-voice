import os
from typing import Literal

from openai.types.realtime import RealtimeSessionCreateRequest
from openai.types.realtime.realtime_audio_config import RealtimeAudioConfig
from openai.types.realtime.realtime_audio_config_input import RealtimeAudioConfigInput
from openai.types.realtime.realtime_audio_config_output import RealtimeAudioConfigOutput
from pydantic import BaseModel, ConfigDict, Field, field_validator

from chatbot.LLM.chat import Chat

Thinker = Literal["luna", "pi"]


def parse_thinker(value: str | None) -> Thinker:
    if value is None or value == "":
        return "luna"
    # Compared one at a time so the narrowing reaches Thinker; `value in
    # ("luna", "pi")` leaves mypy holding a plain str.
    if value == "luna":
        return "luna"
    if value == "pi":
        return "pi"
    raise ValueError(f"VOICE_THINKER must be 'luna' or 'pi', got {value!r}")


def _thinker_from_env() -> Thinker:
    return parse_thinker(os.environ.get("VOICE_THINKER"))


def _apply_update(current: BaseModel, update: BaseModel) -> None:
    """Apply explicitly-set fields from *update* onto *current* in-place,
    recursing into nested BaseModel children so partial nested updates
    don't overwrite unset fields.

    Only fields present in update.model_fields_set (i.e. actually
    sent by the client) are considered.
    """
    for field_name in update.model_fields_set:
        new_val = getattr(update, field_name)
        old_val = getattr(current, field_name, None)
        if isinstance(new_val, BaseModel) and isinstance(old_val, BaseModel):
            _apply_update(old_val, new_val)
        else:
            setattr(current, field_name, new_val)


class RuntimeConfig(BaseModel):
    """
    Shared mutable configuration written by the RealtimeService on
    session.update and read by pipeline handlers (VAD, LLM, TTS) during
    processing.  Python's GIL makes simple attribute reads/writes atomic,
    so no explicit locking is needed for primitive values.

    The canonical state lives in 'session' (a full
    'RealtimeSessionCreateRequest').
    """

    model_config = ConfigDict(validate_assignment=True, arbitrary_types_allowed=True)

    chat: Chat = Field(default_factory=lambda: Chat(10))
    session: RealtimeSessionCreateRequest = Field(
        default_factory=lambda: RealtimeSessionCreateRequest(type="realtime"),
        validate_default=True,
    )
    thinker: Thinker = Field(default_factory=_thinker_from_env)

    @property
    def allows_think(self) -> bool:
        """Whether this session may enqueue an LLM think request."""
        return self.thinker == "luna"

    @field_validator("session", mode="after")
    @classmethod
    def _ensure_audio_structure(cls, v: RealtimeSessionCreateRequest) -> RealtimeSessionCreateRequest:
        """Guarantee 'audio.input' and 'audio.output' are never None."""
        if v.audio is None:
            v.audio = RealtimeAudioConfig()
        if v.audio.input is None:
            v.audio.input = RealtimeAudioConfigInput()
        if v.audio.output is None:
            v.audio.output = RealtimeAudioConfigOutput()
        return v

    @field_validator("thinker", mode="before")
    @classmethod
    def _parse_thinker(cls, value: object) -> Thinker:
        if value is None:
            return _thinker_from_env()
        if not isinstance(value, str):
            raise TypeError(f"thinker must be a string, got {type(value).__name__}")
        return parse_thinker(value)

    @property
    def interrupt_response_enabled(self) -> bool:
        """Whether barge-in should cancel an active response.

        Reads 'turn_detection.interrupt_response' from the session config,
        handling both Pydantic models ('ServerVad') and plain dicts.
        Defaults to 'True' (OpenAI API default).
        """
        assert self.session.audio is not None and self.session.audio.input is not None
        td = self.session.audio.input.turn_detection
        if td is None:
            return True
        if hasattr(td, "interrupt_response"):
            val = td.interrupt_response
        elif isinstance(td, dict):
            val = td.get("interrupt_response", True)
        else:
            return True
        return val if val is not None else True

    def apply_session_update(self, update: RealtimeSessionCreateRequest) -> None:
        """Merge non-None, explicitly-set fields from 'update' into the
        current 'session', preserving any fields not present in the update."""
        _apply_update(self.session, update)
