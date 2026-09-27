"""Research the language model runs itself, inside one response.

Every function call used to leave the server: the client received it, called
the sidecar, posted the output back and asked for a new response, then armed
a watchdog in case any of those hops was lost. Running the work here instead
means "let me check" is followed by the answer with no client round trip.

Only ``bash`` remains, and it needs nothing but this process: the model writes
a ``curl`` command and :mod:`chatbot.LLM.curl_bash` runs it behind a tight
allowlist. The sidecar that once backed ``web_search``, ``read_page``,
``search_chat_history``, ``remember`` and ``forget`` was removed — Pi owns
web reading and memory now, and the page open in Chrome is read with Pi's
browser tools rather than a bridge of our own.

No client-side tools remain: ``screenshot`` was removed, so every tool the
model can call runs in this process.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from openai.types.responses import ResponseFunctionToolCall

from chatbot.LLM.curl_bash import run_research_command

logger = logging.getLogger(__name__)

# Tools executed here. Anything else the model calls is forwarded to the client.
SERVER_TOOL_NAMES: frozenset[str] = frozenset({"bash"})

# One response may chain this many tool rounds (search → read → read → answer
# is four). The cap exists so a confused model cannot loop forever in silence.
MAX_TOOL_ROUNDS = 6
# ...and this is how long, from the first model call, the loop keeps letting
# the model start *new* tool rounds. A spoken answer that is still researching
# after this must be given from what was found; the model can offer to dig
# deeper. One curl is usually under 2 s; the budget sits outside a 12 s cap
# plus the follow-up model call so a slow first fetch can still be answered.
TOOL_TIME_BUDGET_S = 22.0
# How often the cancel check runs while tool calls are in flight.
CANCEL_POLL_S = 0.02

ToolOutput = str


class ServerToolExecutor:
    """Runs the model's own research commands in this process.

    Thread-safe: one instance per LLM handler, called from the handler's
    pipeline thread; calls run on a small pool so a round with several of them
    (``parallel_tool_calls``) finishes in the time of the slowest.
    """

    def __init__(self, *, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="server-tool")

    # ── public API ───────────────────────────────────────────────────────────

    @staticmethod
    def handles(name: str) -> bool:
        return name in SERVER_TOOL_NAMES

    def run(self, name: str, arguments_json: str) -> ToolOutput:
        """Execute one tool and return the text the model reads as its output."""
        try:
            arguments = json.loads(arguments_json or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        started = time.perf_counter()
        try:
            output = self._dispatch(name, arguments)
        except Exception:  # noqa: BLE001 - a tool bug must not kill the response
            logger.exception("Server tool %s crashed", name)
            output = f"{name} failed unexpectedly. Answer from what you know and say you could not check."
        logger.info("Server tool %s finished in %.2fs (%d chars)", name, time.perf_counter() - started, len(output))
        return output

    def run_many(
        self,
        calls: Sequence[ResponseFunctionToolCall],
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> list[ToolOutput | None]:
        """Run several calls concurrently.

        Returns one output per call, in order. When ``is_cancelled`` turns true
        (the user barged in) the wait stops and every unfinished slot is
        ``None``; the commands finish on their own in the pool and are
        discarded.
        """
        futures: list[Future[ToolOutput]] = [
            self._pool.submit(self.run, call.name, call.arguments or "{}") for call in calls
        ]
        outputs: list[ToolOutput | None] = [None] * len(futures)
        pending = set(range(len(futures)))
        while pending:
            if is_cancelled is not None and is_cancelled():
                logger.info("Abandoning %d running tool call(s): the turn was interrupted", len(pending))
                break
            for index in list(pending):
                future = futures[index]
                if future.done():
                    outputs[index] = future.result()
                    pending.discard(index)
            if pending:
                time.sleep(CANCEL_POLL_S)
        return outputs

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ── dispatch ─────────────────────────────────────────────────────────────

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> ToolOutput:
        if name == "bash":
            timeout = arguments.get("timeout")
            seconds: float | None
            try:
                seconds = float(timeout) if timeout not in (None, "") else None
            except (TypeError, ValueError):
                seconds = None
            return run_research_command(str(arguments.get("command") or ""), timeout=seconds)
        return f"Unknown tool: {name}"
