from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, Literal, Union

from openai.types.realtime.conversation_item import (
    RealtimeConversationItemAssistantMessage,
    RealtimeConversationItemFunctionCall,
    RealtimeConversationItemFunctionCallOutput,
    RealtimeConversationItemSystemMessage,
    RealtimeConversationItemUserMessage,
)
from openai.types.realtime.realtime_conversation_item_user_message import Content as UserContent
from openai.types.responses.response_input_image_param import ResponseInputImageParam
from openai.types.responses.response_input_message_content_list_param import (
    ResponseInputMessageContentListParam,
)
from openai.types.responses.response_input_param import (
    FunctionCallOutput,
    ResponseFunctionToolCallParam,
    ResponseInputItemParam,
    ResponseInputParam,
    ResponseOutputMessageParam,
)
from openai.types.responses.response_input_param import (
    Message as ResponseMessage,
)
from openai.types.responses.response_input_text_param import ResponseInputTextParam
from openai.types.responses.response_output_text_param import ResponseOutputTextParam
from pydantic import BaseModel

from chatbot.utils.utils import generate_id

logger = logging.getLogger(__name__)

AUDIO_INPUT_HISTORY_PLACEHOLDER = "[User audio input]"

# A tool output (a fetched article can be 16k chars) is needed in full for the
# turn that produced it and a follow-up question or two. After that it is dead
# weight re-sent on every request, so older outputs are abridged when the chat
# is serialized. The buffer itself keeps the full text.
TOOL_OUTPUT_KEEP_RECENT = 2
TOOL_OUTPUT_HISTORY_CHARS = 1200


def abridge_tool_output(output: str, limit: int = TOOL_OUTPUT_HISTORY_CHARS) -> str:
    if len(output) <= limit:
        return output
    return f"{output[:limit].rstrip()} …[abridged: {len(output) - limit} more characters omitted]"


class ChatItemError(Exception):
    """Raised when a conversation item fails validation in :meth:`Chat.add_item`."""


class CompactionResult(BaseModel):
    """Output of a :data:`CompactFn` summarization run."""

    user_summary: str
    assistant_summary: str


def _ensure_id(value: str | None, prefix: str) -> str:
    if value is None:
        return generate_id(prefix)
    if not value.startswith(f"{prefix}_"):
        raise ChatItemError(f"ID must start with '{prefix}_', got {value!r}")
    return value


SupportedItem = Union[
    RealtimeConversationItemSystemMessage,
    RealtimeConversationItemUserMessage,
    RealtimeConversationItemAssistantMessage,
    RealtimeConversationItemFunctionCall,
    RealtimeConversationItemFunctionCallOutput,
]


CompactFn = Callable[[ResponseInputParam], CompactionResult]


class Chat:
    """Manages conversation history with bounded size to avoid OOM issues.

    The buffer stores ``ConversationItem`` objects (user messages, assistant
    messages, function calls, function call outputs).  System messages are
    stored separately in ``init_chat_message`` and never placed in the buffer.

    History bounding is decided per ``add_item`` call via the ``compactor``
    argument:

    - ``compactor=None``: when the user-turn count exceeds ``size`` the oldest
      complete turn is evicted in place. Synchronous, lossy, no LLM involvement.
    - ``compactor=<fn>``: when ``size`` is exceeded, ``fn`` is invoked in a
      background thread to summarize older turns into a single user/assistant
      pair (with pending function calls preserved). Single-flight: while a
      compaction is running, additional triggers are silently bypassed.
    """

    def __init__(self, size: int) -> None:
        self.size = size
        self.init_chat_message: RealtimeConversationItemSystemMessage | None = None
        # ``size`` is the number of user turns to keep.  When exceeded the
        # oldest complete turn (everything up to the next user message)
        # is evicted -- or, with a compactor, summarized in the background.
        self.buffer: list[SupportedItem] = []
        self._pending_tool_calls: dict[str, RealtimeConversationItemFunctionCall] = {}
        self._user_turn_count: int = 0

        # All state mutations and serializations go through _lock. Public methods
        # acquire it once; internal callers that already hold it use the
        # ``_locked`` helpers, so no reentry is needed (regular Lock is safe).
        self._lock = threading.Lock()
        self._compact_in_flight: bool = False
        self._compact_thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._gen_counter = 0

    # ── Internal mutators (caller holds _lock) ─────────────────

    def _evict_oldest_turn(self) -> None:
        """Remove items from the front until the next user message boundary."""
        if not self.buffer:
            return
        first = self.buffer.pop(0)
        if isinstance(first, RealtimeConversationItemUserMessage):
            self._user_turn_count -= 1
        while self.buffer and not isinstance(self.buffer[0], RealtimeConversationItemUserMessage):
            self.buffer.pop(0)

    def _has_call_id_in_buffer(self, call_id: str) -> bool:
        for entry in self.buffer:
            if isinstance(entry, RealtimeConversationItemFunctionCall) and entry.call_id == call_id:
                return True
        return False

    def _mark_call_completed(
        self, call_id: str, status: Literal["completed", "incomplete", "in_progress"] | None = None
    ) -> None:
        """Set ``status`` to ``"completed"`` on the matching function_call."""
        for entry in self.buffer:
            if isinstance(entry, RealtimeConversationItemFunctionCall) and entry.call_id == call_id:
                entry.status = "completed" if status is None else status
                return

    def append_tool_output(self, call_id: str, output_item: RealtimeConversationItemFunctionCallOutput) -> None:
        """Append a ``function_call_output``, re-injecting its ``function_call`` if evicted.

        Also marks the paired ``function_call`` as ``"completed"`` if its
        status was ``None``.

        Raises :class:`ChatItemError` if *call_id* is unknown.
        """
        with self._lock:
            self._append_tool_output_locked(call_id, output_item)

    def _append_tool_output_locked(self, call_id: str, output_item: RealtimeConversationItemFunctionCallOutput) -> None:
        """Body of :meth:`append_tool_output`. Caller must hold ``_lock``."""
        if self._has_call_id_in_buffer(call_id):
            self._pending_tool_calls.pop(call_id, None)
            self._mark_call_completed(call_id, output_item.status)
            self.buffer.append(output_item)
            return

        if call_id in self._pending_tool_calls:
            logger.info("Re-injecting evicted function_call for call_id=%s", call_id)
            fc = self._pending_tool_calls.pop(call_id)
            fc.status = "completed" if output_item.status is None else output_item.status
            self.buffer.append(fc)
            self.buffer.append(output_item)
            return

        raise ChatItemError(f"No function_call with call_id '{call_id}' found in conversation history.")

    def init_chat(self, message: RealtimeConversationItemSystemMessage) -> None:
        with self._lock:
            self.init_chat_message = message

    def _store_system_locked(self, item: RealtimeConversationItemSystemMessage) -> None:
        """Body of :meth:`add_item` for system messages. Caller must hold ``_lock``."""
        item.id = _ensure_id(item.id, "sys")
        self.init_chat_message = item
        logger.debug("Set system message via conversation item")

    def _store_user_locked(self, item: RealtimeConversationItemUserMessage) -> None:
        """Body of :meth:`add_item` for user messages. Caller must hold ``_lock``."""
        item.id = _ensure_id(item.id, "msg")
        item.content = [
            p
            for p in item.content
            if (p.type == "input_text" and p.text)
            or (p.type == "input_image" and p.image_url)
            or (p.type == "input_audio" and p.audio)
        ]
        if not item.content:
            raise ChatItemError(
                "Message has no supported content. Supported modalities: input_text, input_image, input_audio."
            )
        self.buffer.append(item)
        self._user_turn_count += 1
        logger.debug("Added user message to chat (%d parts)", len(item.content))

    def _store_assistant_locked(self, item: RealtimeConversationItemAssistantMessage) -> bool:
        """Body of :meth:`add_item` for assistant messages. Caller must hold ``_lock``.

        Returns False when the message carries no speakable text and was dropped."""
        item.id = _ensure_id(item.id, "msg")
        item.content = [p for p in item.content if p.type == "output_text" and p.text]
        if not item.content:
            return False
        self.buffer.append(item)
        logger.debug("Added assistant message to chat (%d parts)", len(item.content))
        return True

    def _store_function_call_locked(self, item: RealtimeConversationItemFunctionCall) -> None:
        """Body of :meth:`add_item` for function calls. Caller must hold ``_lock``."""
        item.id = _ensure_id(item.id, "fc")
        item.call_id = _ensure_id(item.call_id, "call")
        self._pending_tool_calls[item.call_id] = item
        logger.debug("Added function_call to chat (call_id=%s)", item.call_id)

    def _enforce_hard_cap_locked(self) -> None:
        """Evict oldest turns past ``2 * size`` (runaway-client safety net). Caller must hold ``_lock``."""
        if self.size > 0 and self._user_turn_count > 2 * self.size:
            logger.warning(
                "Chat buffer exceeded hard cap (%d > 2 * size=%d); evicting oldest turn",
                self._user_turn_count,
                self.size,
            )
            while self._user_turn_count > 2 * self.size:
                self._evict_oldest_turn()

    def add_item(self, item: SupportedItem) -> SupportedItem:
        """Validate and route a conversation item into the chat buffer.

        Does not enforce the soft size limit — call :meth:`trim_if_needed`
        explicitly after each successful generation to evict or compact old
        turns. A hard upper bound at ``2 * size`` is enforced inline as a
        runaway-client safety net: if the user-turn count exceeds it, the
        oldest complete turn is evicted (lossy, no compaction).

        Raises :class:`ChatItemError` if the item fails validation.
        """
        with self._lock:
            if isinstance(item, RealtimeConversationItemSystemMessage):
                self._store_system_locked(item)
            elif isinstance(item, RealtimeConversationItemUserMessage):
                self._store_user_locked(item)
            elif isinstance(item, RealtimeConversationItemAssistantMessage):
                if not self._store_assistant_locked(item):
                    return item
            elif isinstance(item, RealtimeConversationItemFunctionCall):
                self._store_function_call_locked(item)
            elif isinstance(item, RealtimeConversationItemFunctionCallOutput):
                item.id = _ensure_id(item.id, "fco")
                self._append_tool_output_locked(item.call_id, item)
                logger.debug("Added function_call_output to chat (call_id=%s)", item.call_id)
            else:
                raise ChatItemError(f"Unsupported item type: {getattr(item, 'type', None)}")
            self._enforce_hard_cap_locked()
            return item

    def trim_if_needed(self, compactor: CompactFn | None = None) -> None:
        """Enforce the size limit after a generation completes. Fires when
        ``user_turn_count > size``.

        - ``compactor=None``: synchronous eviction of the oldest complete turn.
        - ``compactor=<fn>``: launch a background compaction (single-flight).

        Call once after each successful generation, not inside :meth:`add_item`.
        """
        with self._lock:
            if self._user_turn_count <= self.size:
                return
            if compactor is not None:
                self._maybe_trigger_compaction(compactor)
            else:
                while self._user_turn_count > self.size:
                    self._evict_oldest_turn()

    def replace_user_message_text(self, item_id: str, text: str) -> bool:
        """Replace the text content of an existing user message.

        Used by speculative turn revisions: the conversation turn remains the
        same, but the STT transcript is superseded by a transcription of a
        longer raw-audio buffer.
        """

        with self._lock:
            for item in self.buffer:
                if not isinstance(item, RealtimeConversationItemUserMessage) or item.id != item_id:
                    continue
                item.content = [UserContent(type="input_text", text=text)]
                logger.debug("Replaced speculative user message %s", item_id)
                return True
        return False

    def remove_user_message(self, item_id: str) -> bool:
        """Remove an existing user message from the bounded chat buffer."""

        with self._lock:
            for index, item in enumerate(self.buffer):
                if not isinstance(item, RealtimeConversationItemUserMessage) or item.id != item_id:
                    continue
                del self.buffer[index]
                self._user_turn_count -= 1
                logger.debug("Removed speculative user message %s", item_id)
                return True
        return False

    def rollback_generation(
        self,
        user_message_id: str,
        *,
        item_ids: set[str],
        call_ids: set[str],
    ) -> None:
        """Remove only the provisional state written by one failed generation.

        A tool output may be appended by a fast client while generation is still
        streaming, so rollback matches both item IDs and tool ``call_id`` values.
        Unrelated messages injected concurrently for a later turn are preserved.
        """

        with self._lock:
            kept: list[SupportedItem] = []
            for item in self.buffer:
                remove = item.id == user_message_id or item.id in item_ids
                if isinstance(item, (RealtimeConversationItemFunctionCall, RealtimeConversationItemFunctionCallOutput)):
                    remove = remove or item.call_id in call_ids
                if not remove:
                    kept.append(item)
            self.buffer = kept
            for call_id in call_ids:
                self._pending_tool_calls.pop(call_id, None)
            self._user_turn_count = sum(isinstance(item, RealtimeConversationItemUserMessage) for item in self.buffer)
            logger.debug("Rolled back failed generation for user message %s", user_message_id)

    def to_responses_api_chat(self, items: list[SupportedItem] | None = None) -> ResponseInputParam:
        """Serialize the chat (system prompt + buffer) for the OpenAI Responses API.

        If *items* is provided, serialize that slice instead of the live buffer
        (used by the compaction snapshot).
        """
        with self._lock:
            return self._to_responses_api_chat_locked(items if items is not None else self.buffer)

    @staticmethod
    def _recent_output_indices(items: list[SupportedItem]) -> set[int]:
        """Indices of tool outputs recent enough to send in full (older ones are abridged)."""
        output_indices = [
            index for index, item in enumerate(items) if isinstance(item, RealtimeConversationItemFunctionCallOutput)
        ]
        return set(output_indices[-TOOL_OUTPUT_KEEP_RECENT:]) if TOOL_OUTPUT_KEEP_RECENT else set()

    def _serialize_system_message(self) -> ResponseInputItemParam | None:
        """The stored system prompt as a Responses API item (None when unset)."""
        if not self.init_chat_message:
            return None
        return ResponseMessage(
            content=[
                ResponseInputTextParam(text=p.text or "A helpful AI assistant.", type="input_text")
                for p in self.init_chat_message.content
            ],
            role="system",
            type="message",
        )

    @staticmethod
    def _serialize_user_message(item: RealtimeConversationItemUserMessage) -> ResponseInputItemParam | None:
        """A user message as a Responses API item (None when nothing serializable remains)."""
        content: ResponseInputMessageContentListParam = []
        audio_placeholder_added = False
        for user_part in item.content:
            if user_part.type == "input_text" and user_part.text is not None:
                content.append(ResponseInputTextParam(text=user_part.text or "", type="input_text"))
            elif user_part.type == "input_image" and user_part.image_url is not None:
                img = ResponseInputImageParam(type="input_image", detail=user_part.detail or "auto")
                if user_part.image_url is not None:
                    img["image_url"] = user_part.image_url
                content.append(img)
            elif user_part.type == "input_audio" and not audio_placeholder_added:
                content.append(ResponseInputTextParam(text=AUDIO_INPUT_HISTORY_PLACEHOLDER, type="input_text"))
                audio_placeholder_added = True
        if not content:
            return None
        return ResponseMessage(content=content, role="user", type="message")

    @staticmethod
    def _serialize_assistant_message(item: RealtimeConversationItemAssistantMessage) -> ResponseInputItemParam | None:
        """An assistant message as a Responses API item (None when no text remains)."""
        assistant_content: list[ResponseOutputTextParam] = []
        for assistant_part in item.content:
            if assistant_part.type == "output_text" and assistant_part.text is not None:
                assistant_content.append(
                    ResponseOutputTextParam(text=assistant_part.text, type="output_text", annotations=[])
                )
        if not assistant_content:
            return None
        item_id = item.id
        assert item_id is not None and item_id != ""
        return ResponseOutputMessageParam(
            id=item_id,
            content=assistant_content,
            role="assistant",
            status=item.status or "completed",
            type="message",
        )

    @staticmethod
    def _serialize_function_call(item: RealtimeConversationItemFunctionCall) -> ResponseInputItemParam:
        """A function call as a Responses API item."""
        item_id = item.id
        call_id = item.call_id
        assert item_id is not None and item_id != ""
        assert call_id is not None and call_id != ""
        function_call = ResponseFunctionToolCallParam(
            arguments=item.arguments,
            call_id=call_id,
            name=item.name,
            type="function_call",
            id=item_id,
        )
        if item.id is not None:
            function_call["id"] = item.id
        if item.status is not None:
            function_call["status"] = item.status
        return function_call

    @staticmethod
    def _serialize_function_call_output(
        item: RealtimeConversationItemFunctionCallOutput, *, full: bool
    ) -> ResponseInputItemParam:
        """A function call output as a Responses API item (abridged unless recent)."""
        item_id = item.id
        assert item_id is not None and item_id != ""
        function_call_output = FunctionCallOutput(
            call_id=item.call_id,
            output=item.output if full else abridge_tool_output(item.output),
            type="function_call_output",
        )
        if item.id is not None:
            function_call_output["id"] = item.id
        if item.status is not None:
            function_call_output["status"] = item.status
        return function_call_output

    def _to_responses_api_chat_locked(self, items: list[SupportedItem]) -> ResponseInputParam:
        """Body of :meth:`to_responses_api_chat`. Caller must hold ``_lock``."""
        buffer_items = list(items)
        result: list[ResponseInputItemParam] = []
        full_output_indices = self._recent_output_indices(buffer_items)
        system = self._serialize_system_message()
        if system is not None:
            result.append(system)
        for index, item in enumerate(buffer_items):
            assert item.id is not None and item.id != "", f"item.id is {item.id}"
            if isinstance(item, RealtimeConversationItemUserMessage):
                serialized = self._serialize_user_message(item)
            elif isinstance(item, RealtimeConversationItemAssistantMessage):
                serialized = self._serialize_assistant_message(item)
            elif isinstance(item, RealtimeConversationItemFunctionCall) and item.call_id is not None:
                serialized = self._serialize_function_call(item)
            elif isinstance(item, RealtimeConversationItemFunctionCallOutput):
                serialized = self._serialize_function_call_output(item, full=index in full_output_indices)
            else:
                serialized = None
            if serialized is not None:
                result.append(serialized)
        return result

    def copy(self) -> Chat:
        """Return a shallow snapshot safe for concurrent read access."""
        with self._lock:
            clone = Chat(self.size)
            clone.init_chat_message = self.init_chat_message
            clone.buffer = list(self.buffer)
            clone._pending_tool_calls = dict(self._pending_tool_calls)
            clone._user_turn_count = self._user_turn_count
            return clone

    def reset(self) -> None:
        """Clear all conversation state. Cancels any in-flight compaction splice."""
        with self._lock:
            self._gen_counter += 1
            self._compact_in_flight = False
            self.buffer = []
            self.init_chat_message = None
            self._pending_tool_calls = {}
            self._user_turn_count = 0

    def close(self) -> None:
        """Permanently shut down the chat. In-flight compaction splice is suppressed.

        The compaction worker (a daemon thread) is not joined: it may be blocked
        in an LLM call. Process exit reaps it.
        """
        self._shutdown.set()
        with self._lock:
            self._gen_counter += 1
            self._compact_in_flight = False

    def image_message_ids(self) -> set[str]:
        """IDs of user messages currently carrying ``input_image`` content."""
        with self._lock:
            return {
                item.id
                for item in self.buffer
                if isinstance(item, RealtimeConversationItemUserMessage)
                and item.id is not None
                and any(p.type == "input_image" for p in item.content)
            }

    def strip_images(self, only_ids: set[str] | None = None) -> None:
        """Remove image content parts from user messages in the buffer.

        Called after appending the assistant response so images don't persist
        across turns. With *only_ids*, strip only those message IDs — the images
        the just-completed response actually consumed (captured before the
        request was sent). This leaves intact an image a fast client injected
        mid-generation for the *next* turn, which the current response never saw.
        Without *only_ids*, every image is stripped.
        """
        with self._lock:
            for item in self.buffer:
                if isinstance(item, RealtimeConversationItemUserMessage):
                    if only_ids is not None and item.id not in only_ids:
                        continue
                    item.content = [p for p in item.content if p.type != "input_image"]

    # ── Compaction internals ──────────────────────────────────

    def _compactable_prefix_locked(self) -> tuple[list[SupportedItem], set[str], int]:
        """Buffer items eligible for compaction: everything before the latest user turn.

        Caller must hold ``_lock``. The most recent turn stays untouched (it
        may be in-flight). ``marker_ids`` identifies the items the splice may
        drop. Returns an empty prefix when fewer than 2 turns are compactable.
        """
        n_turns = max(0, self._user_turn_count - 1)
        if n_turns < 2:
            return [], set(), n_turns
        # Slice up to (but not including) the (n_turns + 1)-th user message.
        user_seen = 0
        end_idx = len(self.buffer)
        for i, entry in enumerate(self.buffer):
            if isinstance(entry, RealtimeConversationItemUserMessage):
                user_seen += 1
                if user_seen == n_turns + 1:
                    end_idx = i
                    break
        items_to_compact = self.buffer[:end_idx]
        marker_ids = {entry.id for entry in items_to_compact if entry.id is not None}
        return items_to_compact, marker_ids, n_turns

    @staticmethod
    def _strip_media_from_snapshot(snapshot: ResponseInputParam) -> None:
        """Remove image/audio parts so the summarizer only reads text."""
        for raw in snapshot:
            if not isinstance(raw, dict) or raw.get("role") != "user":
                continue
            msg: dict[str, Any] = raw  # type: ignore[assignment]
            content = msg.get("content")
            if isinstance(content, list):
                msg["content"] = [
                    c for c in content if not (isinstance(c, dict) and c.get("type") in {"input_image", "input_audio"})
                ]

    def _compaction_stale(self, gen: int) -> bool:
        """True when the worker's snapshot was superseded by reset/close or shut down."""
        return self._shutdown.is_set() or self._gen_counter != gen

    def _compaction_drop_ids_locked(self, marker_ids: set[str]) -> set[str]:
        """IDs the splice may drop. Keeps an FC whose FCO sits outside the
        compacted range -- otherwise the FCO left behind would be orphaned.
        Caller must hold ``_lock``."""
        fco_call_ids_in_range = {
            x.call_id
            for x in self.buffer
            if isinstance(x, RealtimeConversationItemFunctionCallOutput) and x.id in marker_ids
        }
        fc_ids_to_keep = {
            x.id
            for x in self.buffer
            if x.id in marker_ids
            and isinstance(x, RealtimeConversationItemFunctionCall)
            and x.call_id not in fco_call_ids_in_range
        }
        return marker_ids - fc_ids_to_keep

    def _snapshot_for_compaction(
        self,
    ) -> tuple[ResponseInputParam, set[str], int]:
        """Compute the snapshot of items eligible for compaction.

        Caller must hold ``_lock``. Returns
        ``(serialized_snapshot, marker_ids, n_turns)``. ``marker_ids``
        identifies the buffer items that may be removed when the splice runs.
        Always leaves the most recent user turn untouched (it may be in-flight).
        Returns an empty result if there are fewer than 2 compactable turns.
        """
        items_to_compact, marker_ids, n_turns = self._compactable_prefix_locked()
        if n_turns < 2:
            return [], set(), n_turns
        snapshot = self._to_responses_api_chat_locked(items=items_to_compact)
        self._strip_media_from_snapshot(snapshot)
        return snapshot, marker_ids, n_turns

    def _maybe_trigger_compaction(self, compactor: CompactFn) -> None:
        """Start a background compaction worker. Bypass silently if one is running.

        Caller must hold ``_lock``.
        """
        if self._shutdown.is_set() or self._compact_in_flight:
            return
        snapshot, marker_ids, n_turns = self._snapshot_for_compaction()
        if n_turns < 2 or not marker_ids:
            return
        gen = self._gen_counter
        self._compact_in_flight = True
        thread = threading.Thread(
            target=self._compact_worker,
            args=(compactor, snapshot, marker_ids, gen),
            daemon=True,
            name="chat-compact",
        )
        self._compact_thread = thread
        logger.info(
            "Chat compaction triggered: compacting %d turn(s) (%d item(s)), buffer size=%d",
            n_turns,
            len(marker_ids),
            len(self.buffer),
        )
        thread.start()

    def _compact_worker(
        self,
        compactor: CompactFn,
        snapshot: ResponseInputParam,
        marker_ids: set[str],
        gen: int,
    ) -> None:
        """Worker thread entry point."""
        try:
            if self._compaction_stale(gen):
                return
            try:
                result = compactor(snapshot)
            except Exception:
                logger.exception("Chat compaction failed; chat unchanged")
                return
            if not isinstance(result, CompactionResult):
                logger.error("Compactor must return a CompactionResult, got %r", type(result).__name__)
                return
            if self._compaction_stale(gen):
                return
            self._apply_compaction(result, marker_ids, gen)
        finally:
            # Don't clobber the flag if reset/close has advanced the gen.
            with self._lock:
                if self._gen_counter == gen:
                    self._compact_in_flight = False

    def _apply_compaction(
        self,
        result: CompactionResult,
        marker_ids: set[str],
        gen: int,
    ) -> None:
        """Splice the summary in front of items not consumed by compaction.

        FC/FCO pairing is left entirely to :meth:`add_item` / :meth:`append_tool_output`.
        Compaction only drops items; it never inserts an FC into the buffer.
        Pending FCs (no FCO yet) stay in ``_pending_tool_calls`` and will be
        appended adjacent to their FCO when it arrives.
        """
        with self._lock:
            if self._compaction_stale(gen):
                return
            remaining = [x for x in self.buffer if x.id not in self._compaction_drop_ids_locked(marker_ids)]
            # Deferred to avoid a chat <-> chat_factories import cycle.
            from chatbot.LLM.chat_factories import make_assistant_message, make_user_message

            user_msg = make_user_message(result.user_summary)
            user_msg.id = generate_id("msg")
            asst_msg = make_assistant_message(result.assistant_summary)
            asst_msg.id = generate_id("msg")

            self.buffer = [user_msg, asst_msg, *remaining]
            self._user_turn_count = sum(1 for x in self.buffer if isinstance(x, RealtimeConversationItemUserMessage))
            logger.info(
                "Chat compaction applied: buffer now %d item(s), %d user turn(s)",
                len(self.buffer),
                self._user_turn_count,
            )
