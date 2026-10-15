"""Final-hop Bailian Bridge tests with synthetic secrets and an in-memory transport."""

from __future__ import annotations

import json
import unittest
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from knowledge_system.infrastructure.model_gateway.bridge import (
    BailianEgressBridge,
    ModelEgressBridgeError,
)
from knowledge_system.modules.model_gateway.application.egress import (
    EgressAdmissionDecisionV1,
    EgressAdmissionOutcome,
    EgressAdmissionRequestV1,
    EgressExecutionPermitV1,
)
from knowledge_system.modules.model_gateway.application.secrets import (
    ProviderSecretReadRequestV1,
)
from knowledge_system.modules.model_gateway.domain.chat_digest import (
    ProviderChatRequestV1,
    digest_provider_chat_request,
)
from knowledge_system.modules.model_gateway.domain.chat_request import (
    ChatJsonSchemaV1,
    ChatMessageV1,
    ChatResponseFormatV1,
)

SECRET_REF = "openbao-kv-v2://knowledge-model/model-provider/" + "a" * 64


def _request(content: str = "public development input") -> ProviderChatRequestV1:
    return ProviderChatRequestV1(
        model="qwen3.8-max-0902",
        messages=(ChatMessageV1(role="user", content=content),),
        temperature=0.0,
        top_p=1.0,
        max_tokens=128,
        response_format=ChatResponseFormatV1(
            json_schema=ChatJsonSchemaV1(
                name="goal_plan",
                schema={
                    "type": "object",
                    "properties": {"goal": {"type": "string"}},
                    "required": ["goal"],
                    "additionalProperties": False,
                },
            )
        ),
    )


def _permit(model: str = "qwen3.8-max-0902") -> EgressExecutionPermitV1:
    return EgressExecutionPermitV1(
        provider="alibaba_bailian",
        provider_model=model,
        secret_ref=SECRET_REF,
        secret_version=1,
    )


class _Admission:
    def __init__(self, decision: EgressAdmissionDecisionV1) -> None:
        self.decision = decision
        self.requests: list[EgressAdmissionRequestV1] = []

    async def admit(self, *, request: EgressAdmissionRequestV1) -> EgressAdmissionDecisionV1:
        self.requests.append(request)
        return self.decision


class _Broker:
    def __init__(self) -> None:
        self.requests: list[ProviderSecretReadRequestV1] = []
        self.issued: list[bytearray] = []

    async def read_provider_key(self, request: ProviderSecretReadRequestV1) -> bytearray:
        self.requests.append(request)
        value = bytearray(b"synthetic-provider-key-value")
        self.issued.append(value)
        return value


class ChatDigestTests(unittest.TestCase):
    def test_digest_is_deterministic_and_input_sensitive(self) -> None:
        first = digest_provider_chat_request(_request())
        second = digest_provider_chat_request(_request())
        changed = digest_provider_chat_request(_request("other public input"))
        self.assertEqual(first, second)
        self.assertNotEqual(first.input_digest, changed.input_digest)
        self.assertEqual(first.parameters_digest, changed.parameters_digest)
        self.assertEqual(first.schema_digest, changed.schema_digest)

    def test_admitted_decision_requires_permit(self) -> None:
        with self.assertRaises(ValidationError):
            EgressAdmissionDecisionV1(outcome=EgressAdmissionOutcome.ADMITTED)
        admitted = EgressAdmissionDecisionV1(
            outcome=EgressAdmissionOutcome.ADMITTED, permit=_permit()
        )
        self.assertEqual(admitted.permit, _permit())


class BailianEgressBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_admit_then_broker_then_fixed_provider_stream(self) -> None:
        captured: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'data: {"choices":[]}\n\ndata: [DONE]\n\n',
            )

        admission = _Admission(
            EgressAdmissionDecisionV1(
                outcome=EgressAdmissionOutcome.ADMITTED,
                permit=_permit(),
            )
        )
        broker = _Broker()
        model_call_id = uuid4()
        bridge = BailianEgressBridge(
            provider_base_url=(
                "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/"
            ),
            admission=admission,
            secret_broker=broker,
            transport=httpx.MockTransport(handle),
        )
        output = b"".join(
            [
                chunk
                async for chunk in bridge.stream(
                    model_call_id=model_call_id,
                    route_ref="route/planning-strong/v1",
                    request=_request(),
                )
            ]
        )

        self.assertIn(b"[DONE]", output)
        self.assertEqual(len(admission.requests), 1)
        self.assertEqual(admission.requests[0].model_call_id, model_call_id)
        self.assertEqual(len(broker.requests), 1)
        self.assertEqual(broker.requests[0].secret_version, 1)
        self.assertEqual(captured[0].url.path, "/compatible-mode/v1/chat/completions")
        self.assertEqual(
            captured[0].headers["authorization"], "Bearer synthetic-provider-key-value"
        )
        self.assertEqual(json.loads(captured[0].content)["model"], "qwen3.8-max-0902")
        self.assertEqual(broker.issued[0], bytearray(len(b"synthetic-provider-key-value")))

    async def test_denial_and_model_mismatch_do_not_touch_broker(self) -> None:
        for decision in (
            EgressAdmissionDecisionV1(
                outcome=EgressAdmissionOutcome.REJECTED,
                reason_code="EGRESS_PROJECTION_MISMATCH:INPUT",
            ),
            EgressAdmissionDecisionV1(
                outcome=EgressAdmissionOutcome.ADMITTED,
                permit=_permit("other-model"),
            ),
        ):
            with self.subTest(outcome=decision.outcome):
                broker = _Broker()
                bridge = BailianEgressBridge(
                    provider_base_url=(
                        "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
                    ),
                    admission=_Admission(decision),
                    secret_broker=broker,
                    transport=httpx.MockTransport(lambda _: httpx.Response(500)),
                )
                with self.assertRaises(ModelEgressBridgeError):
                    async for _ in bridge.stream(
                        model_call_id=uuid4(),
                        route_ref="route/planning-strong/v1",
                        request=_request(),
                    ):
                        pass
                self.assertEqual(broker.requests, [])

    async def test_provider_error_body_never_escapes_and_key_is_zeroized(self) -> None:
        broker = _Broker()
        bridge = BailianEgressBridge(
            provider_base_url=(
                "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
            ),
            admission=_Admission(
                EgressAdmissionDecisionV1(
                    outcome=EgressAdmissionOutcome.ADMITTED,
                    permit=_permit(),
                )
            ),
            secret_broker=broker,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(401, text="sensitive provider response")
            ),
        )
        with self.assertRaisesRegex(ModelEgressBridgeError, "BAILIAN_RESPONSE_REJECTED") as raised:
            async for _ in bridge.stream(
                model_call_id=UUID("00000000-0000-0000-0000-000000000001"),
                route_ref="route/planning-strong/v1",
                request=_request(),
            ):
                pass
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertEqual(raised.exception.http_status, 401)
        self.assertEqual(broker.issued[0], bytearray(len(b"synthetic-provider-key-value")))

    def test_provider_url_is_fixed_bailian_compatible_path(self) -> None:
        admission = _Admission(
            EgressAdmissionDecisionV1(outcome=EgressAdmissionOutcome.ADMITTED, permit=_permit())
        )
        for value in (
            "http://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "https://ws-example.cn-beijing.maas.aliyuncs.com/v1",
            "https://evil.invalid/compatible-mode/v1",
            "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1?x=1",
        ):
            with self.subTest(value=value), self.assertRaises(ModelEgressBridgeError):
                BailianEgressBridge(
                    provider_base_url=value,
                    admission=admission,
                    secret_broker=_Broker(),
                )


if __name__ == "__main__":
    unittest.main()
