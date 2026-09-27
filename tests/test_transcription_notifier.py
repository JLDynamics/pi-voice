import logging
from queue import Queue
from threading import Event

from chatbot.pipeline.events import TranscriptionCompletedEvent
from chatbot.pipeline.messages import Transcription
from chatbot.STT.transcription_notifier import TranscriptionNotifier


def _notifier(
    text_output_queue: Queue | None = None,
    should_listen: Event | None = None,
) -> TranscriptionNotifier:
    notifier = object.__new__(TranscriptionNotifier)
    notifier.setup(text_output_queue=text_output_queue, should_listen=should_listen)
    return notifier


def test_empty_final_transcription_still_emits_completion():
    text_output_queue = Queue()
    notifier = _notifier(text_output_queue=text_output_queue)

    assert list(notifier.process(Transcription(text="", language_code="en", speech_stopped_at_s=123.0))) == []

    completed = text_output_queue.get_nowait()

    assert isinstance(completed, TranscriptionCompletedEvent)
    assert completed.transcript == ""
    assert completed.language_code == "en"
    assert completed.speech_stopped_at_s == 123.0
    assert completed.active_speech_ms is None
    assert text_output_queue.empty()


def test_final_transcription_forwards_active_speech_ms():
    text_output_queue = Queue()
    notifier = _notifier(text_output_queue=text_output_queue)

    assert list(notifier.process(Transcription(text="hello", language_code="en", active_speech_ms=448))) == []

    completed = text_output_queue.get_nowait()
    assert isinstance(completed, TranscriptionCompletedEvent)
    assert completed.transcript == "hello"
    assert completed.active_speech_ms == 448
    assert text_output_queue.empty()


def test_non_empty_final_transcription_logs_full_text_at_info(caplog):
    notifier = _notifier()
    transcript = "hello " * 30

    with caplog.at_level(logging.INFO, logger="chatbot.STT.transcription_notifier"):
        assert list(notifier.process(Transcription(text=transcript, language_code="en"))) == []

    assert "Transcription completed (language=en): " + transcript in caplog.text


def test_empty_final_transcription_reenables_listening_without_runtime_config():
    should_listen = Event()
    notifier = _notifier(should_listen=should_listen)

    assert list(notifier.process(Transcription(text="", language_code="en"))) == []

    assert should_listen.is_set()
