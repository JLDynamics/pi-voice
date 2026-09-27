"""Factory helpers for building chat items and chats.

Standalone constructors shared by the conversation handler (in-band item
injection) and the language-model handlers (out-of-band chats). They build on
:class:`chatbot.LLM.chat.Chat` but carry no buffer state of their own.
"""

from __future__ import annotations

from openai.types.realtime import ConversationItem
from openai.types.realtime.conversation_item import (
    RealtimeConversationItemAssistantMessage,
    RealtimeConversationItemFunctionCall,
    RealtimeConversationItemFunctionCallOutput,
    RealtimeConversationItemSystemMessage,
    RealtimeConversationItemUserMessage,
)
from openai.types.realtime.realtime_conversation_item_assistant_message import (
    Content as AssistantContent,
)
from openai.types.realtime.realtime_conversation_item_system_message import Content as SystemContent
from openai.types.realtime.realtime_conversation_item_user_message import Content as UserContent
from openai.types.realtime.realtime_response_create_params import RealtimeResponseCreateParams

from chatbot.LLM.chat import Chat, ChatItemError


def make_user_message(text: str) -> RealtimeConversationItemUserMessage:
    return RealtimeConversationItemUserMessage(
        type="message",
        role="user",
        content=[UserContent(type="input_text", text=text)],
    )


def make_user_audio_message(audio_b64: str) -> RealtimeConversationItemUserMessage:
    return RealtimeConversationItemUserMessage(
        type="message",
        role="user",
        content=[UserContent(type="input_audio", audio=audio_b64)],
    )


def make_assistant_message(text: str) -> RealtimeConversationItemAssistantMessage:
    return RealtimeConversationItemAssistantMessage(
        type="message",
        role="assistant",
        content=[AssistantContent(type="output_text", text=text)],
    )


def make_system_message(text: str) -> RealtimeConversationItemSystemMessage:
    return RealtimeConversationItemSystemMessage(
        type="message",
        role="system",
        content=[SystemContent(type="input_text", text=text)],
    )


def add_supported_item(chat: Chat, item: ConversationItem) -> None:
    """Narrow a protocol ``ConversationItem`` to a :data:`SupportedItem` and add it to *chat*.

    Raises :class:`ChatItemError` on validation failure or unsupported type. Shared
    by the conversation handler (in-band item injection) and the language-model
    handlers (seeding an out-of-band response's throwaway chat from ``response.input``).
    """
    # call_id on function_call items must be client-supplied: it is referenced later by
    # function_call_output items, so we cannot silently generate one here.
    if isinstance(item, RealtimeConversationItemFunctionCall) and (
        item.call_id is None or not item.call_id.startswith("call_")
    ):
        raise ChatItemError("function_call item is missing a call_id. The call_id should start with 'call_'.")
    if isinstance(
        item,
        (
            RealtimeConversationItemSystemMessage,
            RealtimeConversationItemUserMessage,
            RealtimeConversationItemAssistantMessage,
            RealtimeConversationItemFunctionCall,
            RealtimeConversationItemFunctionCallOutput,
        ),
    ):
        chat.add_item(item)
        return
    raise ChatItemError(f"Unsupported item type: {getattr(item, 'type', None)}")


def build_active_chat(original_chat: Chat, response: RealtimeResponseCreateParams | None) -> Chat:
    """Build the chat an *out-of-band* response generates against (caller ensures out-of-band).

    Mirrors the OpenAI realtime semantics for ``input``:

    - ``input is None`` -> a read-only **copy of the default conversation** (the
      out-of-band response reads history but never commits back).
    - ``input == []`` -> a **fresh, empty chat** (context cleared; only the
      system prompt, added later by the handler, will be present).
    - ``input == [...]`` -> a **fresh chat seeded** with those items.

    Raises :class:`ChatItemError` if an ``input`` item fails validation.
    """
    if response is not None and response.input is not None:
        fresh = Chat(original_chat.size)
        for item in response.input:
            add_supported_item(fresh, item)
        return fresh
    return original_chat.copy()
