from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, fields
from importlib import import_module
from queue import Queue
from threading import Event
from typing import Any, Literal

from chatbot.arguments_classes.native_stt_arguments import NativeSTTHandlerArguments
from chatbot.arguments_classes.responses_api_language_model_arguments import (
    ResponsesApiLanguageModelHandlerArguments,
)
from chatbot.arguments_classes.siri_tts_arguments import SiriTTSHandlerArguments
from chatbot.pipeline.cancel_scope import CancelScope
from chatbot.pipeline.speculative_turns import SpeculativeTurnTracker

BackendKind = Literal["stt", "llm", "tts"]
BackendConfig = dict[str, Any]


@dataclass(frozen=True)
class HandlerContext:
    stop_event: Event
    queue_in: Queue[Any]
    queue_out: Queue[Any]
    text_output_queue: Queue[Any]
    should_listen: Event
    cancel_scope: CancelScope
    speculative_turns: SpeculativeTurnTracker
    pipeline_index: int
    sample_rate: int


HandlerFactory = Callable[[HandlerContext, Mapping[str, Any]], Any]


@dataclass(frozen=True)
class BackendSpec:
    name: str
    kind: BackendKind
    config_type: type[Any]
    create_handler: HandlerFactory
    config_prefix: str | None = None

    def normalize(self, config: Any) -> BackendConfig:
        if not isinstance(config, self.config_type):
            raise TypeError(f"Backend {self.name!r} expects {self.config_type.__name__}, got {type(config).__name__}.")
        return normalize_dataclass_config(config, self.config_prefix)


@dataclass(frozen=True)
class BackendSelection:
    spec: BackendSpec
    config: BackendConfig

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def kind(self) -> BackendKind:
        return self.spec.kind

    def copy_for_pipeline(self) -> BackendSelection:
        return BackendSelection(self.spec, deepcopy(self.config))


def normalize_dataclass_config(config: Any, prefix: str | None = None) -> BackendConfig:
    normalized: BackendConfig = {}
    generation: BackendConfig = {}
    marker = f"{prefix}_" if prefix else None
    for config_field in fields(config):
        name = config_field.name
        value = deepcopy(getattr(config, name))
        if marker and name.startswith(marker):
            name = name[len(marker) :]
        if name == "gen_kwargs" and isinstance(value, Mapping):
            generation.update(value)
        elif name.startswith("gen_"):
            generation[name[len("gen_") :]] = value
        else:
            normalized[name] = value
    normalized["gen_kwargs"] = generation
    return normalized


def build_backend_registry(kind: BackendKind, specs: Iterable[BackendSpec]) -> dict[str, BackendSpec]:
    registry: dict[str, BackendSpec] = {}
    for spec in specs:
        if spec.kind != kind:
            raise ValueError(f"Backend {spec.name!r} has kind {spec.kind!r}; expected {kind!r}.")
        if spec.name in registry:
            raise ValueError(f"Duplicate {kind} backend name: {spec.name!r}.")
        registry[spec.name] = spec
    return registry


def select_backend(registry: Mapping[str, BackendSpec], name: str, config: Any) -> BackendSelection:
    try:
        spec = registry[name]
    except KeyError as exc:
        choices = ", ".join(registry)
        raise ValueError(f"Unsupported backend {name!r}; choose one of: {choices}.") from exc
    return BackendSelection(spec, spec.normalize(config))


def create_backend_handler(selection: BackendSelection, context: HandlerContext) -> Any:
    return selection.spec.create_handler(context, selection.config)


def _load_handler(module_name: str, class_name: str) -> type[Any]:
    return getattr(import_module(module_name), class_name)


def _factory(
    module_name: str,
    class_name: str,
    *,
    should_listen: bool = False,
    runtime_context: bool = False,
    text_output_queue: bool = False,
) -> HandlerFactory:
    def create(context: HandlerContext, config: Mapping[str, Any]) -> Any:
        setup_kwargs = dict(config)
        if runtime_context:
            setup_kwargs.update(
                cancel_scope=context.cancel_scope,
                speculative_turns=context.speculative_turns,
            )
        if text_output_queue:
            setup_kwargs["text_output_queue"] = context.text_output_queue
        handler = _load_handler(module_name, class_name)(
            context.stop_event,
            queue_in=context.queue_in,
            queue_out=context.queue_out,
            setup_args=(context.should_listen,) if should_listen else (),
            setup_kwargs=setup_kwargs,
            defer_setup=True,
        )
        handler.speculative_turns = context.speculative_turns
        return handler

    return create


def _create_stt(module_name: str, class_name: str) -> HandlerFactory:
    """The STT handler takes no should_listen event and no runtime context."""

    def create(context: HandlerContext, config: Mapping[str, Any]) -> Any:
        handler = _load_handler(module_name, class_name)(
            context.stop_event,
            queue_in=context.queue_in,
            queue_out=context.queue_out,
            setup_kwargs={**config},
            defer_setup=True,
        )
        handler.speculative_turns = context.speculative_turns
        return handler

    return create


STT_BACKENDS = build_backend_registry(
    "stt",
    [
        BackendSpec(
            "native-stt",
            "stt",
            NativeSTTHandlerArguments,
            _create_stt("chatbot.STT.native_stt_handler", "NativeSTTHandler"),
            "native_stt",
        )
    ],
)
LLM_BACKENDS = build_backend_registry(
    "llm",
    [
        BackendSpec(
            "responses-api",
            "llm",
            ResponsesApiLanguageModelHandlerArguments,
            _factory(
                "chatbot.LLM.responses_api_language_model",
                "ResponsesApiModelHandler",
                runtime_context=True,
            ),
            "responses_api",
        )
    ],
)
TTS_BACKENDS = build_backend_registry(
    "tts",
    [
        BackendSpec(
            "siri",
            "tts",
            SiriTTSHandlerArguments,
            _factory(
                "chatbot.TTS.siri_tts_handler",
                "SiriTTSHandler",
                should_listen=True,
                runtime_context=True,
                text_output_queue=True,
            ),
            "siri_tts",
        ),
    ],
)
