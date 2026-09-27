from dataclasses import dataclass, field


@dataclass
class SiriTTSHandlerArguments:
    """Apple's Siri voices, via the siri-tts helper binary."""

    siri_tts_voice: str = field(
        default="en-US-F",
        metadata={
            "help": "Siri voice name, e.g. en-US-F, aaron, damon, quinn (en-US) or arthur, en-GB-D (en-GB). "
            "`siri-tts voices --available` lists what this Mac has installed."
        },
    )
    siri_tts_binary: str = field(
        default="~/.local/bin/siri-tts",
        metadata={"help": "Path to the siri-tts binary (github.com/maximilianromer/siri-tts-cli)."},
    )
    siri_tts_timeout_s: float = field(
        default=60.0,
        metadata={"help": "Per-utterance ceiling. A stuck helper is killed rather than stalling the turn."},
    )
    # Siri's output is already clean, unlike the MLX models these defaults were
    # tuned for, so the denoise chain is off: running it would only soften a
    # signal that has no hiss to remove.
    siri_tts_gen_noise_gate: bool = field(
        default=False,
        metadata={"help": "Downward expander for model hiss. Off: Siri audio has no noise floor to hide."},
    )
    siri_tts_gen_spectral_denoise: bool = field(
        default=False,
        metadata={"help": "Spectral noise reduction. Off for the same reason as the noise gate."},
    )
