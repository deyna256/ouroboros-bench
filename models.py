"""The models that play the user and judge the dialogue."""

from convy import ChatJudge, Models, OpenAiModel

from gateway import gateway

models = Models(
    user=OpenAiModel(gateway, "openai/gpt-5.6-luna"),
    judge=ChatJudge(OpenAiModel(gateway, "openai/gpt-5.6-luna")),
)
