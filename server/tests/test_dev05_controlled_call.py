"""DEV-05 受控单次模型调用（批1）：Policy Adapter编排 + PydanticAI `GatewayModel`适配器离线验收。

以测试替身端口（Fake Ledger/Budget/Permit/Bifrost）驱动`ModelGatewayService.complete()`，验证
Prompt → 正确BifrostChatRequest → ModelCall账本（ACCEPTED→SENDING→STREAMING→终态）→ 预算预留/消耗/
结算 → 一次性出网许可 → Bifrost流归一 → 稳定文本 → 账本结算的完整生命周期；并以PydanticAI `Agent`
经`GatewayModel`适配器验证「Prompt → 受控计划装配 → 结构化输出」往返。不接真实模型、不接PG/OpenBao。
"""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel
from pydantic_ai import Agent, NativeOutput

from knowledge_system.infrastructure.model_gateway.gateway_model import GatewayModel
from knowledge_system.modules.memory.public import (
    ContextPackReuseHitV1,
    ContextPackStateV1,
    ContextPackTransitionOutcomeV1,
    ContextPackTransitionResultV1,
)
from knowledge_system.modules.model_gateway.application.budget import (
    BudgetReservationInsertDisposition,
    BudgetReservationInsertResult,
    BudgetReservationTransitionConflict,
    BudgetReservationTransitionResult,
)
from knowledge_system.modules.model_gateway.application.egress_permit_issuance import (
    EgressPermitIssuanceDisposition,
    EgressPermitIssuanceRequestV1,
    EgressPermitIssuanceResultV1,
)
from knowledge_system.modules.model_gateway.application.gateway import (
    ModelCallInsertDisposition,
    ModelCallInsertResult,
    ModelCallTransitionConflict,
    ModelCallTransitionResult,
)
from knowledge_system.modules.model_gateway.application.policy_adapter import ModelGatewayService
from knowledge_system.modules.model_gateway.domain.budget_ledger import BudgetLedgerEntryKind
from knowledge_system.modules.model_gateway.domain.budget_quantity import BudgetQuantityV1
from knowledge_system.modules.model_gateway.domain.budget_reservation import BudgetReservationStatus
from knowledge_system.modules.model_gateway.domain.call_state import ModelCallState
from knowledge_system.modules.model_gateway.domain.chat_digest import (
    ProviderChatRequestV1,
    digest_provider_chat_request,
)
from knowledge_system.modules.model_gateway.domain.chat_request import (
    ChatJsonSchemaV1,
    ChatMessageV1,
)
from knowledge_system.modules.model_gateway.domain.controlled_call import (
    ControlledCallFailure,
    ControlledCallPlanV1,
)
from knowledge_system.modules.model_gateway.domain.egress_admission import verify_egress_projection
from knowledge_system.modules.model_gateway.domain.model_call import ModelCallV1
from knowledge_system.modules.model_gateway.domain.provider_error import ProviderErrorSource
from knowledge_system.modules.model_gateway.domain.stream_event import (
    BifrostDeltaEventV1,
    BifrostErrorEventV1,
    BifrostEventV1,
    BifrostUsageEventV1,
)
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    EstimatedDevTokenAccountingV1,
)
from knowledge_system.modules.tasking.domain.core_types import canonical_json_sha256

RESERVED = BudgetQuantityV1(model_call_count=1, input_tokens=100, output_tokens=100)
ESTIMATED_ACCOUNTING = EstimatedDevTokenAccountingV1(
    mode="ESTIMATED_DEV",
    estimated=True,
    estimated_input_tokens=64,
    estimator_revision="est-1",
    serialized_request_bytes=1024,
    protocol_overhead_tokens=8,
)

PACK_HASH = "a" * 64
PACK_INPUT_DIGEST = "b" * 64


def make_schema() -> ChatJsonSchemaV1:
    return ChatJsonSchemaV1(
        name="goal_answer",
        schema={
            "type": "object",
            "properties": {"goal": {"type": "string"}},
            "required": ["goal"],
            "additionalProperties": False,
        },
    )


def make_plan(
    *,
    capability_alias: str = "planning_strong",
    model_call_id: UUID | None = None,
) -> ControlledCallPlanV1:
    return ControlledCallPlanV1(
        model_call_id=model_call_id or uuid4(),
        task_id=uuid4(),
        account_id=uuid4(),
        context_pack_id=uuid4(),
        context_pack_hash=PACK_HASH,
        context_pack_input_digest=PACK_INPUT_DIGEST,
        capability_alias=capability_alias,
        messages=(ChatMessageV1(role="user", content="public development input"),),
        max_tokens=128,
        response_schema=make_schema(),
        token_accounting=ESTIMATED_ACCOUNTING,
        reserved=RESERVED,
        prompt_version="v1",
        context_policy_version="v1",
        runtime_version="v1",
        agent_manifest_version="v1",
        authorization_version=1,
        execution_generation=1,
        cancellation_generation=0,
        expires_in_seconds=300,
    )


def _success_events() -> list[BifrostEventV1]:
    return [
        BifrostDeltaEventV1(type="DELTA", text_delta='{"goal":"answer"}', chunk_id="c1"),
        BifrostUsageEventV1(type="USAGE", input_tokens=10, output_tokens=5),
    ]


class FakeModelCallLedger:
    def __init__(self) -> None:
        self.calls: dict[UUID, ModelCallV1] = {}
        self.row_versions: dict[UUID, int] = {}
        self.transitions: list[tuple[ModelCallState, ModelCallState]] = []

    async def insert_accepted(self, call: ModelCallV1) -> ModelCallInsertResult:
        self.calls[call.model_call_id] = call
        self.row_versions[call.model_call_id] = 1
        return ModelCallInsertResult(
            disposition=ModelCallInsertDisposition.INSERTED, call=call, row_version=1
        )

    async def transition_status(
        self, model_call_id, *, from_status, to_status, expected_row_version, updates=None
    ):
        call = self.calls.get(model_call_id)
        if call is None:
            return ModelCallTransitionResult(
                committed=False, conflict_reason=ModelCallTransitionConflict.NOT_FOUND
            )
        if self.row_versions.get(model_call_id) != expected_row_version:
            return ModelCallTransitionResult(
                committed=False, conflict_reason=ModelCallTransitionConflict.ROW_VERSION_CONFLICT
            )
        if call.status != from_status:
            return ModelCallTransitionResult(
                committed=False, conflict_reason=ModelCallTransitionConflict.NOT_IN_STATE
            )
        update_fields = updates.model_dump(exclude_none=True) if updates is not None else {}
        new_call = call.model_copy(update={**update_fields, "status": to_status})
        self.calls[model_call_id] = new_call
        self.row_versions[model_call_id] = expected_row_version + 1
        self.transitions.append((from_status, to_status))
        return ModelCallTransitionResult(committed=True, call=new_call)

    async def get_by_id(self, model_call_id):
        return self.calls.get(model_call_id)

    async def get_by_provider_request_id(self, provider_request_id):
        return None


class FakeBudgetLedger:
    def __init__(self) -> None:
        self.reservations: dict[UUID, object] = {}
        self.row_versions: dict[UUID, int] = {}
        self.entries: list[object] = []

    async def insert_reserved(self, reservation, reserve_entry):
        self.reservations[reservation.reservation_id] = reservation
        self.row_versions[reservation.reservation_id] = 1
        self.entries.append(reserve_entry)
        return BudgetReservationInsertResult(
            disposition=BudgetReservationInsertDisposition.INSERTED,
            reservation=reservation,
            row_version=1,
        )

    async def transition_status(
        self, reservation_id, *, from_status, to_status, expected_row_version, settled=None
    ):
        reservation = self.reservations.get(reservation_id)
        if reservation is None:
            return BudgetReservationTransitionResult(
                committed=False,
                conflict_reason=BudgetReservationTransitionConflict.NOT_FOUND,
            )
        if self.row_versions.get(reservation_id) != expected_row_version:
            return BudgetReservationTransitionResult(
                committed=False,
                conflict_reason=BudgetReservationTransitionConflict.ROW_VERSION_CONFLICT,
            )
        if reservation.status != from_status:
            return BudgetReservationTransitionResult(
                committed=False,
                conflict_reason=BudgetReservationTransitionConflict.NOT_IN_STATE,
            )
        update = {"status": to_status}
        if settled is not None:
            update["settled"] = settled
        new_reservation = reservation.model_copy(update=update)
        self.reservations[reservation_id] = new_reservation
        self.row_versions[reservation_id] = expected_row_version + 1
        return BudgetReservationTransitionResult(committed=True, reservation=new_reservation)

    async def append_entry(self, entry):
        self.entries.append(entry)
        return entry

    async def get_by_id(self, reservation_id):
        return self.reservations.get(reservation_id)

    async def get_by_model_call_id(self, model_call_id):
        for reservation in self.reservations.values():
            if reservation.model_call_id == model_call_id:
                return reservation
        return None

    async def list_entries(self, reservation_id):
        return [e for e in self.entries if e.reservation_id == reservation_id]


class FakePermitIssuer:
    def __init__(self) -> None:
        self.requests: list[EgressPermitIssuanceRequestV1] = []

    async def issue_ready(self, request) -> EgressPermitIssuanceResultV1:
        self.requests.append(request)
        return EgressPermitIssuanceResultV1(
            disposition=EgressPermitIssuanceDisposition.INSERTED
        )


class FakeBifrost:
    def __init__(self, events: list[BifrostEventV1] | None = None) -> None:
        self.events = events if events is not None else _success_events()
        self.calls: list[object] = []

    async def stream(
        self, *, model_call_id, trace_id, route_ref, request
    ) -> AsyncIterator[BifrostEventV1]:
        self.calls.append(request)
        for event in self.events:
            yield event


class FakeContextPacks:
    def __init__(self) -> None:
        self.consumed: list[UUID] = []

    async def find_by_id(self, *, pack_id):
        return ContextPackReuseHitV1(
            id=pack_id,
            task_id=uuid4(),
            state=ContextPackStateV1.READY,
            pack_hash=PACK_HASH,
            input_digest=PACK_INPUT_DIGEST,
            input_tokens=64,
            body_artifact_id=uuid4(),
            created_at=datetime.now(UTC),
        )

    async def mark_consumed(self, *, pack_id, consumed_at):
        del consumed_at
        self.consumed.append(pack_id)
        return ContextPackTransitionResultV1(
            state=ContextPackStateV1.CONSUMED,
            outcome=ContextPackTransitionOutcomeV1.TRANSITIONED,
        )


class FakeTransaction:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def make_service(
    bifrost: FakeBifrost | None = None,
    context_packs: FakeContextPacks | None = None,
):
    ledger = FakeModelCallLedger()
    budget = FakeBudgetLedger()
    permit = FakePermitIssuer()
    bifrost = bifrost or FakeBifrost()
    transaction = FakeTransaction()
    context_packs = context_packs or FakeContextPacks()
    service = ModelGatewayService(
        ledger=ledger,
        budget=budget,
        permit_issuer=permit,
        bifrost=bifrost,
        transactions=transaction,
        context_packs=context_packs,
    )
    return service, ledger, budget, permit, bifrost, transaction


class GoalAnswer(BaseModel):
    goal: str


class PolicyAdapterLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_success_lifecycle(self) -> None:
        plan = make_plan()
        service, ledger, budget, permit, bifrost, transaction = make_service()
        result = await service.complete(plan)

        self.assertEqual(transaction.commits, 2)
        self.assertEqual(transaction.rollbacks, 0)
        self.assertEqual(result.status, ModelCallState.COMPLETED)
        self.assertEqual(result.text, '{"goal":"answer"}')
        self.assertIsNone(result.error_code)
        self.assertEqual(result.usage.kind, "EXACT")
        self.assertEqual(result.usage.input_tokens, 10)
        self.assertEqual(result.usage.output_tokens, 5)

        call = ledger.calls[plan.model_call_id]
        self.assertEqual(call.status, ModelCallState.COMPLETED)
        self.assertEqual(call.approved_model_id, "qwen3.8-max-0902")
        self.assertEqual(call.actual_model_id, "qwen3.8-max-0902")
        self.assertEqual(call.response_hash, canonical_json_sha256('{"goal":"answer"}'))
        self.assertEqual(
            ledger.transitions,
            [
                (ModelCallState.ACCEPTED, ModelCallState.SENDING),
                (ModelCallState.SENDING, ModelCallState.STREAMING),
                (ModelCallState.STREAMING, ModelCallState.COMPLETED),
            ],
        )

        reservation = budget.reservations[call.budget_reservation_id]
        self.assertEqual(reservation.status, BudgetReservationStatus.SETTLED)
        self.assertEqual([e.entry_kind for e in budget.entries], [
            BudgetLedgerEntryKind.RESERVE,
            BudgetLedgerEntryKind.SETTLE,
            BudgetLedgerEntryKind.RETURN,
        ])
        reserve_entry, settle_entry, return_entry = budget.entries
        self.assertEqual(reserve_entry.delta, RESERVED.as_delta())
        self.assertEqual(settle_entry.delta.input_tokens, 10)
        self.assertEqual(settle_entry.delta.output_tokens, 5)
        self.assertEqual(return_entry.delta.input_tokens, -90)
        self.assertEqual(return_entry.delta.output_tokens, -95)

        self.assertEqual(len(permit.requests), 1)
        permit_request = permit.requests[0]
        self.assertEqual(permit_request.route_ref, "development/planning_strong")
        self.assertEqual(permit_request.provider, "alibaba_bailian")
        self.assertEqual(permit_request.provider_model, "qwen3.8-max-0902")
        self.assertEqual(permit_request.model_call_id, plan.model_call_id)

        self.assertEqual(len(bifrost.calls), 1)
        bifrost_request = bifrost.calls[0]
        self.assertEqual(bifrost_request.model, "bailian-bridge/qwen3.8-max-0902")
        self.assertEqual(bifrost_request.max_tokens, 128)
        self.assertEqual(bifrost_request.temperature, 0)
        self.assertEqual(bifrost_request.messages, plan.messages)

    async def test_complete_consumes_context_pack_before_accept(self) -> None:
        packs = FakeContextPacks()
        service, ledger, _budget, _permit, _bifrost, _transaction = make_service(
            context_packs=packs
        )
        plan = make_plan()
        result = await service.complete(plan)

        self.assertEqual(result.status, ModelCallState.COMPLETED)
        self.assertEqual(packs.consumed, [plan.context_pack_id])
        call = ledger.calls[plan.model_call_id]
        self.assertEqual(call.context_pack_hash, PACK_HASH)
        self.assertEqual(call.context_pack_input_digest, PACK_INPUT_DIGEST)

    async def test_permit_body_digest_matches_frozen_projection(self) -> None:
        plan = make_plan()
        service, _ledger, _budget, permit, bifrost, _transaction = make_service()
        await service.complete(plan)

        request = bifrost.calls[0]
        provider_request = ProviderChatRequestV1(
            model="qwen3.8-max-0902",
            messages=request.messages,
            stream=request.stream,
            stream_options=request.stream_options,
            temperature=request.temperature,
            top_p=request.top_p,
            max_tokens=request.max_tokens,
            response_format=request.response_format,
        )
        expected = digest_provider_chat_request(provider_request)
        self.assertEqual(permit.requests[0].body, expected)
        self.assertIsNone(
            verify_egress_projection(approved=permit.requests[0].body, actual=expected)
        )

    async def test_mid_stream_error_fails_call(self) -> None:
        bifrost = FakeBifrost(
            [
                BifrostDeltaEventV1(type="DELTA", text_delta='{"goal":"partial"}', chunk_id="c1"),
                BifrostErrorEventV1(type="ERROR", http_status=503),
            ]
        )
        service, ledger, budget, _permit, _bifrost, _transaction = make_service(bifrost=bifrost)
        plan = make_plan()
        result = await service.complete(plan)

        self.assertEqual(result.status, ModelCallState.FAILED)
        self.assertEqual(result.error_code, "MODEL_STREAM_ABORTED")
        call = ledger.calls[plan.model_call_id]
        self.assertEqual(call.status, ModelCallState.FAILED)
        self.assertEqual(
            ledger.transitions,
            [
                (ModelCallState.ACCEPTED, ModelCallState.SENDING),
                (ModelCallState.SENDING, ModelCallState.STREAMING),
                (ModelCallState.STREAMING, ModelCallState.FAILED),
            ],
        )
        reservation = budget.reservations[call.budget_reservation_id]
        self.assertEqual(reservation.status, BudgetReservationStatus.SETTLED)
        self.assertEqual([e.entry_kind for e in budget.entries], [
            BudgetLedgerEntryKind.RESERVE,
            BudgetLedgerEntryKind.WORST_CASE_SETTLE,
        ])

    async def test_pre_stream_connection_error_unknown(self) -> None:
        bifrost = FakeBifrost(
            [BifrostErrorEventV1(type="ERROR", source=ProviderErrorSource.CONNECTION)]
        )
        service, ledger, budget, _permit, _bifrost, _transaction = make_service(bifrost=bifrost)
        plan = make_plan()
        result = await service.complete(plan)

        self.assertEqual(result.status, ModelCallState.UNKNOWN)
        self.assertIsNone(result.error_code)
        call = ledger.calls[plan.model_call_id]
        self.assertEqual(call.status, ModelCallState.UNKNOWN)
        self.assertEqual(
            ledger.transitions,
            [
                (ModelCallState.ACCEPTED, ModelCallState.SENDING),
                (ModelCallState.SENDING, ModelCallState.UNKNOWN),
            ],
        )
        reservation = budget.reservations[call.budget_reservation_id]
        self.assertEqual(reservation.status, BudgetReservationStatus.SETTLED)
        self.assertEqual([e.entry_kind for e in budget.entries], [
            BudgetLedgerEntryKind.RESERVE,
            BudgetLedgerEntryKind.WORST_CASE_SETTLE,
        ])

    async def test_unregistered_binding_fails_closed(self) -> None:
        service, ledger, _budget, _permit, _bifrost, _transaction = make_service()
        plan = make_plan(capability_alias="nonexistent_alias")
        with self.assertRaises(ControlledCallFailure) as caught:
            await service.complete(plan)
        self.assertEqual(caught.exception.code, "MODEL_BINDING_NOT_REGISTERED")
        self.assertEqual(len(ledger.calls), 0)


class GatewayModelAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_round_trip_assembles_plan_and_returns_text(self) -> None:
        service, ledger, _budget, _permit, _bifrost, _transaction = make_service()
        gateway = GatewayModel(
            service,
            capability_alias="planning_strong",
            task_id=uuid4(),
            account_id=uuid4(),
            context_pack_id=uuid4(),
            context_pack_hash=PACK_HASH,
            context_pack_input_digest=PACK_INPUT_DIGEST,
            token_accounting=ESTIMATED_ACCOUNTING,
            reserved=RESERVED,
            prompt_version="v1",
            context_policy_version="v1",
            runtime_version="v1",
            agent_manifest_version="v1",
            authorization_version=1,
            execution_generation=1,
            cancellation_generation=0,
            expires_in_seconds=300,
        )
        agent = Agent(
            gateway,
            name="controlled-call",
            output_type=NativeOutput(GoalAnswer, strict=True),
            tools=[],
        )
        agent.instrument = False
        output = await agent.run("public development input")

        self.assertEqual(output.output, GoalAnswer(goal="answer"))
        self.assertEqual(len(ledger.calls), 1)
        only_call = next(iter(ledger.calls.values()))
        self.assertEqual(only_call.status, ModelCallState.COMPLETED)
        self.assertEqual(only_call.capability_alias, "planning_strong")

    async def test_non_completed_terminal_raises_failure(self) -> None:
        bifrost = FakeBifrost(
            [BifrostErrorEventV1(type="ERROR", source=ProviderErrorSource.CONNECTION)]
        )
        service, _ledger, _budget, _permit, _bifrost, _transaction = make_service(bifrost=bifrost)
        gateway = GatewayModel(
            service,
            capability_alias="planning_strong",
            task_id=uuid4(),
            account_id=uuid4(),
            context_pack_id=uuid4(),
            context_pack_hash=PACK_HASH,
            context_pack_input_digest=PACK_INPUT_DIGEST,
            token_accounting=ESTIMATED_ACCOUNTING,
            reserved=RESERVED,
            prompt_version="v1",
            context_policy_version="v1",
            runtime_version="v1",
            agent_manifest_version="v1",
            authorization_version=1,
            execution_generation=1,
            cancellation_generation=0,
            expires_in_seconds=300,
        )
        agent = Agent(
            gateway,
            name="controlled-call",
            output_type=NativeOutput(GoalAnswer, strict=True),
            tools=[],
        )
        agent.instrument = False
        with self.assertRaises(ControlledCallFailure) as caught:
            await agent.run("public development input")
        self.assertEqual(caught.exception.code, "MODEL_CALL_NOT_COMPLETED")


if __name__ == "__main__":
    unittest.main()
