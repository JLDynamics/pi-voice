import torch

from chatbot.VAD.vad_iterator import VADIterator


class _FakeVADModel:
    def __init__(self, probs: list[float]) -> None:
        self._probs = iter(probs)

    def reset_states(self) -> None:
        pass

    def __call__(self, x: torch.Tensor, sampling_rate: int) -> torch.Tensor:
        return torch.tensor(next(self._probs), dtype=torch.float32)


def _finish_utterance(iterator: VADIterator, silence_chunk: torch.Tensor):
    spoken_utterance = None
    for _ in range(5):
        spoken_utterance = iterator(silence_chunk)
        if spoken_utterance is not None:
            break
    return spoken_utterance


def test_triggering_chunk_is_kept_in_buffer() -> None:
    model = _FakeVADModel([0.9, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1])
    iterator = VADIterator(
        model=model,
        threshold=0.5,
        sampling_rate=16000,
        min_silence_duration_ms=100,
        speech_pad_ms=0,
    )

    first_chunk = torch.ones(512)
    second_chunk = torch.ones(512) * 2
    silence_chunk = torch.zeros(512)

    assert iterator(first_chunk) is None
    assert iterator(second_chunk) is None
    spoken_utterance = _finish_utterance(iterator, silence_chunk)

    assert spoken_utterance is not None
    assert len(spoken_utterance) == 7
    assert torch.equal(spoken_utterance[0], first_chunk)
    assert torch.equal(spoken_utterance[1], second_chunk)
    assert all(torch.equal(chunk, silence_chunk) for chunk in spoken_utterance[2:])


def test_pre_speech_padding_is_prepended_to_final_utterance() -> None:
    model = _FakeVADModel([0.1, 0.1, 0.9, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1])
    iterator = VADIterator(
        model=model,
        threshold=0.5,
        sampling_rate=16000,
        min_silence_duration_ms=100,
        speech_pad_ms=64,
    )

    first_chunk = torch.ones(512)
    second_chunk = torch.ones(512) * 2
    third_chunk = torch.ones(512) * 3
    fourth_chunk = torch.ones(512) * 4
    silence_chunk = torch.zeros(512)

    assert iterator(first_chunk) is None
    assert iterator(second_chunk) is None
    assert iterator(third_chunk) is None
    assert iterator(fourth_chunk) is None

    spoken_utterance = _finish_utterance(iterator, silence_chunk)

    assert spoken_utterance is not None
    assert len(spoken_utterance) == 9
    assert torch.equal(spoken_utterance[0], first_chunk)
    assert torch.equal(spoken_utterance[1], second_chunk)
    assert torch.equal(spoken_utterance[2], third_chunk)
    assert torch.equal(spoken_utterance[3], fourth_chunk)
    assert all(torch.equal(chunk, silence_chunk) for chunk in spoken_utterance[4:])


def test_speech_buffer_keeps_prefix_out_of_active_speech_buffer() -> None:
    model = _FakeVADModel([0.1, 0.1, 0.9])
    iterator = VADIterator(
        model=model,
        threshold=0.5,
        sampling_rate=16000,
        min_silence_duration_ms=100,
        speech_pad_ms=32,
    )

    older_chunk = torch.ones(512)
    latest_pre_speech_chunk = torch.ones(512) * 2
    triggering_chunk = torch.ones(512) * 3

    assert iterator(older_chunk) is None
    assert iterator(latest_pre_speech_chunk) is None
    assert iterator(triggering_chunk) is None

    assert len(iterator.buffer) == 1
    assert torch.equal(iterator.buffer[0], triggering_chunk)

    speech_buffer = iterator.speech_buffer()
    assert len(speech_buffer) == 2
    assert torch.equal(speech_buffer[0], latest_pre_speech_chunk)
    assert torch.equal(speech_buffer[1], triggering_chunk)


def test_final_samples_are_kept_until_vad_declares_done() -> None:
    model = _FakeVADModel([0.9, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1])
    iterator = VADIterator(
        model=model,
        threshold=0.5,
        sampling_rate=16000,
        min_silence_duration_ms=100,
        speech_pad_ms=64,
    )

    first_chunk = torch.ones(512)
    second_chunk = torch.ones(512) * 2
    trailing_chunks = [torch.ones(512) * value for value in (10, 11, 12, 13, 14)]

    assert iterator(first_chunk) is None
    assert iterator(second_chunk) is None

    spoken_utterance = None
    for chunk in trailing_chunks:
        spoken_utterance = iterator(chunk)

    assert spoken_utterance is not None
    assert len(spoken_utterance) == 7
    assert torch.equal(spoken_utterance[0], first_chunk)
    assert torch.equal(spoken_utterance[1], second_chunk)
    assert torch.equal(spoken_utterance[2], trailing_chunks[0])
    assert torch.equal(spoken_utterance[3], trailing_chunks[1])
    assert torch.equal(spoken_utterance[4], trailing_chunks[2])
    assert torch.equal(spoken_utterance[5], trailing_chunks[3])
    assert torch.equal(spoken_utterance[6], trailing_chunks[4])


def test_brief_silence_is_preserved_when_speech_resumes() -> None:
    model = _FakeVADModel([0.9, 0.1, 0.1, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1])
    iterator = VADIterator(
        model=model,
        threshold=0.5,
        sampling_rate=16000,
        min_silence_duration_ms=100,
        speech_pad_ms=0,
    )

    first_chunk = torch.ones(512)
    pause_chunks = [torch.ones(512) * value for value in (8, 9)]
    resumed_chunk = torch.ones(512) * 2
    ending_silence = torch.zeros(512)

    assert iterator(first_chunk) is None
    assert iterator(pause_chunks[0]) is None
    assert iterator(pause_chunks[1]) is None
    assert iterator(resumed_chunk) is None

    spoken_utterance = _finish_utterance(iterator, ending_silence)

    assert spoken_utterance is not None
    assert len(spoken_utterance) == 9
    assert torch.equal(spoken_utterance[0], first_chunk)
    assert torch.equal(spoken_utterance[1], pause_chunks[0])
    assert torch.equal(spoken_utterance[2], pause_chunks[1])
    assert torch.equal(spoken_utterance[3], resumed_chunk)
    assert all(torch.equal(chunk, ending_silence) for chunk in spoken_utterance[4:])


def test_active_speech_samples_include_hysteresis_band_and_exclude_trailing_silence() -> None:
    model = _FakeVADModel([0.1, 0.9, 0.4, 0.1, 0.1, 0.1, 0.1, 0.1])
    iterator = VADIterator(
        model=model,
        threshold=0.5,
        sampling_rate=16000,
        min_silence_duration_ms=100,
        speech_pad_ms=512,
    )

    pre_speech_chunk = torch.ones(512)
    speech_chunk = torch.ones(512) * 2
    maintained_speech_chunk = torch.ones(512) * 3
    silence_chunk = torch.zeros(512)

    assert iterator(pre_speech_chunk) is None
    assert iterator(speech_chunk) is None
    assert iterator(maintained_speech_chunk) is None
    assert iterator.active_speech_samples == 1024

    spoken_utterance = _finish_utterance(iterator, silence_chunk)

    assert spoken_utterance is not None
    assert iterator.last_utterance_active_speech_samples == 1024
    assert iterator.active_speech_samples == 0
    assert len(spoken_utterance) > 2


def test_sample_counters_track_the_buffers_they_summarize() -> None:
    # The handler polls these totals once per audio chunk instead of re-summing
    # the chunk lists, so they must stay exactly in step through trigger,
    # continuation and end-of-speech resets.
    probs = [0.1, 0.1, 0.9, 0.9, 0.9, 0.2, 0.1, 0.1, 0.1, 0.1, 0.9, 0.9, 0.1, 0.1, 0.1, 0.1]
    iterator = VADIterator(
        model=_FakeVADModel(probs),
        threshold=0.5,
        sampling_rate=16000,
        min_silence_duration_ms=100,
        speech_pad_ms=30,
    )

    saw_speech = False
    for _ in probs:
        iterator(torch.ones(512))
        assert iterator.speech_buffer_samples() == sum(len(t) for t in iterator.speech_buffer())
        assert iterator.buffer_samples() == sum(len(t) for t in iterator.buffer)
        saw_speech = saw_speech or bool(iterator.buffer)

    assert saw_speech


def test_reset_states_clears_sample_counters() -> None:
    iterator = VADIterator(
        model=_FakeVADModel([0.9, 0.9]),
        threshold=0.5,
        sampling_rate=16000,
        min_silence_duration_ms=100,
        speech_pad_ms=0,
    )
    iterator(torch.ones(512))
    iterator(torch.ones(512))
    assert iterator.speech_buffer_samples() > 0

    iterator.reset_states()
    assert iterator.speech_buffer_samples() == 0
    assert iterator.buffer_samples() == 0


def test_long_utterance_compacts_tiny_chunks_without_losing_samples() -> None:
    chunk_count = 48
    iterator = VADIterator(
        model=_FakeVADModel([0.9] * chunk_count),
        threshold=0.5,
        sampling_rate=16000,
        min_silence_duration_ms=100,
        speech_pad_ms=0,
    )
    for _ in range(chunk_count):
        iterator(torch.ones(512))

    assert iterator.speech_buffer_samples() == chunk_count * 512
    assert len(iterator.buffer) < chunk_count
    assert iterator.speech_buffer_samples() == sum(len(t) for t in iterator.speech_buffer())
