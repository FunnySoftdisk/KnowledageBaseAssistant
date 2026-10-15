"""First runnable Chat Completion request contract for the controlled Bifrost path."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessageV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32768)


class ChatJsonSchemaV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    strict: Literal[True] = True
    schema_: dict[str, object] = Field(alias="schema")


class ChatResponseFormatV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["json_schema"] = "json_schema"
    json_schema: ChatJsonSchemaV1


class ChatStreamOptionsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    include_usage: Literal[True] = True


class BifrostChatRequestV1(BaseModel):
    """Policy Adapter output sent to Bifrost; no URL, provider key, fallback or retry knobs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    model: str = Field(
        min_length=3,
        max_length=256,
        pattern=r"^[a-z][a-z0-9_-]{1,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,191}$",
    )
    messages: tuple[ChatMessageV1, ...] = Field(min_length=1, max_length=64)
    stream: Literal[True] = True
    stream_options: ChatStreamOptionsV1 = Field(default_factory=ChatStreamOptionsV1)
    temperature: float = Field(ge=0, le=2)
    top_p: float = Field(gt=0, le=1)
    max_tokens: int = Field(gt=0, le=32768)
    response_format: ChatResponseFormatV1


__all__ = [
    "BifrostChatRequestV1",
    "ChatJsonSchemaV1",
    "ChatMessageV1",
    "ChatResponseFormatV1",
    "ChatStreamOptionsV1",
]
