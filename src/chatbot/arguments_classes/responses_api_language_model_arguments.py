from dataclasses import dataclass, field

from chatbot.arguments_classes.language_model_base_arguments import LanguageModelBaseArguments


@dataclass
class ResponsesApiLanguageModelHandlerArguments(LanguageModelBaseArguments):
    model_name: str = field(
        default="z-ai/glm-5.3-flash",
        metadata={"help": "OpenRouter Responses API model name."},
    )
    responses_api_stream: bool = field(
        default=True,
        metadata={
            "help": "The stream parameter typically indicates whether data should be transmitted in a continuous flow rather"
            " than in a single, complete response, often used for handling large or real-time data.Default is True"
        },
    )
    responses_api_disable_thinking: bool = field(
        default=True,
        metadata={
            "help": "Disable provider-side thinking/reasoning when supported by the OpenAI-compatible backend. "
            "For Together Qwen3.5 models this sends chat_template_kwargs.enable_thinking=false. Ignored when "
            "responses_api_reasoning_effort is set."
        },
    )
    responses_api_reasoning_effort: str | None = field(
        default="low",
        metadata={
            "help": "Reasoning effort sent as `reasoning.effort` (none, minimal, low, medium, high). Bounds how long "
            "the model deliberates before it starts speaking. Empty string disables the parameter. Default is low."
        },
    )
