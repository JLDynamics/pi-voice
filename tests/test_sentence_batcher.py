from chatbot.LLM.sentence_batcher import SentenceBatcher


def test_sentence_batcher_first_sentence_and_batch_accumulation():
    """First complete sentence is released alone; later ones accumulate up to batch_size."""
    batcher = SentenceBatcher(batch_size=2)

    # First complete sentence flushes immediately even when batch_size > 1
    ready = batcher.add("First sentence. Second sentence! ")
    assert ready == [["First sentence."]]

    # Second sentence is buffered while waiting for batch_size (2)
    ready = batcher.add("Third sentence? ")
    assert ready == []

    # Third sentence completes the batch of 2
    ready = batcher.add("Fourth sentence. ")
    assert ready == [["Second sentence!", "Third sentence?"]]

    # Trailing sentence drained at the end
    assert batcher.drain() == ["Fourth sentence."]


def test_sentence_batcher_splitting_punctuation_and_newline():
    """SentenceBatcher handles '.', '!', '?', and newlines."""
    batcher = SentenceBatcher(batch_size=2)

    # Add text with period, exclamation, question mark, and newline
    ready = batcher.add("Hello world. How are you! Is it sunny? Line one.\nLine two. ")
    assert ready == [
        ["Hello world."],
        ["How are you!", "Is it sunny?"],
    ]

    # Remainder can be drained
    remainder = batcher.drain()
    assert remainder == ["Line one.", "Line two."]


def test_sentence_batcher_drain_flushes_remainder():
    """drain() flushes any unflushed batch and trailing non-empty text."""
    batcher = SentenceBatcher(batch_size=3)

    ready = batcher.add("Only one sentence. Partial trail without punct")
    assert ready == [["Only one sentence."]]

    remainder = batcher.drain()
    assert remainder == ["Partial trail without punct"]
    assert batcher.drain() == []
