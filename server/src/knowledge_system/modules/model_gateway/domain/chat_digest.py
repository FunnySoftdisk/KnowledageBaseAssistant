"""Canonical request projections used by the final-hop egress admission check."""

from __future__ import annotations

import hashlib
from typing import Any

import rfc8785
from pydantic import AliasChoices, Field, field_validator

from .chat_request import BifrostChatRequestV1
from .egress_admission import EgressBodyDigestV1


class ProviderChatRequestV1(BifrostChatRequestV1):
    """Bifrost wire body normalized to the fixed Bailian request projection."""

    model: str = Field(
        min_length=1,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$",
    )
    max_tokens: int = Field(
        gt=0,
        le=32768,
        validation_alias=AliasChoices("max_completion_tokens", "max_tokens"),
        serialization_alias="max_completion_tokens",
    )

    @field_validator("response_format", mode="before")
    @classmethod
    def normalize_bifrost_response_format(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "strict" not in value:
            return value
        json_schema = value.get("json_schema")
        if not isinstance(json_schema, dict) or "strict" in json_schema:
            return value
        normalized_format = dict(value)
        normalized_schema = dict(json_schema)
        normalized_schema["strict"] = normalized_format.pop("strict")
        normalized_format["json_schema"] = normalized_schema
        return normalized_format


def _digest(value: Any) -> str:
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def digest_provider_chat_request(request: ProviderChatRequestV1) -> EgressBodyDigestV1:
    dumped = request.model_dump(mode="json", by_alias=True)
    parameters = {
        "stream": dumped["stream"],
        "stream_options": dumped["stream_options"],
        "temperature": dumped["temperature"],
        "top_p": dumped["top_p"],
        "max_completion_tokens": dumped["max_completion_tokens"],
    }
    return EgressBodyDigestV1(
        model_id=request.model,
        model_revision=request.model,
        parameters_digest=_digest(parameters),
        schema_digest=_digest(dumped["response_format"]),
        input_digest=_digest(dumped["messages"]),
    )


__all__ = ["ProviderChatRequestV1", "digest_provider_chat_request"]
