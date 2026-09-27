from __future__ import annotations

import logging
import signal
from copy import deepcopy
from dataclasses import dataclass, fields
from queue import Queue
from threading import Event
from types import FrameType
from typing import Any, Literal, Optional, Sequence

from rich.console import Console

from chatbot.api.openai_realtime.pipeline_unit import PipelineUnit
from chatbot.arguments_classes.module_arguments import ModuleArguments
from chatbot.arguments_classes.native_stt_arguments import NativeSTTHandlerArguments
from chatbot.arguments_classes.realtime_server_arguments import RealtimeServerArguments
from chatbot.arguments_classes.responses_api_language_model_arguments import (
    ResponsesApiLanguageModelHandlerArguments,
)
from chatbot.arguments_classes.siri_tts_arguments import SiriTTSHandlerArguments
from chatbot.arguments_classes.vad_arguments import VADHandlerArguments
from chatbot.backend_registry import (
    LLM_BACKENDS,
    STT_BACKENDS,
    TTS_BACKENDS,
    BackendSelection,
    HandlerContext,
    create_backend_handler,
    select_backend,
)
from chatbot.pipeline.cancel_scope import CancelScope
from chatbot.pipeline.queue_types import (
    AudioInItem,
    AudioOutItem,
    LMOutItem,
    STTOutItem,
    TextEventItem,
    TextPromptItem,
    TTSInItem,
    VADOutItem,
)
from chatbot.pipeline.ready import PipelineReady
from chatbot.pipeline.speculative_turns import SpeculativeTurnTracker
from chatbot.utils.thread_manager import ThreadManager

console = Console()
logger = logging.getLogger(__name__)


@dataclass
class ParsedArguments:
    module_kwargs: ModuleArguments
    realtime_server_kwargs: RealtimeServerArguments
    vad_handler_kwargs: VADHandlerArguments
    stt_backend: BackendSelection
    llm_backend: BackendSelection
    tts_backend: BackendSelection


def parse_arguments(
    argv: Sequence[str] | None = None,
    *,
    command: Literal["serve"] = "serve",
) -> ParsedArguments:
    # transformers' DataClassType protocol does not recognise ordinary
    # @dataclass classes under mypy, even though they are the documented input.
    from transformers import HfArgumentParser

    argument_types: Any = (
        ModuleArguments,
        RealtimeServerArguments,
        VADHandlerArguments,
        NativeSTTHandlerArguments,
        ResponsesApiLanguageModelHandlerArguments,
        SiriTTSHandlerArguments,
    )
    parser = HfArgumentParser(argument_types, prog=f"chatbot {command}")
    parsed = parser.parse_args_into_dataclasses(args=list(argv) if argv is not None else None)
    module, server, vad, stt_config, llm_config, tts_config = parsed
    stt_backend = select_backend(STT_BACKENDS, module.stt, stt_config)
    tts_backend = select_backend(TTS_BACKENDS, module.tts, tts_config)
    llm_backend = select_backend(LLM_BACKENDS, "responses-api", llm_config)
    return ParsedArguments(
        module_kwargs=module,
        realtime_server_kwargs=server,
        vad_handler_kwargs=vad,
        stt_backend=stt_backend,
        llm_backend=llm_backend,
        tts_backend=tts_backend,
    )


def setup_logger(log_level: str) -> None:
    from chatbot.pipeline.log_context import PipelineLogFilter

    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s - %(pipeline_prefix)s%(name)s - %(levelname)s - %(message)s",
    )
    pipeline_filter = PipelineLogFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(pipeline_filter)


def _build_handlers(
    *,
    stop_event: Event,
    should_listen: Event,
    recv_audio_chunks_queue: Queue[AudioInItem],
    spoken_prompt_queue: Queue[VADOutItem],
    stt_output_queue: Queue[STTOutItem],
    text_prompt_queue: Queue[TextPromptItem],
    lm_response_queue: Queue[LMOutItem],
    lm_processed_queue: Queue[TTSInItem],
    send_audio_chunks_queue: Queue[AudioOutItem],
    text_output_queue: Queue[TextEventItem],
    module_kwargs: ModuleArguments,
    vad_handler_kwargs: VADHandlerArguments,
    stt_backend: BackendSelection,
    llm_backend: BackendSelection,
    tts_backend: BackendSelection,
    speculative_turns: SpeculativeTurnTracker,
    cancel_scope: CancelScope,
    response_playing: Event,
) -> list[Any]:
    from chatbot.LLM.lm_output_processor import LMOutputProcessor
    from chatbot.STT.transcription_notifier import TranscriptionNotifier
    from chatbot.VAD.vad_handler import VADHandler

    vad = VADHandler(
        stop_event,
        queue_in=recv_audio_chunks_queue,
        queue_out=spoken_prompt_queue,
        setup_args=(should_listen,),
        setup_kwargs={
            **{field.name: deepcopy(getattr(vad_handler_kwargs, field.name)) for field in fields(vad_handler_kwargs)},
            "text_output_queue": text_output_queue,
            "speculative_turns": speculative_turns,
            "response_playing": response_playing,
        },
        defer_setup=True,
    )

    def context(queue_in: Queue[Any], queue_out: Queue[Any]) -> HandlerContext:
        return HandlerContext(
            stop_event=stop_event,
            queue_in=queue_in,
            queue_out=queue_out,
            text_output_queue=text_output_queue,
            should_listen=should_listen,
            cancel_scope=cancel_scope,
            speculative_turns=speculative_turns,
            pipeline_index=0,
            sample_rate=vad_handler_kwargs.sample_rate,
        )

    stt = create_backend_handler(stt_backend, context(spoken_prompt_queue, stt_output_queue))
    notifier = TranscriptionNotifier(
        stop_event,
        queue_in=stt_output_queue,
        queue_out=text_prompt_queue,
        setup_kwargs={"text_output_queue": text_output_queue, "should_listen": should_listen},
        defer_setup=True,
    )
    llm = create_backend_handler(llm_backend, context(text_prompt_queue, lm_response_queue))
    processor = LMOutputProcessor(
        stop_event,
        queue_in=lm_response_queue,
        queue_out=lm_processed_queue,
        setup_kwargs={"text_output_queue": text_output_queue, "speculative_turns": speculative_turns},
        defer_setup=True,
    )
    tts = create_backend_handler(tts_backend, context(lm_processed_queue, send_audio_chunks_queue))
    return [vad, stt, notifier, llm, processor, tts]


def _build_pipeline_unit(
    *,
    index: int,
    stop_event: Event,
    module_kwargs: ModuleArguments,
    vad_handler_kwargs: VADHandlerArguments,
    stt_backend: BackendSelection,
    llm_backend: BackendSelection,
    tts_backend: BackendSelection,
) -> PipelineUnit:
    from chatbot.api.openai_realtime.service import RealtimeService

    # Kept set for the whole session. Full-duplex barge-in: VAD never mutes
    # while TTS plays. Echo suppression is client AEC + Silero, not this Event.
    should_listen = Event()
    response_playing = Event()
    cancel_scope = CancelScope()
    speculative_turns = SpeculativeTurnTracker()
    recv_audio: Queue[AudioInItem] = Queue()
    send_audio: Queue[AudioOutItem] = Queue()
    spoken_prompt: Queue[VADOutItem] = Queue()
    stt_output: Queue[STTOutItem] = Queue()
    text_prompt: Queue[TextPromptItem] = Queue()
    lm_response: Queue[LMOutItem] = Queue()
    lm_processed: Queue[TTSInItem] = Queue()
    text_output: Queue[TextEventItem] = Queue()

    vad_kwargs = deepcopy(vad_handler_kwargs)
    llm_config = llm_backend.copy_for_pipeline()
    service = RealtimeService(
        text_prompt_queue=text_prompt,
        should_listen=should_listen,
        chat_size=llm_config.config.get("chat_size", 20),
        speculative_turns=speculative_turns,
        default_instructions=llm_config.config.get("init_chat_prompt"),
    )
    handlers = _build_handlers(
        stop_event=stop_event,
        should_listen=should_listen,
        recv_audio_chunks_queue=recv_audio,
        spoken_prompt_queue=spoken_prompt,
        stt_output_queue=stt_output,
        text_prompt_queue=text_prompt,
        lm_response_queue=lm_response,
        lm_processed_queue=lm_processed,
        send_audio_chunks_queue=send_audio,
        text_output_queue=text_output,
        module_kwargs=module_kwargs,
        vad_handler_kwargs=vad_kwargs,
        stt_backend=stt_backend.copy_for_pipeline(),
        llm_backend=llm_config,
        tts_backend=tts_backend.copy_for_pipeline(),
        speculative_turns=speculative_turns,
        cancel_scope=cancel_scope,
        response_playing=response_playing,
    )
    ready_gate = PipelineReady(len(handlers))
    for handler in handlers:
        handler.pipeline_index = 0
        handler.ready_callback = ready_gate.mark
    return PipelineUnit(
        index=0,
        service=service,
        cancel_scope=cancel_scope,
        should_listen=should_listen,
        response_playing=response_playing,
        input_queue=recv_audio,
        output_queue=send_audio,
        text_output_queue=text_output,
        text_prompt_queue=text_prompt,
        handlers=handlers,
        ready_gate=ready_gate,
    )


def build_pipeline(args: ParsedArguments, stop_event: Event, *, host: str | None = None) -> ThreadManager:
    from chatbot.api.openai_realtime.server import RealtimeServer

    unit = _build_pipeline_unit(
        index=0,
        stop_event=stop_event,
        module_kwargs=args.module_kwargs,
        vad_handler_kwargs=args.vad_handler_kwargs,
        stt_backend=args.stt_backend,
        llm_backend=args.llm_backend,
        tts_backend=args.tts_backend,
    )
    server = RealtimeServer(
        stop_event=stop_event,
        unit=unit,
        host=host or args.realtime_server_kwargs.host,
        port=args.realtime_server_kwargs.port,
    )
    # Bind HTTP first so /health is reachable while models load on handler threads.
    return ThreadManager([server, *unit.handlers])


def run_pipeline_command(command: Literal["serve"], argv: Sequence[str]) -> None:
    args = parse_arguments(argv, command=command)
    setup_logger(args.module_kwargs.log_level)
    stop_event = Event()
    manager = build_pipeline(args, stop_event)
    shutdown_requested = False

    def stop(_sig: int, _frame: Optional[FrameType]) -> None:
        nonlocal shutdown_requested
        if not shutdown_requested:
            shutdown_requested = True
            console.print("\n[yellow]Shutting down gracefully...[/yellow]")
            manager.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        manager.start()
        manager.wait()
    except KeyboardInterrupt:
        stop(signal.SIGINT, None)


def main() -> None:
    from chatbot.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
