from dataclasses import dataclass, field


@dataclass
class ModuleArguments:
    """Options shared by the single supported browser voice pipeline."""

    stt: str = field(default="native-stt", metadata={"choices": ("native-stt",)})
    llm_backend: str = field(default="responses-api", metadata={"choices": ("responses-api",)})
    tts: str = field(default="siri", metadata={"choices": ("siri",)})
    log_level: str = field(default="info", metadata={"help": "Python logging level."})
