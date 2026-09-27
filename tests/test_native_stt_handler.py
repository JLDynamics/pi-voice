import json
import pathlib
import subprocess

import numpy as np
import pytest

from chatbot.pipeline.messages import Transcription, VADAudio
from chatbot.STT.native_stt_handler import (
    HelperMissingError,
    NativeSTTHandler,
    find_helper,
    language_of,
    to_pcm16,
)

HELPER = pathlib.Path(__file__).resolve().parents[1] / "macos" / "SpeechHelper" / "build" / "speech-helper"


class FakeRun:
    """One `subprocess.run` result, plus what the call was given."""

    def __init__(self, returncode=0, stdout=b"", stderr=b"", raises=None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises


def _handler(monkeypatch, runs, locale="en-US"):
    handler = object.__new__(NativeSTTHandler)
    handler.locale = locale
    handler.timeout_s = 5.0
    handler.prepare_timeout_s = 5.0
    handler.last_language = language_of(locale)
    handler.sample_rate = 16000
    handler.helper = pathlib.Path("/fake/speech-helper")
    handler.calls = []

    def fake_run(command, input=None, capture_output=False, timeout=None):
        handler.calls.append({"command": command, "samples": len(input) // 2 if input else 0})
        result = runs.pop(0)
        if result.raises is not None:
            raise result.raises
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    return handler


def _ok(text, **extra):
    return FakeRun(stdout=json.dumps({"text": text, **extra}).encode())


def test_pcm16_conversion_clips_and_scales():
    pcm = to_pcm16(np.array([0.0, 1.0, -1.0, 2.0, -2.0], dtype=np.float32))
    assert np.frombuffer(pcm, dtype="<i2").tolist() == [0, 32767, -32767, 32767, -32767]


def test_language_of_drops_the_region():
    assert language_of("en-US") == "en"
    assert language_of("zh-CN") == "zh"
    assert language_of("yue-CN") == "yue"
    assert language_of("en_GB") == "en", "underscore form comes from Locale.identifier"


def test_a_configured_helper_path_must_exist(tmp_path):
    binary = tmp_path / "speech-helper"
    binary.write_text("#!/bin/sh\n")
    assert find_helper(str(binary)) == binary
    with pytest.raises(HelperMissingError, match="no speech helper at"):
        find_helper(str(tmp_path / "missing"))


def test_transcribes_a_turn(monkeypatch):
    handler = _handler(monkeypatch, [_ok("Hello there.", locale="en-US")])
    out = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1", turn_revision=0)))

    assert len(out) == 1 and isinstance(out[0], Transcription)
    assert out[0].text == "Hello there."
    assert out[0].error is None
    assert out[0].language_code == "en"
    assert handler.calls[0]["samples"] == 16000


def test_the_locale_and_sample_rate_reach_the_helper(monkeypatch):
    handler = _handler(monkeypatch, [_ok("你好")], locale="zh-CN")
    out = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1")))

    assert out[0].language_code == "zh"
    command = handler.calls[0]["command"]
    assert command[1:] == ["--locale", "zh-CN", "--sample-rate", "16000"]


def test_a_revision_re_transcribes_the_whole_turn(monkeypatch):
    """Unlike the xAI handler, which uploaded only the audio past a mark.

    Locally there is nothing to save by sending less, and the full utterance is
    what lets the engine punctuate it, so each revision replaces the text
    rather than being stitched onto it.
    """
    handler = _handler(monkeypatch, [_ok("hello there"), _ok("Hello there, and again.")])
    a = VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1", turn_revision=0)
    b = VADAudio(audio=np.zeros(24000, dtype=np.float32), turn_id="t1", turn_revision=1)

    assert list(handler.process(a))[0].text == "hello there"
    assert list(handler.process(b))[0].text == "Hello there, and again."
    assert [call["samples"] for call in handler.calls] == [16000, 24000]


def test_empty_audio_is_not_sent_to_the_helper(monkeypatch):
    handler = _handler(monkeypatch, [])
    out = list(handler.process(VADAudio(audio=np.zeros(0, dtype=np.float32), turn_id="t1")))

    assert out[0].text == "" and out[0].error is None
    assert handler.calls == []


def test_a_failed_helper_degrades_to_an_empty_transcript(monkeypatch):
    handler = _handler(monkeypatch, [FakeRun(returncode=1, stdout=b'{"error":"boom"}')])
    out = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1")))

    assert out[0].text == ""
    assert out[0].error == "stt_failed"


def test_a_json_error_body_is_a_failure_even_with_status_zero(monkeypatch):
    handler = _handler(monkeypatch, [FakeRun(stdout=b'{"error":"locale xx-YY is not supported"}')])
    out = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1")))

    assert out[0].error == "stt_failed"


def test_a_non_json_body_is_a_failure(monkeypatch):
    handler = _handler(monkeypatch, [FakeRun(stdout=b"not json at all")])
    out = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1")))

    assert out[0].error == "stt_failed"


def test_a_timeout_degrades_to_an_empty_transcript(monkeypatch):
    timeout = subprocess.TimeoutExpired(cmd="speech-helper", timeout=5.0)
    handler = _handler(monkeypatch, [FakeRun(raises=timeout)])
    out = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1")))

    assert out[0].text == ""
    assert out[0].error == "stt_failed"


def test_an_unbuilt_helper_reports_itself_distinctly(monkeypatch):
    """setup() leaves helper None rather than raising, so the pipeline still runs."""
    handler = _handler(monkeypatch, [])
    handler.helper = None
    out = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1")))

    assert out[0].error == "stt_unavailable"
    assert handler.calls == []


def test_registry_config_matches_the_setup_signature():
    """The registry strips the `native_stt` prefix before calling setup().

    The xAI handler shipped with prefixed parameter names and died on the first
    live run with "setup() got an unexpected keyword argument 'url'". The unit
    tests build the handler by hand and never call setup(), so only this catches it.
    """
    import inspect

    from chatbot.arguments_classes.native_stt_arguments import NativeSTTHandlerArguments
    from chatbot.backend_registry import STT_BACKENDS

    config = STT_BACKENDS["native-stt"].normalize(NativeSTTHandlerArguments())
    accepted = set(inspect.signature(NativeSTTHandler.setup).parameters) - {"self"}
    assert set(config) <= accepted, f"setup() cannot accept {sorted(set(config) - accepted)}"


def test_chinese_is_not_advertised_as_a_language_this_build_handles():
    """The engine can transcribe it; no TTS backend here can speak it back."""
    from chatbot.LLM.utils import WHISPER_LANGUAGE_TO_LLM_LANGUAGE
    from chatbot.STT.native_stt_handler import SUPPORTED_LANGUAGES

    assert not {"zh", "yue"} & set(SUPPORTED_LANGUAGES)
    assert "zh" not in WHISPER_LANGUAGE_TO_LLM_LANGUAGE


# --- the real binary, when it has been built -----------------------------------------


needs_helper = pytest.mark.skipif(
    not HELPER.is_file(),
    reason="run macos/SpeechHelper/scripts/build.sh to test the helper itself",
)


@needs_helper
def test_the_helper_lists_locales_as_json():
    out = subprocess.run([str(HELPER), "--locales"], capture_output=True, timeout=60)
    body = json.loads(out.stdout)

    assert out.returncode == 0
    assert "en-US" in body["supported"]
    assert set(body["installed"]) <= set(body["supported"])


@needs_helper
def test_the_helper_returns_an_empty_transcript_for_silence():
    """The contract the handler depends on: silence is text "", not a failure."""
    silence = to_pcm16(np.zeros(16000, dtype=np.float32))
    out = subprocess.run(
        [str(HELPER), "--locale", "en-US", "--sample-rate", "16000"],
        input=silence,
        capture_output=True,
        timeout=60,
    )
    body = json.loads(out.stdout)

    assert out.returncode == 0
    assert body["text"] == ""
    assert body["duration_s"] == pytest.approx(1.0)


@needs_helper
def test_the_helper_rejects_a_locale_it_cannot_transcribe():
    out = subprocess.run(
        [str(HELPER), "--locale", "xx-YY"],
        input=b"",
        capture_output=True,
        timeout=60,
    )

    assert out.returncode == 1
    assert "xx-YY" in json.loads(out.stdout)["error"]


@needs_helper
def test_odd_length_input_is_reported_rather_than_guessed():
    """Half a sample means the caller and helper disagree about the format."""
    out = subprocess.run([str(HELPER), "--locale", "en-US"], input=b"abc", capture_output=True, timeout=60)

    assert out.returncode == 1
    assert "16-bit" in json.loads(out.stdout)["error"]
