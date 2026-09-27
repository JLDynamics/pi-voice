import importlib

import pytest

from chatbot.LLM.utils import (
    WHISPER_LANGUAGE_TO_LLM_LANGUAGE,
    remove_unspeechable,
    resolve_auto_language,
    split_sentences,
)


def test_split_sentences_matches_nltk_for_english() -> None:
    text = "First one. Second one? Third"
    assert split_sentences(text) == ["First one.", "Second one?", "Third"]


def test_split_sentences_keeps_substrings_for_the_streaming_loop() -> None:
    text = "Huawei is big. It was founded in 1987. Did you know?"
    sentences = split_sentences(text)
    assert sentences == ["Huawei is big.", "It was founded in 1987.", "Did you know?"]
    # The streaming loop finds the tail by substring search.
    assert all(sentence in text for sentence in sentences)
    assert split_sentences("") == []
    assert split_sentences("   ") == []


def test_remove_unspeechable_normalizes_smart_apostrophes() -> None:
    assert remove_unspeechable("I’ll reply if here’s the plan.") == "I'll reply if here's the plan."


def test_remove_unspeechable_keeps_text_and_drops_emoji() -> None:
    assert remove_unspeechable("Hello 👋 lobster 🦞") == "Hello  lobster "


def test_remove_unspeechable_keeps_chinese_punctuation() -> None:
    text = "你好，今天怎么样？很好！停顿；说明：一、二。"
    assert remove_unspeechable(text) == text


def test_remove_unspeechable_strips_markdown_asterisks() -> None:
    assert remove_unspeechable("**Mistral AI**") == "Mistral AI"
    assert remove_unspeechable("**OpenAI** is claiming") == "OpenAI is claiming"
    assert (
        remove_unspeechable("**NVIDIA** is in the news about acquiring **Hugging Face**")
        == "NVIDIA is in the news about acquiring Hugging Face"
    )
    assert remove_unspeechable("**Coca-Cola** is using AI") == "Coca-Cola is using AI"
    assert remove_unspeechable("*italic*") == "italic"


def test_remove_unspeechable_strips_asterisks_split_across_chunks() -> None:
    assert remove_unspeechable("**Mis") == "Mis"
    assert remove_unspeechable("tral AI**") == "tral AI"


def test_remove_unspeechable_strips_markdown_list_markers() -> None:
    assert remove_unspeechable("- **Mistral AI** — the French AI company") == "Mistral AI — the French AI company"
    assert (
        remove_unspeechable("It goes through. - On the robotics side, **Arm** launched")
        == "It goes through. On the robotics side, Arm launched"
    )


# --- language name coverage ---------------------------------------------------------------
#
# A language code with no entry in WHISPER_LANGUAGE_TO_LLM_LANGUAGE resolves to a `None`
# language name, and both LLM backends gate the prompt on it:
#
#     if lang_name and self.enable_lang_prompt:
#         active_chat.add_item(make_user_message(f"Please reply to my message in {lang_name}."))
#
# so `--enable_lang_prompt` silently emits nothing for that language.

# native-stt is the single retained STT backend.
_STT_HANDLER_MODULES = ["chatbot.STT.native_stt_handler"]
_ALWAYS_IMPORTABLE = {"chatbot.STT.native_stt_handler"}


def _supported_languages(module_name):
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    return list(module.SUPPORTED_LANGUAGES)


@pytest.mark.parametrize("module_name", _STT_HANDLER_MODULES)
def test_every_stt_language_has_an_llm_language_name(module_name):
    """Any language a bundled STT backend can report must be nameable for the prompt."""
    languages = _supported_languages(module_name)
    if languages is None:
        if module_name in _ALWAYS_IMPORTABLE:
            pytest.fail(f"{module_name} should be importable without optional extras")
        pytest.skip(f"{module_name} requires an optional dependency")

    missing = sorted(code for code in languages if code not in WHISPER_LANGUAGE_TO_LLM_LANGUAGE)
    assert missing == [], (
        f"{module_name} can report {missing}, which have no entry in "
        f"WHISPER_LANGUAGE_TO_LLM_LANGUAGE, so --enable_lang_prompt would emit no "
        f"instruction for them"
    )


def test_default_stt_languages_are_fully_covered():
    """Explicit guard for the default backend, independent of the parametrized sweep.

    Every code the STT can report needs a name here, or --enable_lang_prompt
    silently emits nothing for that language.
    """
    stt = importlib.import_module("chatbot.STT.native_stt_handler")

    missing = sorted(set(stt.SUPPORTED_LANGUAGES) - set(WHISPER_LANGUAGE_TO_LLM_LANGUAGE))
    assert not missing, f"no language name for: {missing}"


def test_language_names_are_lowercase_and_non_empty():
    """The name is interpolated mid-sentence, so it must read as lowercase prose."""
    for code, name in WHISPER_LANGUAGE_TO_LLM_LANGUAGE.items():
        assert name and name == name.lower(), f"{code} -> {name!r}"
        assert name.isalpha(), f"{code} -> {name!r}"


# --- resolve_auto_language ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("sv", ("sv", "swedish")),
        ("sv-auto", ("sv", "swedish")),
        ("ru-auto", ("ru", "russian")),
        ("no-auto", ("no", "norwegian")),
        ("lt", ("lt", "lithuanian")),
        ("en-auto", ("en", "english")),
    ],
)
def test_resolve_auto_language_names_stt_languages(code, expected):
    assert resolve_auto_language(code) == expected


@pytest.mark.parametrize("code", [None, ""])
def test_resolve_auto_language_passes_through_empty_codes(code):
    assert resolve_auto_language(code) == (code, None)


def test_resolve_auto_language_returns_no_name_for_unknown_code():
    """Unknown codes still round-trip the code, they just cannot be named."""
    assert resolve_auto_language("xx-auto") == ("xx", None)
