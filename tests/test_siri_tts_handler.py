import pathlib
import subprocess

import numpy as np
import pytest

from chatbot.TTS.siri_tts_handler import (
    MODEL_SR,
    PIPELINE_SR,
    SiriTTSHandler,
    SiriTTSUnavailable,
    find_binary,
)

BINARY = pathlib.Path("~/.local/bin/siri-tts").expanduser()

VOICES_TABLE = """NAME       LANGUAGE   VERSION  ENGINE  INSTALLED  ASSET KEY
en-US-F    en-US      0        siri    yes        en-US:fmvoice:male:en-US-F:premium:0
aaron      en-US      0        siri    yes        en-US:fmvoice:male:aaron:premium:0
martha     en-GB      0        siri    no         com.apple.speech.synthesis.voice.custom.siri.martha.premium
"""


def _handler(voice="en-US-F"):
    handler = object.__new__(SiriTTSHandler)
    handler.voice = voice
    handler.timeout_s = 5.0
    handler.binary = pathlib.Path("/fake/siri-tts")
    handler._resampler = None
    return handler


def test_a_configured_binary_must_exist(tmp_path, monkeypatch):
    binary = tmp_path / "siri-tts"
    binary.write_text("#!/bin/sh\n")
    assert find_binary(str(binary)) == binary

    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(SiriTTSUnavailable, match="no siri-tts binary"):
        find_binary(str(tmp_path / "missing"))


def test_an_uninstalled_voice_fails_at_startup(monkeypatch):
    """Better than discovering it mid-conversation, where it reads as a TTS fault."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=VOICES_TABLE, stderr=""),
    )
    _handler("en-US-F")._check_voice()  # installed: no raise

    with pytest.raises(SiriTTSUnavailable, match="martha"):
        _handler("martha")._check_voice()


def test_only_installed_voices_are_offered_in_the_error(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=VOICES_TABLE, stderr=""),
    )
    with pytest.raises(SiriTTSUnavailable) as excinfo:
        _handler("nope")._check_voice()

    message = str(excinfo.value)
    assert "aaron" in message and "en-US-F" in message
    assert "martha" not in message, "martha is not installed, so it is not a usable suggestion"


def test_the_resampler_maps_48k_to_the_pipeline_rate():
    import soxr

    handler = _handler()
    handler._resampler = soxr.ResampleStream(MODEL_SR, PIPELINE_SR, 1, dtype="float32")
    one_second = np.zeros(MODEL_SR, dtype=np.float32)

    out = np.concatenate([handler._map_chunk(one_second), handler._model_tail()])

    assert PIPELINE_SR * 0.98 <= out.size <= PIPELINE_SR * 1.02, "one second in, one second out"


def test_registry_config_matches_the_setup_signature():
    """The registry strips the `siri_tts` prefix before calling setup()."""
    import inspect

    from chatbot.arguments_classes.siri_tts_arguments import SiriTTSHandlerArguments
    from chatbot.backend_registry import TTS_BACKENDS

    config = TTS_BACKENDS["siri"].normalize(SiriTTSHandlerArguments())
    accepted = set(inspect.signature(SiriTTSHandler.setup).parameters) - {"self", "should_listen"}
    assert set(config) <= accepted, f"setup() cannot accept {sorted(set(config) - accepted)}"


def test_the_denoise_chain_is_off_by_default():
    """Siri audio has no hiss; running the MLX-tuned chain would only soften it."""
    from chatbot.arguments_classes.siri_tts_arguments import SiriTTSHandlerArguments
    from chatbot.backend_registry import TTS_BACKENDS

    gen = TTS_BACKENDS["siri"].normalize(SiriTTSHandlerArguments())["gen_kwargs"]

    assert gen["noise_gate"] is False
    assert gen["spectral_denoise"] is False


# --- the real binary, when it is installed ------------------------------------------


needs_binary = pytest.mark.skipif(
    not BINARY.is_file(),
    reason="build siri-tts from github.com/maximilianromer/siri-tts-cli to test it",
)


@needs_binary
def test_the_binary_streams_pcm_for_a_real_voice():
    """The contract the handler depends on: PCM on stdout, 48 kHz, 16-bit mono."""
    handler = _handler()
    handler.binary = BINARY
    chunks = list(handler._stream("Testing one two three.", "en-US-F"))

    assert chunks, "no audio came back"
    audio = np.concatenate([c.audio for c in chunks])
    assert audio.dtype == np.float32
    assert -1.0 <= float(audio.min()) and float(audio.max()) <= 1.0
    assert audio.size > MODEL_SR * 0.5, "a three-word sentence should exceed half a second"


@needs_binary
def test_an_unknown_voice_does_not_hang_or_yield_garbage():
    handler = _handler()
    handler.binary = BINARY
    chunks = list(handler._stream("hello", "no-such-voice-xyz"))

    assert sum(c.audio.size for c in chunks) == 0, "a bad voice must yield silence, not noise"
