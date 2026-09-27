import re
from typing import Optional

SMART_PUNCT_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
    }
)

SPEECHABLE_PATTERN = re.compile(
    r"[^\w\s.,!?;:'\"\-()\/\\@#%&*+=$€£¥₹₽¢\[\]{}<>~`^|…—–，。！？；：、\n\r\t]",
    flags=re.UNICODE,
)
MARKDOWN_LIST_PREFIX = re.compile(r"(^|\n)\s*[-+]\s+")
CLAUSE_LIST_PREFIX = re.compile(r"\.\s+[-+]\s+")


def split_sentences(text: str) -> list[str]:
    """Sentence-split *text* for streaming TTS."""
    from nltk import sent_tokenize

    return [sentence.strip() for sentence in sent_tokenize(text) if sentence.strip()]


def remove_unspeechable(text: str) -> str:
    """Keep only speechable characters: letters, digits, punctuation, whitespace.
    support unicode characters (english, arabic, chinese, japanese, korean, etc.)

    Markdown emphasis (``**bold**``, ``*italic*``) is stripped because TTS reads
    ``*`` as the word "asterisk". Backticks are dropped for the same reason.
    """
    text = text.translate(SMART_PUNCT_TRANSLATION)
    text = MARKDOWN_LIST_PREFIX.sub(r"\1", text)
    text = CLAUSE_LIST_PREFIX.sub(". ", text)
    text = text.replace("*", "").replace("`", "")
    return SPEECHABLE_PATTERN.sub("", text)


# Maps an STT language code to the language name used in the "Please reply ... in {name}"
# prompt. Every language any bundled STT backend can report needs an entry here, otherwise
# `--enable_lang_prompt` silently emits no instruction for it. The names are lowercase
# because they are interpolated mid-sentence.
#
# `tests/test_llm_utils.py` asserts this covers the SUPPORTED_LANGUAGES of every bundled STT
# handler, so adding a language to a handler without adding it here fails CI.
WHISPER_LANGUAGE_TO_LLM_LANGUAGE = {
    "en": "english",
    "fr": "french",
    "es": "spanish",
    "ja": "japanese",
    "ko": "korean",
    "hi": "hindi",
    "de": "german",
    "pt": "portuguese",
    "pl": "polish",
    "it": "italian",
    "nl": "dutch",
    # The remaining languages the STT backend can report.
    "ru": "russian",
    "uk": "ukrainian",
    "cs": "czech",
    "sk": "slovak",
    "hu": "hungarian",
    "ro": "romanian",
    "bg": "bulgarian",
    "hr": "croatian",
    "sl": "slovenian",
    "sr": "serbian",
    "da": "danish",
    "no": "norwegian",
    "sv": "swedish",
    "fi": "finnish",
    "et": "estonian",
    "lv": "latvian",
    "lt": "lithuanian",
    # Reported by the STT backend but previously unnamed here, which made
    # --enable_lang_prompt silently emit nothing for them.
    "ar": "arabic",
    "fa": "persian",
    "fil": "filipino",
    "id": "indonesian",
    "mk": "macedonian",
    "ms": "malay",
    "th": "thai",
    "tr": "turkish",
    "vi": "vietnamese",
    # Locales the on-device macOS engine transcribes that no hosted backend
    # here ever offered. Cantonese gets its own entry because the engine
    # reports it separately from Mandarin.
    "bn": "bengali",
    "gu": "gujarati",
    "kn": "kannada",
    "ks": "kashmiri",
    "mai": "maithili",
    "ml": "malayalam",
    "mr": "marathi",
    "ne": "nepali",
    "or": "odia",
    "pa": "punjabi",
    "ta": "tamil",
    "te": "telugu",
    "ur": "urdu",
}


def resolve_auto_language(language_code: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Strip the ``-auto`` suffix and resolve the human-readable language name.

    Returns ``(clean_code, language_name)``.  ``language_name`` is non-None
    when the code (with or without ``-auto``) maps to a known language.
    """
    if not language_code:
        return language_code, None
    if language_code.endswith("-auto"):
        language_code = language_code[:-5]
    if language_code not in WHISPER_LANGUAGE_TO_LLM_LANGUAGE:
        return language_code, None
    return language_code, WHISPER_LANGUAGE_TO_LLM_LANGUAGE.get(language_code)
