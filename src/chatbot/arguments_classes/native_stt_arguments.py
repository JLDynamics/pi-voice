from dataclasses import dataclass, field


@dataclass
class NativeSTTHandlerArguments:
    """Apple's on-device speech-to-text, via the macos/SpeechHelper binary."""

    native_stt_locale: str = field(
        default="en-US",
        metadata={
            "help": "Locale to transcribe, e.g. en-US, en-GB, zh-CN, yue-CN, ja-JP. The engine has no "
            "auto-detect, so this fixes the spoken language; `speech-helper --locales` lists what this "
            "Mac supports and what is already installed."
        },
    )
    native_stt_helper_path: str = field(
        default="",
        metadata={
            "help": "Path to the speech helper binary. Empty looks in this checkout "
            "(macos/SpeechHelper/build/speech-helper), then on PATH."
        },
    )
    native_stt_timeout_s: float = field(
        default=20.0,
        metadata={"help": "Per-turn timeout. A stuck helper falls back to an empty transcript, not a hang."},
    )
    native_stt_prepare_timeout_s: float = field(
        default=300.0,
        metadata={
            "help": "Startup budget for installing the locale's model. Only the first run of a new "
            "language spends this; afterwards the model is on disk."
        },
    )
