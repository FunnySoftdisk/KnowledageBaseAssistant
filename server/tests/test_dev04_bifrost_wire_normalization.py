from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from knowledge_system.modules.model_gateway.domain.chat_digest import (
    ProviderChatRequestV1,
    digest_provider_chat_request,
)


def _wire_body() -> dict[str, object]:
    return {
        "model": "qwen3.7-plus-2026-05-26",
        "messages": [{"role": "user", "content": "public synthetic input"}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0.0,
        "top_p": 1.0,
        "max_completion_tokens": 128,
        "response_format": {
            "type": "json_schema",
            "strict": True,
            "json_schema": {
                "name": "probe",
                "schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            },
        },
    }


class BifrostWireNormalizationTests(unittest.TestCase):
    def test_normalizes_only_the_observed_bifrost_wire_shape(self) -> None:
        request = ProviderChatRequestV1.model_validate_json(json.dumps(_wire_body()), strict=True)
        normalized = request.model_dump(mode="json", by_alias=True)
        self.assertEqual(request.max_tokens, 128)
        self.assertEqual(normalized["max_completion_tokens"], 128)
        self.assertNotIn("max_tokens", normalized)
        self.assertNotIn("strict", normalized["response_format"])
        self.assertIs(normalized["response_format"]["json_schema"]["strict"], True)
        self.assertEqual(digest_provider_chat_request(request).model_id, request.model)

    def test_rejects_ambiguous_strict_locations(self) -> None:
        body = _wire_body()
        response_format = body["response_format"]
        assert isinstance(response_format, dict)
        json_schema = response_format["json_schema"]
        assert isinstance(json_schema, dict)
        json_schema["strict"] = True
        with self.assertRaises(ValidationError):
            ProviderChatRequestV1.model_validate_json(json.dumps(body), strict=True)

    def test_rejects_unknown_bifrost_wire_fields(self) -> None:
        body = _wire_body()
        body["provider_url"] = "https://evil.invalid"
        with self.assertRaises(ValidationError):
            ProviderChatRequestV1.model_validate_json(json.dumps(body), strict=True)


if __name__ == "__main__":
    unittest.main()
