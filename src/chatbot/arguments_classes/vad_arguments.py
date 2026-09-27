from dataclasses import dataclass, field


@dataclass
class VADHandlerArguments:
    thresh: float = field(
        default=0.6,
        metadata={
            "help": "The threshold value for voice activity detection (VAD). Values typically range from 0 to 1, with higher values requiring higher confidence in speech detection."
        },
    )
    sample_rate: int = field(
        default=16000,
        metadata={
            "help": "The sample rate of the audio in Hertz. Default is 16000 Hz, which is a common setting for voice audio."
        },
    )
    min_silence_ms: int = field(
        default=2000,
        metadata={
            "help": "Minimum length of silence intervals to be used for segmenting speech. Measured in milliseconds. Default is 2000 ms."
        },
    )
    min_speech_ms: int = field(
        default=384,
        metadata={
            "help": "Minimum length of speech segments to be considered valid speech. Measured in milliseconds. Default is 384 ms (softened barge-in: brief noises or the assistant's own echo won't interrupt a reply, but sustained speech still will)."
        },
    )
    min_speech_continuation_ms: int = field(
        default=192,
        metadata={
            "help": "Hysteresis threshold (ms of active speech) for accepting speech that continues a reopenable turn (soft-ended, uncommitted, within the reopen window). Set to 0 to disable the split and use min_speech_ms. Clamped to [100, min_speech_ms]. New turns and barge-ins always require min_speech_ms. Default: 192 (clamped against the min_speech_ms default of 500)."
        },
    )
    max_speech_ms: float = field(
        default=float("inf"),
        metadata={
            "help": "Maximum length of continuous speech before forcing a split. Default is infinite, allowing for uninterrupted speech segments."
        },
    )
    speech_pad_ms: int = field(
        default=500,
        metadata={
            "help": "Amount of audio retained before VAD triggers and prepended to detected speech segments. Once speech is detected, audio continues to be kept until VAD declares the segment done. Measured in milliseconds. Default is 500 ms."
        },
    )
    speculative_reopen_ms: int = field(
        default=800,
        metadata={
            "help": "Keep a soft-ended Realtime turn reopenable for this many milliseconds unless a response commits it. Default is 800 ms."
        },
    )
    reopen_complete_window_ms: int = field(
        default=-1,
        metadata={
            "help": "How long (ms) a soft-ended turn that Smart Turn scored complete stays reopenable by resumed speech. -1 (default) follows speculative_reopen_ms. 0 disables post-complete reopen entirely so later audio starts a new turn. Positive values override the grace. Turns Smart Turn scored incomplete always keep unanswered_reopen_ms so a mid-thought pause never orphans them. Shrink (or zero) this in a noisy room where late noise resurrects finished turns; widen it if quick afterthoughts get split into separate turns."
        },
    )
    reopen_complete_min_speech_ms: int = field(
        default=0,
        metadata={
            "help": "Active-speech floor (ms) for reopening a Smart-Turn-complete turn. 0 (default) reuses the fresh-turn bar (idle greeting floor / barge-in min_speech_ms): trailing audio after a finished utterance must qualify as a new utterance instead of riding the min_speech_continuation_ms hysteresis, which only bridges pauses inside an unfinished utterance. Set higher (e.g. 600) to tighten a noisy room without touching the hysteresis that incomplete turns rely on. Clamped to a 100 ms fragment floor."
        },
    )
    reopen_require_complete: bool = field(
        default=False,
        metadata={
            "help": 'Only let a reopened revision supersede a Smart-Turn-complete turn when the reopened audio itself scores Smart Turn complete. Off by default: it loses whole utterances. Reopening already marks the complete revision stale and its transcript is dropped, so suppressing the reopen as well leaves nothing — the turn is never answered until the user speaks again. Observed live: rev 10 scored complete (p=0.904), a trailing breath reopened it as rev 11, rev 10\'s transcript was dropped, rev 11 scored incomplete (p=0.288) and was suppressed, and 18 seconds passed before the user asked "can you hear me?". Enable only if a noisy room is corrupting committed transcripts, which is the smaller harm.'
        },
    )
    unanswered_reopen_ms: int = field(
        default=7000,
        metadata={
            "help": "Sanity cap (ms) for reopening a soft-ended speculative turn that has not yet been answered by any assistant output. While a turn is uncommitted, resumed speech within this window reopens the same turn instead of starting a new one. Has no effect below speculative_reopen_ms and is clamped to smart_turn_max_wait_ms when Smart Turn is enabled."
        },
    )
    short_segment_merge_ms: int = field(
        default=0,
        metadata={
            "help": "When greater than 0, adjacent VAD segments below min_speech_ms are held and stitched for this many milliseconds before being discarded. Fragments shorter than 100 ms of active speech are never held. Default is 0 ms."
        },
    )
    smart_turn: bool = field(
        default=True,
        metadata={
            "help": "Use Smart Turn v3.2 after Silero finalizes a Realtime turn to choose how long assistant output remains speculative. Enabled by default; pass --no_smart_turn to disable it."
        },
    )
    smart_turn_model_path: str | None = field(
        default=None,
        metadata={
            "help": "Optional path to a Smart Turn v3.x CPU ONNX model. When omitted, the latest supported v3.2 CPU model is downloaded from pipecat-ai/smart-turn-v3."
        },
    )
    smart_turn_threshold: float = field(
        default=0.5,
        metadata={
            "help": "Smart Turn completion probability threshold. Higher values wait more readily on ambiguous pauses. Default is 0.5."
        },
    )
    smart_turn_max_wait_ms: int = field(
        default=2000,
        metadata={
            "help": "Speculative reopen grace used when Smart Turn reports an incomplete turn. Resumed speech creates a newer turn revision; otherwise output may commit after this delay. Default is 2000 ms."
        },
    )
    smart_turn_incomplete_delay_ms: int = field(
        default=600,
        metadata={
            "help": "Delay STT and LLM processing after Smart Turn reports an incomplete turn, allowing resumed speech to invalidate the revision before expensive work begins. This delay runs within smart_turn_max_wait_ms; matching it means one generation per turn instead of a cancelled speculative one per pause. Default is 600 ms."
        },
    )
    smart_turn_cpu_count: int = field(
        default=1,
        metadata={"help": "Number of CPU threads ONNX Runtime may use for each Smart Turn inference. Default is 1."},
    )
