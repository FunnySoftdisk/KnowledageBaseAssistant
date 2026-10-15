"""Policy Adapter：受控单次模型调用的客户端编排。

`ModelGatewayService.complete()`把一次受控调用编排为：冻结开发绑定解析 → 试跑输入校验 →
Bifrost请求与Provider Body摘要冻结 → ModelCall账本（ACCEPTED→SENDING→STREAMING→终态）→
预算预留与结算 → 一次性出网许可签发 → Bifrost流归一 → 稳定文本与终态结算。所有持久化与网络
都经端口（`ModelCallLedger`/`BudgetLedger`/`EgressPermitIssuer`/`BifrostStreamPort`）注入，实现
位于基础设施层；本服务不持Secret、不选Provider、不做授权。

真实PG下预流持久化（ACCEPTED→SENDING + RESERVED→CONSUMING）必须先提交、出网许可签发方的
短事务才能读到已提交`SENDING`，因此编排经`ModelGatewayTransaction`端口在预流段后与流后段
各显式`commit`一次，失败则`rollback`；本服务不持有Session，提交边界由基础设施层绑定同一
Session的ledger/budget实现。

网络结果未知不误标失败：流迭代器异常归一为`DEPENDENCY_UNAVAILABLE`并在未开始时按`UNKNOWN`
对账、不新建计费行。预算结算按Usage口径归纳：EXACT→`SETTLE`+`SETTLED`、ESTIMATED_DEV→
`WORST_CASE_SETTLE`+`SETTLED`、UNKNOWN→`RECONCILING`。真实成本结算依赖费率卡，本切片暂按
`cost_amount=0`结算Token与调用次数，费率卡接入留待真实交接。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from knowledge_system.modules.memory.application.ports import ContextPackRepositoryPort
from knowledge_system.modules.tasking.domain.core_types import canonical_json_sha256

from ..domain.budget_ledger import BudgetLedgerEntryKind, BudgetLedgerEntryV1, settle_deltas
from ..domain.budget_quantity import BudgetQuantityV1
from ..domain.budget_reservation import (
    BudgetReservationStatus,
    BudgetReservationV1,
    resolve_settlement,
)
from ..domain.call_state import ModelCallState
from ..domain.chat_digest import ProviderChatRequestV1, digest_provider_chat_request
from ..domain.chat_request import BifrostChatRequestV1, ChatResponseFormatV1
from ..domain.controlled_call import (
    ControlledCallFailure,
    ControlledCallPlanV1,
    ControlledCallResultV1,
)
from ..domain.development_policy import DevelopmentPolicyViolation, validate_trial_input
from ..domain.egress_admission import EgressBodyDigestV1
from ..domain.model_binding import (
    DevelopmentModelBindingV1,
    ModelBindingViolation,
    development_route_ref,
    resolve_development_binding,
)
from ..domain.model_call import DataScope, ModelCallV1
from ..domain.provider_error import (
    ProviderErrorCode,
    ProviderErrorSource,
    ProviderErrorV1,
)
from ..domain.stream_event import ModelDeltaEventV1, ModelErrorEventV1
from ..domain.stream_normalizer import (
    StreamNormalizerState,
    normalize_bifrost_event,
    resolve_stream_terminal,
)
from ..domain.usage import ExactModelUsageV1, ModelUsageV1, UnknownModelUsageV1
from .bifrost import BifrostStreamPort
from .budget import BudgetLedger
from .context_pack_consumption import consume_context_pack
from .egress_permit_issuance import (
    EgressPermitIssuanceRequestV1,
    EgressPermitIssuer,
)
from .gateway import ModelCallInsertDisposition, ModelCallLedger, ModelCallTransitionUpdates
from .transaction import ModelGatewayTransaction


class ModelGatewayService:
    """Policy Adapter编排；只依赖端口，不持有时钟之外的运行时状态。"""

    def __init__(
        self,
        *,
        ledger: ModelCallLedger,
        budget: BudgetLedger,
        permit_issuer: EgressPermitIssuer,
        bifrost: BifrostStreamPort,
        transactions: ModelGatewayTransaction,
        context_packs: ContextPackRepositoryPort,
    ) -> None:
        self._ledger = ledger
        self._budget = budget
        self._permit_issuer = permit_issuer
        self._bifrost = bifrost
        self._transactions = transactions
        self._context_packs = context_packs

    async def complete(self, plan: ControlledCallPlanV1) -> ControlledCallResultV1:
        """编排一次受控调用并返回稳定结果；前置校验失败抛`ControlledCallFailure`。"""
        try:
            binding = resolve_development_binding(plan.capability_alias)
        except ModelBindingViolation as exc:
            raise ControlledCallFailure(exc.code) from exc
        self._validate_input(plan)

        request = self._build_request(binding, plan)
        digest = self._build_digest(binding, request)
        route_ref = development_route_ref(plan.capability_alias)

        created_at = datetime.now(UTC)
        expires_at = created_at + timedelta(seconds=plan.expires_in_seconds)
        reservation_id = uuid4()

        call = self._build_model_call(plan, binding, request, reservation_id, created_at)
        reservation = self._build_reservation(plan, reservation_id, created_at)

        row_version = await self._persist_pre_stream(plan, call, reservation, binding)
        await self._transactions.commit()

        await self._issue_permit(plan, binding, digest, route_ref, expires_at)

        state, text, stream_error, completed = await self._stream(
            plan, route_ref, request
        )
        result = await self._persist_post_stream(
            plan, reservation, state, text, stream_error, completed, row_version
        )
        await self._transactions.commit()
        return result

    async def _persist_pre_stream(
        self,
        plan: ControlledCallPlanV1,
        call: ModelCallV1,
        reservation: BudgetReservationV1,
        binding: DevelopmentModelBindingV1,
    ) -> int:
        """预流持久化（MODEL_REQUEST_ACCEPTED事务）：ACCEPTED→SENDING + RESERVED→CONSUMING。"""
        try:
            await consume_context_pack(
                self._context_packs,
                context_pack_id=plan.context_pack_id,
                context_pack_hash=plan.context_pack_hash,
                context_pack_input_digest=plan.context_pack_input_digest,
                consumed_at=call.created_at,
            )
            insert = await self._ledger.insert_accepted(call)
            if insert.disposition is ModelCallInsertDisposition.ALREADY_PRESENT:
                raise ControlledCallFailure("MODEL_CALL_REPLAY_UNSUPPORTED")
            row_version = insert.row_version

            await self._budget.insert_reserved(
                reservation, self._reserve_entry(reservation)
            )

            sending = await self._ledger.transition_status(
                plan.model_call_id,
                from_status=ModelCallState.ACCEPTED,
                to_status=ModelCallState.SENDING,
                expected_row_version=row_version,
                updates=ModelCallTransitionUpdates(
                    actual_provider=binding.provider,
                    actual_model_id=binding.model_revision,
                    actual_model_revision=binding.model_revision,
                ),
            )
            if not sending.committed:
                raise ControlledCallFailure("MODEL_CALL_SENDING_TRANSITION_FAILED")
            row_version += 1

            consuming = await self._budget.transition_status(
                reservation.reservation_id,
                from_status=BudgetReservationStatus.RESERVED,
                to_status=BudgetReservationStatus.CONSUMING,
                expected_row_version=1,
            )
            if not consuming.committed:
                raise ControlledCallFailure("BUDGET_CONSUMING_TRANSITION_FAILED")
            return row_version
        except ControlledCallFailure:
            await self._transactions.rollback()
            raise

    async def _issue_permit(
        self,
        plan: ControlledCallPlanV1,
        binding: DevelopmentModelBindingV1,
        digest: EgressBodyDigestV1,
        route_ref: str,
        expires_at: datetime,
    ) -> None:
        """签发一次性出网许可；签发方在基础设施层解析SecretRef/SecretVersion。"""
        await self._permit_issuer.issue_ready(
            EgressPermitIssuanceRequestV1(
                model_call_id=plan.model_call_id,
                route_ref=route_ref,
                provider=binding.provider,
                provider_model=binding.model_revision,
                body=digest,
                authorization_version=plan.authorization_version,
                execution_generation=plan.execution_generation,
                cancellation_generation=plan.cancellation_generation,
                expires_at=expires_at,
            )
        )

    async def _persist_post_stream(
        self,
        plan: ControlledCallPlanV1,
        reservation: BudgetReservationV1,
        state: StreamNormalizerState,
        text: str,
        stream_error: ProviderErrorV1 | None,
        completed: bool,
        row_version: int,
    ) -> ControlledCallResultV1:
        """流后持久化（终态收敛 + 预算结算）并返回稳定结果。"""
        try:
            if state.stream_started:
                streaming = await self._ledger.transition_status(
                    plan.model_call_id,
                    from_status=ModelCallState.SENDING,
                    to_status=ModelCallState.STREAMING,
                    expected_row_version=row_version,
                )
                if not streaming.committed:
                    raise ControlledCallFailure("MODEL_CALL_STREAMING_TRANSITION_FAILED")
                row_version += 1

            terminal = resolve_stream_terminal(
                state=state,
                completed=completed,
                error=stream_error,
                estimated_input_tokens=plan.reserved.input_tokens,
                estimated_output_tokens=plan.reserved.output_tokens,
            )
            from_status = (
                ModelCallState.STREAMING if state.stream_started else ModelCallState.SENDING
            )
            final = await self._ledger.transition_status(
                plan.model_call_id,
                from_status=from_status,
                to_status=terminal.status,
                expected_row_version=row_version,
                updates=ModelCallTransitionUpdates(
                    usage=terminal.usage,
                    response_hash=canonical_json_sha256(text),
                    error_code=terminal.error_code,
                ),
            )
            if not final.committed:
                raise ControlledCallFailure("MODEL_CALL_TERMINAL_TRANSITION_FAILED")

            await self._settle_budget(reservation, terminal.usage)

            return ControlledCallResultV1(
                model_call_id=plan.model_call_id,
                status=terminal.status,
                text=text,
                usage=terminal.usage,
                error_code=terminal.error_code,
            )
        except ControlledCallFailure:
            await self._transactions.rollback()
            raise

    def _validate_input(self, plan: ControlledCallPlanV1) -> None:
        text = "\n".join(message.content for message in plan.messages)
        try:
            validate_trial_input(app_env="development", data_scope="PUBLIC_DEVELOPMENT", text=text)
        except DevelopmentPolicyViolation as exc:
            raise ControlledCallFailure(exc.code) from exc

    def _build_request(
        self, binding: DevelopmentModelBindingV1, plan: ControlledCallPlanV1
    ) -> BifrostChatRequestV1:
        return BifrostChatRequestV1(
            model=binding.bifrost_model,
            messages=plan.messages,
            temperature=binding.temperature,
            top_p=binding.top_p,
            max_tokens=plan.max_tokens,
            response_format=ChatResponseFormatV1(json_schema=plan.response_schema),
        )

    def _build_digest(
        self, binding: DevelopmentModelBindingV1, request: BifrostChatRequestV1
    ) -> EgressBodyDigestV1:
        provider_request = ProviderChatRequestV1(
            model=binding.model_revision,
            messages=request.messages,
            stream=request.stream,
            stream_options=request.stream_options,
            temperature=request.temperature,
            top_p=request.top_p,
            max_tokens=request.max_tokens,
            response_format=request.response_format,
        )
        return digest_provider_chat_request(provider_request)

    def _build_model_call(
        self,
        plan: ControlledCallPlanV1,
        binding: DevelopmentModelBindingV1,
        request: BifrostChatRequestV1,
        reservation_id: UUID,
        created_at: datetime,
    ) -> ModelCallV1:
        return ModelCallV1(
            model_call_id=plan.model_call_id,
            task_id=plan.task_id,
            capability_alias=plan.capability_alias,
            approved_model_id=binding.model_revision,
            approved_model_revision=binding.model_revision,
            prompt_version=plan.prompt_version,
            context_pack_id=plan.context_pack_id,
            context_pack_hash=plan.context_pack_hash,
            context_pack_input_digest=plan.context_pack_input_digest,
            context_policy_version=plan.context_policy_version,
            authorization_version=plan.authorization_version,
            execution_generation=plan.execution_generation,
            cancellation_generation=plan.cancellation_generation,
            runtime_version=plan.runtime_version,
            agent_manifest_version=plan.agent_manifest_version,
            conversation_memory_version=0,
            user_private_memory_version=0,
            task_memory_version=0,
            token_accounting=plan.token_accounting,
            data_scope=DataScope.PUBLIC_DEVELOPMENT,
            request_hash=canonical_json_sha256(request.model_dump(mode="json", by_alias=True)),
            usage=UnknownModelUsageV1(kind="UNKNOWN"),
            budget_reservation_id=reservation_id,
            status=ModelCallState.ACCEPTED,
            created_at=created_at,
        )

    def _build_reservation(
        self, plan: ControlledCallPlanV1, reservation_id: UUID, created_at: datetime
    ) -> BudgetReservationV1:
        return BudgetReservationV1(
            reservation_id=reservation_id,
            task_id=plan.task_id,
            model_call_id=plan.model_call_id,
            account_id=plan.account_id,
            reservation_version=1,
            reserved=plan.reserved,
            settled=BudgetQuantityV1(),
            reservation_digest=canonical_json_sha256(
                {
                    "reservation_id": str(reservation_id),
                    "task_id": str(plan.task_id),
                    "account_id": str(plan.account_id),
                    "reservation_version": 1,
                    "reserved": plan.reserved.model_dump(mode="json"),
                }
            ),
            status=BudgetReservationStatus.RESERVED,
            created_at=created_at,
            row_version=1,
        )

    def _reserve_entry(self, reservation: BudgetReservationV1) -> BudgetLedgerEntryV1:
        return BudgetLedgerEntryV1(
            entry_id=uuid4(),
            task_id=reservation.task_id,
            account_id=reservation.account_id,
            reservation_id=reservation.reservation_id,
            entry_kind=BudgetLedgerEntryKind.RESERVE,
            delta=reservation.reserved.as_delta(),
            created_at=reservation.created_at,
        )

    async def _stream(
        self,
        plan: ControlledCallPlanV1,
        route_ref: str,
        request: BifrostChatRequestV1,
    ) -> tuple[StreamNormalizerState, str, ProviderErrorV1 | None, bool]:
        state = StreamNormalizerState()
        text_parts: list[str] = []
        stream_error: ProviderErrorV1 | None = None
        completed = False
        try:
            async for event in self._bifrost.stream(
                model_call_id=str(plan.model_call_id),
                trace_id=uuid4().hex,
                route_ref=route_ref,
                request=request,
            ):
                state, model_events = normalize_bifrost_event(event, state)
                for model_event in model_events:
                    if isinstance(model_event, ModelDeltaEventV1):
                        text_parts.append(model_event.text_delta)
                    elif isinstance(model_event, ModelErrorEventV1):
                        stream_error = model_event.error
        except Exception:
            stream_error = ProviderErrorV1(
                code=ProviderErrorCode.DEPENDENCY_UNAVAILABLE,
                source=ProviderErrorSource.CONNECTION,
            )
        else:
            completed = True
        return state, "".join(text_parts), stream_error, completed

    async def _settle_budget(
        self, reservation: BudgetReservationV1, usage: ModelUsageV1
    ) -> None:
        decision = resolve_settlement(usage)
        if decision.status is BudgetReservationStatus.RECONCILING:
            reconciled = await self._budget.transition_status(
                reservation.reservation_id,
                from_status=BudgetReservationStatus.CONSUMING,
                to_status=BudgetReservationStatus.RECONCILING,
                expected_row_version=2,
            )
            if not reconciled.committed:
                raise ControlledCallFailure("BUDGET_RECONCILE_TRANSITION_FAILED")
            return

        entry_kind = decision.entry_kind
        assert entry_kind is not None  # SETTLED 必有结算分录种类
        settled_quantity = self._settlement_quantity(entry_kind, reservation, usage)
        settle_delta, return_delta = settle_deltas(reservation.reserved, settled_quantity)
        settled = await self._budget.transition_status(
            reservation.reservation_id,
            from_status=BudgetReservationStatus.CONSUMING,
            to_status=BudgetReservationStatus.SETTLED,
            expected_row_version=2,
            settled=settled_quantity,
        )
        if not settled.committed:
            raise ControlledCallFailure("BUDGET_SETTLE_TRANSITION_FAILED")

        await self._budget.append_entry(
            BudgetLedgerEntryV1(
                entry_id=uuid4(),
                task_id=reservation.task_id,
                account_id=reservation.account_id,
                reservation_id=reservation.reservation_id,
                entry_kind=entry_kind,
                delta=settle_delta,
                created_at=datetime.now(UTC),
            )
        )
        if any(getattr(return_delta, name) != 0 for name in _DIMENSIONS):
            await self._budget.append_entry(
                BudgetLedgerEntryV1(
                    entry_id=uuid4(),
                    task_id=reservation.task_id,
                    account_id=reservation.account_id,
                    reservation_id=reservation.reservation_id,
                    entry_kind=BudgetLedgerEntryKind.RETURN,
                    delta=return_delta,
                    created_at=datetime.now(UTC),
                )
            )

    def _settlement_quantity(
        self,
        entry_kind: BudgetLedgerEntryKind,
        reservation: BudgetReservationV1,
        usage: ModelUsageV1,
    ) -> BudgetQuantityV1:
        if entry_kind is BudgetLedgerEntryKind.SETTLE:
            assert isinstance(usage, ExactModelUsageV1)  # EXACT→SETTLE
            return BudgetQuantityV1(
                model_call_count=1,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                tool_count=0,
                cost_amount=Decimal("0"),
            )
        return reservation.reserved


_DIMENSIONS = (
    "model_call_count",
    "input_tokens",
    "output_tokens",
    "tool_count",
    "cost_amount",
)


__all__ = ["ModelGatewayService"]
