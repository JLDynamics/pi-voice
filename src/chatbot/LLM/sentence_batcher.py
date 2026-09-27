from __future__ import annotations

from chatbot.LLM.utils import split_sentences


class SentenceBatcher:
    """Batches spoken sentences for TTS from a stream of filtered text deltas.

    The first complete sentence is released on its own so speech starts as
    early as possible; later sentences are grouped up to ``batch_size`` for
    better prosody once audio is already playing.
    """

    def __init__(self, batch_size: int) -> None:
        self._batch_size = max(1, batch_size)
        self._tail = ""
        self._batch: list[str] = []
        self._first_flush_pending = True

    def add(self, text: str) -> list[list[str]]:
        """Fold filtered text in; return batches that reached the flush threshold."""
        self._tail += text
        ready: list[list[str]] = []
        sentences = split_sentences(self._tail)
        if len(sentences) > 1:
            for sentence in sentences[:-1]:
                self._batch.append(sentence)
                threshold = 1 if self._first_flush_pending else self._batch_size
                if len(self._batch) >= threshold:
                    ready.append(list(self._batch))
                    self._batch.clear()
                    self._first_flush_pending = False
            # Keep the raw tail (the tokenizer strips its trailing space)
            # so the next delta cannot glue onto it as "one.Here" and
            # hide a sentence boundary from the tokenizer.
            tail_start = self._tail.rfind(sentences[-1])
            self._tail = self._tail[tail_start:] if tail_start >= 0 else sentences[-1]
        return ready

    def drain(self) -> list[str]:
        """Move any trailing text into the batch and hand the remainder over."""
        if self._tail.strip():
            self._batch.append(self._tail.strip())
            self._tail = ""
        batch, self._batch = self._batch, []
        return batch
