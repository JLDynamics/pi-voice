"""Speak with Apple's Siri voices, through the siri-tts helper binary.

Siri's voices are the ones macOS itself uses and the ones most people find
best on a Mac, but Apple does not publish them: `AVSpeechSynthesisVoice`
never lists them, `say -v` silently ignores their names, and only the
system-wide System Voice setting reaches them -- at 1.27s per utterance with
no streaming, which a live conversation cannot use.

siri-tts (MIT, github.com/maximilianromer/siri-tts-cli) dlopens the private
SiriTTSService/TextToSpeech frameworks and streams raw PCM instead. Measured
against the Kokoro backend it replaced, on the same sentence: 84ms to first
audio versus 208ms, and the whole utterance rendered in 92ms.

The cost is honest: private frameworks are not API, so a macOS update can
rename or remove what this depends on. That failure is loud -- the binary
exits non-zero and the turn degrades. There is no second backend to fall back
to: the MLX voices were removed deliberately, so restoring one means reverting
that removal and running `uv sync`, not flipping a flag.
"""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from queue import Queue
from threading import Event
from typing import Any

import numpy as np

from chatbot.pipeline.cancel_scope import CancelScope
from chatbot.pipeline.messages import TTSInput
from chatbot.pipeline.queue_types import TextEventItem
from chatbot.pipeline.speculative_turns import SpeculativeTurnTracker
from chatbot.TTS.base_tts_handler import BaseTTSHandler

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "en-US-F"
DEFAULT_BINARY = "~/.local/bin/siri-tts"
# siri-tts emits LEI16@48000 and rejects any other --data-format, so the
# handler resamples rather than asking the binary to.
MODEL_SR = 48000
PIPELINE_SR = 24000
# 2048 frames at 48 kHz: ~43ms per chunk, small enough that the first block
# reaches the player promptly and large enough to avoid a read per sample.
READ_BYTES = 4096


class SiriTTSUnavailable(RuntimeError):
    """The helper binary is missing or refused to run."""


@dataclass
class _Chunk:
    """What the base class's streaming loop expects: an object with `audio`."""

    audio: np.ndarray


def find_binary(binary: str = DEFAULT_BINARY) -> pathlib.Path:
    """The siri-tts binary: the configured path, then PATH."""
    candidate = pathlib.Path(os.path.expanduser(binary))
    if candidate.is_file():
        return candidate
    found = shutil.which("siri-tts")
    if found:
        return pathlib.Path(found)
    raise SiriTTSUnavailable(
        f"no siri-tts binary at {candidate}; build it from github.com/maximilianromer/siri-tts-cli"
    )


class SiriTTSHandler(BaseTTSHandler):
    """Stream Siri speech from the helper binary into the pipeline."""

    backend_name = "Siri"

    def setup(
        self,
        should_listen: Event,
        voice: str = DEFAULT_VOICE,
        binary: str = DEFAULT_BINARY,
        timeout_s: float = 60.0,
        gen_kwargs: dict[str, Any] | None = None,
        cancel_scope: CancelScope | None = None,
        speculative_turns: SpeculativeTurnTracker | None = None,
        text_output_queue: Queue[TextEventItem] | None = None,
    ) -> None:
        self.voice = voice
        self.timeout_s = float(timeout_s)
        self._resampler: Any = None
        # No weights are loaded here: the voices live in the OS, so there is
        # nothing resident and `model` stays None for the base class.
        self.model = None
        self._common_setup(should_listen, gen_kwargs, cancel_scope, speculative_turns, text_output_queue, "siri")
        self.binary = find_binary(binary)
        self._check_voice()
        self.warmup()

    def _check_voice(self) -> None:
        """Fail at startup on an unknown voice, not mid-conversation."""
        try:
            listed = subprocess.run(
                [str(self.binary), "voices", "--available"],
                capture_output=True,
                timeout=30,
                text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SiriTTSUnavailable(f"could not run {self.binary}: {exc}") from exc

        installed = {
            line.split()[0]
            for line in listed.stdout.splitlines()[1:]
            if len(line.split()) > 4 and "yes" in line.split()
        }
        if installed and self.voice not in installed:
            raise SiriTTSUnavailable(
                f"Siri voice {self.voice!r} is not installed; available: {', '.join(sorted(installed))}"
            )
        logger.info("Siri TTS ready (%s, %s)", self.voice, self.binary)

    def _generate(self, text: str, gen_kwargs: dict[str, Any]) -> Iterable[Any]:
        import soxr

        self._resampler = soxr.ResampleStream(MODEL_SR, PIPELINE_SR, 1, dtype="float32")
        voice = str(gen_kwargs.pop("voice", self.voice))
        return self._stream(text, voice)

    def _stream(self, text: str, voice: str) -> Iterator[_Chunk]:
        command = [
            str(self.binary),
            "synthesize",
            # An explicit engine is required for stdout streaming: an auto
            # fallback could interleave two engines' partial streams.
            "--engine",
            "siri",
            "-v",
            voice,
            "--format",
            "pcm",
            "-o",
            "-",
            text,
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            assert process.stdout is not None
            leftover = b""
            while True:
                block = process.stdout.read(READ_BYTES)
                if not block:
                    break
                block = leftover + block
                usable = len(block) - (len(block) % 2)
                leftover = block[usable:]
                samples = np.frombuffer(block[:usable], dtype="<i2").astype(np.float32) / 32768.0
                if samples.size:
                    yield _Chunk(audio=samples)
            code = process.wait(timeout=self.timeout_s)
            if code != 0:
                detail = (process.stderr.read() or b"").decode("utf-8", "replace").strip() if process.stderr else ""
                logger.error("Siri TTS exited %s: %s", code, detail[:200])
        except subprocess.TimeoutExpired:
            logger.error("Siri TTS timed out after %.1fs", self.timeout_s)
        finally:
            # The base loop returns early on cancellation, which closes this
            # generator; without this the helper would keep rendering audio
            # nobody will play.
            if process.poll() is None:
                process.kill()
                process.wait()
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    def _map_chunk(self, audio: np.ndarray) -> np.ndarray:
        if self._resampler is None:
            return audio
        return np.asarray(self._resampler.resample_chunk(np.ascontiguousarray(audio), last=False), dtype=np.float32)

    def _model_tail(self) -> np.ndarray:
        if self._resampler is None:
            return np.zeros(0, dtype=np.float32)
        return np.asarray(self._resampler.resample_chunk(np.zeros(0, dtype=np.float32), last=True), dtype=np.float32)

    def cleanup(self) -> None:
        self._resampler = None

    def _apply_voice(self, tts_input: TTSInput) -> None:
        """Honour a per-session voice only when this Mac actually has it."""
        voice: str | None = None
        response: Any = tts_input.response
        audio_in: Any = getattr(response, "audio", None) if response else None
        if audio_in is not None and audio_in.output and audio_in.output.voice:
            voice = str(audio_in.output.voice)
        if voice is None and tts_input.runtime_config is not None:
            audio = tts_input.runtime_config.session.audio
            output = audio.output if audio is not None else None
            if output is not None and output.voice:
                voice = str(output.voice)
        if voice and voice != self.voice:
            try:
                previous, self.voice = self.voice, voice
                self._check_voice()
                logger.info("Siri voice set to %s", voice)
            except SiriTTSUnavailable as exc:
                logger.warning("Keeping %s: %s", previous, exc)
                self.voice = previous
