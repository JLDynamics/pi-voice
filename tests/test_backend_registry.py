from dataclasses import dataclass, fields

import pytest

from chatbot.arguments_classes.module_arguments import ModuleArguments
from chatbot.backend_registry import (
    LLM_BACKENDS,
    STT_BACKENDS,
    TTS_BACKENDS,
    BackendSpec,
    build_backend_registry,
    select_backend,
)


@dataclass
class FakeArguments:
    fake_option: str = "default"


def _factory(_context, config):
    return config


def test_registry_matches_the_only_supported_cli_choices():
    module_fields = {item.name: item for item in fields(ModuleArguments)}

    assert tuple(STT_BACKENDS) == module_fields["stt"].metadata["choices"] == ("native-stt",)
    assert tuple(LLM_BACKENDS) == module_fields["llm_backend"].metadata["choices"] == ("responses-api",)
    assert tuple(TTS_BACKENDS) == module_fields["tts"].metadata["choices"] == ("siri",)


def test_registry_rejects_duplicates_and_normalizes_prefixed_config():
    spec = BackendSpec("fake", "llm", FakeArguments, _factory, "fake")
    with pytest.raises(ValueError, match="Duplicate llm backend"):
        build_backend_registry("llm", [spec, spec])

    selection = select_backend({"fake": spec}, "fake", FakeArguments("chosen"))
    assert selection.config == {"option": "chosen", "gen_kwargs": {}}
