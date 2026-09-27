"""Speech-to-text on the Mac itself, through Apple's on-device engine.

Replaces a hosted transcription path with on-device speech recognition.
This path has no hosted-service usage limits.

Measured on the same 5.9s clip that the xAI handler was timed against
(~370-400ms per turn): 105ms inside the engine, 175ms including the helper
process launch. Punctuation and capitals come back, which was the reason to
prefer a hosted service over the old local Parakeet model in the first place,
and Mandarin, Cantonese and Taiwanese are supported outright rather than being
an undocumented accident.

The engine is Swift-only, so the work happens in ``macos/SpeechHelper``: PCM in
on stdin, one JSON object out. A turn's audio never reaches disk. Build it with
``macos/SpeechHelper/scripts/build.sh``; ``run-openrouter.sh`` does that
automatically when the binary is missing.

Every failure degrades to an empty transcript plus an error code, never an
exception into the pipeline, so a bad turn costs one turn.
"""

from __future__ import annotations

import json
import logging
import pathlib
import shutil
import subprocess
from time import perf_counter
from typing import Any, Iterator, Optional

import numpy as np

from chatbot.pipeline.handler_types import STTIn, STTOut
from chatbot.pipeline.messages import Transcription
from chatbot.STT.base_stt_handler import BaseSTTHandler

logger = logging.getLogger(__name__)

# The language codes behind the locales this engine transcribes, which is what
# the reply-language prompt is keyed on. `speech-helper --locales` prints the
# live list for a given Mac; this is the macOS 27 set, minus `mul` (the
# multilingual Indian locale, not a language anyone replies in) and minus the
# Chinese locales (zh, yue), which the engine transcribes but no TTS backend
# here can speak back.
#
# Checked against chatbot.LLM.utils by tests/test_llm_utils.py: a code without a
# name there would make --enable_lang_prompt silently emit nothing.
SUPPORTED_LANGUAGES = [
    "bn",
    "de",
    "en",
    "es",
    "fr",
    "gu",
    "hi",
    "it",
    "ja",
    "kn",
    "ks",
    "mai",
    "ml",
    "mr",
    "ne",
    "or",
    "pa",
    "pt",
    "ta",
    "te",
    "ur",
]

# macos/SpeechHelper/build/speech-helper, relative to this file's checkout.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_DEFAULT_HELPER = _REPO_ROOT / "macos" / "SpeechHelper" / "build" / "speech-helper"


class HelperMissingError(RuntimeError):
    """The helper binary is not built. Distinct from a turn that failed."""


def find_helper(helper_path: str = "") -> pathlib.Path:
    """The speech helper binary: the configured path, this checkout, then PATH."""
    if helper_path:
        candidate = pathlib.Path(helper_path).expanduser()
        if candidate.is_file():
            return candidate
        raise HelperMissingError(f"no speech helper at {candidate}")

    if _DEFAULT_HELPER.is_file():
        return _DEFAULT_HELPER
    found = shutil.which("speech-helper")
    if found:
        return pathlib.Path(found)
    raise HelperMissingError(
        f"no speech helper at {_DEFAULT_HELPER}; build it with macos/SpeechHelper/scripts/build.sh"
    )


def to_pcm16(audio: np.ndarray) -> bytes:
    """Float samples in [-1, 1] to the raw little-endian PCM the helper reads."""
    clipped = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def language_of(locale: str) -> str:
    """``en-US`` -> ``en``, ``zh-Hant-TW`` -> ``zh``. The prompt wants the language."""
    return locale.replace("_", "-").split("-")[0].lower()


class NativeSTTHandler(BaseSTTHandler):
    """Transcribe finalized utterances with Apple's on-device speech engine."""

    def setup(
        self,
        locale: str = "en-US",
        helper_path: str = "",
        timeout_s: float = 20.0,
        prepare_timeout_s: float = 300.0,
        gen_kwargs: dict[str, Any] | None = None,
    ) -> None:
        # The registry strips the ``native_stt`` prefix from the argument names
        # before calling this, so these are the short forms.
        self.locale = locale
        self.timeout_s = timeout_s
        self.prepare_timeout_s = prepare_timeout_s
        self.last_language = language_of(locale)
        self.sample_rate = 16000
        self.helper: pathlib.Path | None = None
        try:
            self.helper = find_helper(helper_path)
        except HelperMissingError as exc:
            # Surface a missing helper at startup rather than on the first
            # spoken turn, where it would look like the microphone failed.
            logger.error("Native STT has no helper binary yet: %s", exc)
            return
        self._prepare()

    def _prepare(self) -> None:
        """Install this locale's model now so no spoken turn waits for it."""
        if self.helper is None:
            return
        try:
            result = subprocess.run(
                [str(self.helper), "--prepare", "--locale", self.locale],
                capture_output=True,
                timeout=self.prepare_timeout_s,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Native STT could not prepare %s: %s", self.locale, exc)
            return
        if result.returncode != 0:
            logger.warning(
                "Native STT could not prepare %s: %s",
                self.locale,
                _error_of(result.stdout, result.stderr),
            )
            return
        logger.info("Native STT ready (%s, %s)", self.locale, self.helper)

    def warmup(self) -> None:
        """No weights to hold: the helper loads the model per turn, from cache."""

    def process(self, vad_audio: STTIn) -> Iterator[STTOut]:
        audio = np.asarray(vad_audio.audio, dtype=np.float32)
        started_s = perf_counter()

        # The whole turn goes through every revision, unlike the xAI handler,
        # which uploaded only the audio past a mark because each byte crossed
        # the network. Locally the audio is free and the full utterance gives
        # the engine the context it needs for punctuation, so there is no
        # resume mark to keep and no partial text to stitch.
        text, error = self._transcribe(audio)

        logger.info(
            "Native STT turn=%s rev=%s audio=%.1fs total=%.3fs chars=%d%s",
            vad_audio.turn_id,
            vad_audio.turn_revision,
            len(audio) / self.sample_rate,
            perf_counter() - started_s,
            len(text),
            f" error={error}" if error else "",
        )

        yield Transcription(
            text=text,
            language_code=self.last_language,
            turn_id=vad_audio.turn_id,
            turn_revision=vad_audio.turn_revision,
            speech_stopped_at_s=vad_audio.created_at_s,
            error=error,
            active_speech_ms=vad_audio.active_speech_ms,
        )

    def _transcribe(self, audio: np.ndarray) -> tuple[str, Optional[str]]:
        """(text, error). Any failure yields empty text plus a code."""
        if len(audio) == 0:
            return "", None
        if self.helper is None:
            return "", "stt_unavailable"

        command = [
            str(self.helper),
            "--locale",
            self.locale,
            "--sample-rate",
            str(self.sample_rate),
        ]
        try:
            result = subprocess.run(
                command,
                input=to_pcm16(audio),
                capture_output=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            logger.error("Native STT timed out after %.1fs", self.timeout_s)
            return "", "stt_failed"
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("Native STT could not run the helper: %s", exc)
            return "", "stt_failed"

        if result.returncode != 0:
            logger.error("Native STT helper failed: %s", _error_of(result.stdout, result.stderr))
            return "", "stt_failed"

        try:
            body = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
        except ValueError:
            logger.error("Native STT helper returned a non-JSON body")
            return "", "stt_failed"
        if not isinstance(body, dict):
            logger.error("Native STT helper returned %s, not an object", type(body).__name__)
            return "", "stt_failed"
        if body.get("error"):
            logger.error("Native STT helper reported: %s", body["error"])
            return "", "stt_failed"

        return str(body.get("text") or "").strip(), None

    def on_session_end(self) -> None:
        super().on_session_end()
        self.last_language = language_of(self.locale)


def _error_of(stdout: bytes, stderr: bytes) -> str:
    """The helper's JSON error if it wrote one, else whatever it said."""
    try:
        body = json.loads(stdout.decode("utf-8", "replace") or "{}")
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])
    except ValueError:
        pass
    text = stderr.decode("utf-8", "replace").strip() or stdout.decode("utf-8", "replace").strip()
    return text or "no output"
