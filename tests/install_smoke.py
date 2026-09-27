from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess


def main() -> None:
    required = (
        "fastapi",
        "huggingface_hub",
        "lingua",
        "nltk",
        "numpy",
        "onnxruntime",
        "openai",
        "scipy",
        "torch",
        "torchaudio",
        "transformers",
        "uvicorn",
        "websockets",
    )
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError(f"Missing install-time modules: {', '.join(missing)}")

    root_help = subprocess.run(
        ["chatbot", "--help"],
        check=True,
        env={**os.environ, "OPENROUTER_API_KEY": ""},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ).stdout
    if "serve" not in root_help or "talk" in root_help or "local" in root_help:
        raise RuntimeError("Installed CLI does not expose the browser-only serve command.")

    from chatbot.arguments_classes.module_arguments import ModuleArguments
    from chatbot.arguments_classes.siri_tts_arguments import SiriTTSHandlerArguments

    modules = ModuleArguments()
    siri = SiriTTSHandlerArguments()
    assert (modules.stt, modules.llm_backend, modules.tts) == ("native-stt", "responses-api", "siri")
    assert siri.siri_tts_voice == "en-US-F"
    for module in (
        "chatbot.STT.native_stt_handler",
        "chatbot.LLM.responses_api_language_model",
        "chatbot.TTS.siri_tts_handler",
        "chatbot.api.openai_realtime.server",
    ):
        importlib.import_module(module)
    print("chatbot install smoke test passed")


if __name__ == "__main__":
    main()
