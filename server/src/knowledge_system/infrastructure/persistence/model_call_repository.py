"""`workflow.model_call`账本SQLAlchemy适配器。

实现`ModelCallLedger`端口：插入按主键幂等（`INSERT ... ON CONFLICT (id) DO NOTHING`，重复
`model_call_id`不新建第二条计费行、也不覆盖既有事实），状态转移按
`from_status + expected_row_version`CAS并落地`started_at/ended_at`，冲突时精确区分不存在/非预期
状态/行版本陈旧。本模块不提交事务，由上层UoW/服务在合适边界提交。Token Accounting与Usage的
嵌套值对象在此与判别列+互斥分支列互转。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_system.modules.model_gateway.application.gateway import (
    ModelCallInsertDisposition,
    ModelCallInsertResult,
    ModelCallTransitionConflict,
    ModelCallTransitionResult,
    ModelCallTransitionUpdates,
)
from knowledge_system.modules.model_gateway.domain.call_state import (
    ModelCallState,
    ModelCallStateViolation,
    is_terminal,
    require_transition,
)
from knowledge_system.modules.model_gateway.domain.model_call import DataScope, ModelCallV1
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    EstimatedDevTokenAccountingV1,
    ExactTokenAccountingV1,
)
from knowledge_system.modules.model_gateway.domain.usage import (
    EstimatedModelUsageV1,
    ExactModelUsageV1,
    UnknownModelUsageV1,
)

from .model_call_models import ModelCallRecord


class ModelCallRecordIntegrityError(RuntimeError):
    """已持久化列违反Token Accounting/Usage分支不变式，无法还原为读模型。"""


def _require_int(value: int | None, column: str) -> int:
    if value is None:
        raise ModelCallRecordIntegrityError(f"MODEL_CALL_NULL_{column}")
    return value


def _require_str(value: str | None, column: str) -> str:
    if value is None:
        raise ModelCallRecordIntegrityError(f"MODEL_CALL_NULL_{column}")
    return value


def _flatten_accounting(
    accounting: ExactTokenAccountingV1 | EstimatedDevTokenAccountingV1,
) -> dict[str, object]:
    if isinstance(accounting, ExactTokenAccountingV1):
        return {
            "token_accounting_kind": "EXACT",
            "tokenizer_revision": accounting.tokenizer_revision,
            "chat_template_hash": accounting.chat_template_hash,
            "exact_input_tokens": accounting.exact_input_tokens,
            "estimator_revision": None,
            "serialized_request_bytes": None,
            "protocol_overhead_tokens": None,
            "estimated_input_tokens": None,
        }
    return {
        "token_accounting_kind": "ESTIMATED_DEV",
        "tokenizer_revision": None,
        "chat_template_hash": None,
        "exact_input_tokens": None,
        "estimator_revision": accounting.estimator_revision,
        "serialized_request_bytes": accounting.serialized_request_bytes,
        "protocol_overhead_tokens": accounting.protocol_overhead_tokens,
        "estimated_input_tokens": accounting.estimated_input_tokens,
    }


def _flatten_usage(
    usage: ExactModelUsageV1 | EstimatedModelUsageV1 | UnknownModelUsageV1,
) -> dict[str, object]:
    if isinstance(usage, UnknownModelUsageV1):
        return {"usage_source": "UNKNOWN", "input_tokens": None, "output_tokens": None}
    if isinstance(usage, ExactModelUsageV1):
        return {
            "usage_source": "PROVIDER",
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }
    return {
        "usage_source": "WORST_CASE_RESERVATION",
        "input_tokens": usage.estimated_input_tokens,
        "output_tokens": usage.estimated_output_tokens,
    }


def record_from_v1(call: ModelCallV1) -> ModelCallRecord:
    """读模型 → ORM行（判别列+互斥分支列展开），`row_version`与时间戳显式落定。"""
    accounting = _flatten_accounting(call.token_accounting)
    usage = _flatten_usage(call.usage)
    return ModelCallRecord(
        id=call.model_call_id,
        task_id=call.task_id,
        attempt_id=call.attempt_id,
        action_attempt_id=call.action_attempt_id,
        branch_id=call.branch_id,
        delegation_id=call.delegation_id,
        execution_generation=call.execution_generation,
        cancellation_generation=call.cancellation_generation,
        approved_endpoint_id=call.approved_endpoint_id,
        actual_endpoint_id=call.actual_endpoint_id,
        capability_alias=call.capability_alias,
        approved_model_id=call.approved_model_id,
        approved_model_revision=call.approved_model_revision,
        actual_provider=call.actual_provider,
        actual_model_id=call.actual_model_id,
        actual_model_revision=call.actual_model_revision,
        bifrost_request_id=call.bifrost_request_id,
        provider_request_id=call.provider_request_id,
        prompt_version=call.prompt_version,
        tool_schema_versions=call.tool_schema_versions,
        context_pack_id=call.context_pack_id,
        context_policy_version=call.context_policy_version,
        authorization_version=call.authorization_version,
        grant_id=call.grant_id,
        runtime_version=call.runtime_version,
        plan_version=call.plan_version,
        agent_manifest_version=call.agent_manifest_version,
        conversation_memory_version=call.conversation_memory_version,
        user_private_memory_version=call.user_private_memory_version,
        task_memory_version=call.task_memory_version,
        output_schema_id=call.output_schema_id,
        output_schema_version=call.output_schema_version,
        data_scope=call.data_scope.value,
        request_hash=call.request_hash,
        response_hash=call.response_hash,
        cost_amount=call.cost_amount,
        cost_currency=call.cost_currency,
        budget_reservation_id=call.budget_reservation_id,
        rate_card_version=call.rate_card_version,
        exchange_rate_snapshot_version=call.exchange_rate_snapshot_version,
        queue_ms=call.queue_ms,
        ttft_ms=call.ttft_ms,
        duration_ms=call.duration_ms,
        status=call.status.value,
        error_code=call.error_code,
        row_version=1,
        created_at=call.created_at,
        updated_at=call.created_at,
        **accounting,
        **usage,
    )


def _accounting_from_record(
    record: ModelCallRecord,
) -> ExactTokenAccountingV1 | EstimatedDevTokenAccountingV1:
    if record.token_accounting_kind == "EXACT":
        return ExactTokenAccountingV1(
            mode="EXACT",
            estimated=False,
            exact_input_tokens=_require_int(record.exact_input_tokens, "EXACT_INPUT_TOKENS"),
            tokenizer_revision=_require_str(record.tokenizer_revision, "TOKENIZER_REVISION"),
            chat_template_hash=_require_str(record.chat_template_hash, "CHAT_TEMPLATE_HASH"),
        )
    return EstimatedDevTokenAccountingV1(
        mode="ESTIMATED_DEV",
        estimated=True,
        estimated_input_tokens=_require_int(
            record.estimated_input_tokens, "ESTIMATED_INPUT_TOKENS"
        ),
        estimator_revision=_require_str(record.estimator_revision, "ESTIMATOR_REVISION"),
        serialized_request_bytes=_require_int(
            record.serialized_request_bytes, "SERIALIZED_REQUEST_BYTES"
        ),
        protocol_overhead_tokens=_require_int(
            record.protocol_overhead_tokens, "PROTOCOL_OVERHEAD_TOKENS"
        ),
    )


def _usage_from_record(
    record: ModelCallRecord,
) -> ExactModelUsageV1 | EstimatedModelUsageV1 | UnknownModelUsageV1:
    if record.usage_source == "UNKNOWN":
        return UnknownModelUsageV1(kind="UNKNOWN")
    if record.usage_source == "PROVIDER":
        return ExactModelUsageV1(
            kind="EXACT",
            input_tokens=_require_int(record.input_tokens, "INPUT_TOKENS"),
            output_tokens=_require_int(record.output_tokens, "OUTPUT_TOKENS"),
            source="PROVIDER",
        )
    return EstimatedModelUsageV1(
        kind="ESTIMATED_DEV",
        estimated_input_tokens=_require_int(record.input_tokens, "INPUT_TOKENS"),
        estimated_output_tokens=_require_int(record.output_tokens, "OUTPUT_TOKENS"),
        source="WORST_CASE_RESERVATION",
    )


def v1_from_record(record: ModelCallRecord) -> ModelCallV1:
    """ORM行 → 读模型（判别列重组为嵌套值对象）。"""
    return ModelCallV1(
        model_call_id=record.id,
        task_id=record.task_id,
        attempt_id=record.attempt_id,
        action_attempt_id=record.action_attempt_id,
        branch_id=record.branch_id,
        delegation_id=record.delegation_id,
        execution_generation=record.execution_generation,
        cancellation_generation=record.cancellation_generation,
        approved_endpoint_id=record.approved_endpoint_id,
        actual_endpoint_id=record.actual_endpoint_id,
        capability_alias=record.capability_alias,
        approved_model_id=record.approved_model_id,
        approved_model_revision=record.approved_model_revision,
        actual_provider=record.actual_provider,
        actual_model_id=record.actual_model_id,
        actual_model_revision=record.actual_model_revision,
        bifrost_request_id=record.bifrost_request_id,
        provider_request_id=record.provider_request_id,
        prompt_version=record.prompt_version,
        tool_schema_versions=record.tool_schema_versions,
        context_pack_id=record.context_pack_id,
        context_policy_version=record.context_policy_version,
        authorization_version=record.authorization_version,
        grant_id=record.grant_id,
        runtime_version=record.runtime_version,
        plan_version=record.plan_version,
        agent_manifest_version=record.agent_manifest_version,
        conversation_memory_version=record.conversation_memory_version,
        user_private_memory_version=record.user_private_memory_version,
        task_memory_version=record.task_memory_version,
        token_accounting=_accounting_from_record(record),
        output_schema_id=record.output_schema_id,
        output_schema_version=record.output_schema_version,
        data_scope=DataScope(record.data_scope),
        request_hash=record.request_hash,
        response_hash=record.response_hash,
        usage=_usage_from_record(record),
        cost_amount=record.cost_amount,
        cost_currency=record.cost_currency,
        budget_reservation_id=record.budget_reservation_id,
        rate_card_version=record.rate_card_version,
        exchange_rate_snapshot_version=record.exchange_rate_snapshot_version,
        queue_ms=record.queue_ms,
        ttft_ms=record.ttft_ms,
        duration_ms=record.duration_ms,
        status=ModelCallState(record.status),
        error_code=record.error_code,
        created_at=record.created_at,
    )


class ModelCallLedgerRepository:
    """模型调用账本适配器；持有同Session，不提交、不开启事务。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_accepted(self, call: ModelCallV1) -> ModelCallInsertResult:
        if call.status != ModelCallState.ACCEPTED:
            raise ModelCallStateViolation("MODEL_CALL_INSERT_NOT_ACCEPTED")
        record = record_from_v1(call)
        values = {
            column.name: getattr(record, column.name)
            for column in ModelCallRecord.__table__.columns
        }
        statement = (
            pg_insert(ModelCallRecord)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ModelCallRecord.id])
            .returning(ModelCallRecord.id)
        )
        inserted_id = (await self._session.execute(statement)).scalar_one_or_none()
        if inserted_id is not None:
            return ModelCallInsertResult(
                disposition=ModelCallInsertDisposition.INSERTED,
                call=call,
                row_version=1,
            )
        existing = await self._get_record_by_id(call.model_call_id)
        if existing is None:
            raise ModelCallRecordIntegrityError("MODEL_CALL_INSERT_CONFLICT_MISSING_ROW")
        return ModelCallInsertResult(
            disposition=ModelCallInsertDisposition.ALREADY_PRESENT,
            call=v1_from_record(existing),
            row_version=existing.row_version,
        )

    async def transition_status(
        self,
        model_call_id: UUID,
        *,
        from_status: ModelCallState,
        to_status: ModelCallState,
        expected_row_version: int,
        updates: ModelCallTransitionUpdates | None = None,
    ) -> ModelCallTransitionResult:
        require_transition(from_status, to_status)
        patch = updates or ModelCallTransitionUpdates()
        values = _transition_values(patch, to_status)
        statement = (
            update(ModelCallRecord)
            .where(
                ModelCallRecord.id == model_call_id,
                ModelCallRecord.status == from_status.value,
                ModelCallRecord.row_version == expected_row_version,
            )
            .values(**values)
            .returning(ModelCallRecord.id)
        )
        committed_id = (await self._session.execute(statement)).scalar_one_or_none()
        if committed_id is not None:
            record = await self._get_record_by_id(model_call_id)
            assert record is not None
            return ModelCallTransitionResult(committed=True, call=v1_from_record(record))

        record = await self._get_record_by_id(model_call_id)
        if record is None:
            reason = ModelCallTransitionConflict.NOT_FOUND
        elif ModelCallState(record.status) != from_status:
            reason = ModelCallTransitionConflict.NOT_IN_STATE
        else:
            reason = ModelCallTransitionConflict.ROW_VERSION_CONFLICT
        return ModelCallTransitionResult(committed=False, conflict_reason=reason)

    async def get_by_id(self, model_call_id: UUID) -> ModelCallV1 | None:
        record = await self._get_record_by_id(model_call_id)
        return None if record is None else v1_from_record(record)

    async def get_by_provider_request_id(self, provider_request_id: str) -> ModelCallV1 | None:
        statement: Select[tuple[ModelCallRecord]] = select(ModelCallRecord).where(
            ModelCallRecord.provider_request_id == provider_request_id
        )
        record = (await self._session.scalars(statement)).first()
        return None if record is None else v1_from_record(record)

    async def _get_record_by_id(self, model_call_id: UUID) -> ModelCallRecord | None:
        statement: Select[tuple[ModelCallRecord]] = select(ModelCallRecord).where(
            ModelCallRecord.id == model_call_id
        )
        return (await self._session.scalars(statement)).first()


def _transition_values(
    updates: ModelCallTransitionUpdates, to_status: ModelCallState
) -> dict[str, object]:
    values: dict[str, object] = {
        "status": to_status.value,
        "row_version": ModelCallRecord.row_version + 1,
        "updated_at": datetime.now(UTC),
    }
    if to_status == ModelCallState.SENDING:
        values["started_at"] = datetime.now(UTC)
    if is_terminal(to_status):
        values["ended_at"] = datetime.now(UTC)
    if updates.actual_endpoint_id is not None:
        values["actual_endpoint_id"] = updates.actual_endpoint_id
    if updates.actual_provider is not None:
        values["actual_provider"] = updates.actual_provider
    if updates.actual_model_id is not None:
        values["actual_model_id"] = updates.actual_model_id
    if updates.actual_model_revision is not None:
        values["actual_model_revision"] = updates.actual_model_revision
    if updates.bifrost_request_id is not None:
        values["bifrost_request_id"] = updates.bifrost_request_id
    if updates.provider_request_id is not None:
        values["provider_request_id"] = updates.provider_request_id
    if updates.response_hash is not None:
        values["response_hash"] = updates.response_hash
    if updates.cost_amount is not None:
        values["cost_amount"] = updates.cost_amount
    if updates.cost_currency is not None:
        values["cost_currency"] = updates.cost_currency
    if updates.error_code is not None:
        values["error_code"] = updates.error_code
    if updates.queue_ms is not None:
        values["queue_ms"] = updates.queue_ms
    if updates.ttft_ms is not None:
        values["ttft_ms"] = updates.ttft_ms
    if updates.duration_ms is not None:
        values["duration_ms"] = updates.duration_ms
    if updates.usage is not None:
        values.update(_flatten_usage(updates.usage))
    return values


__all__ = [
    "ModelCallLedgerRepository",
    "ModelCallRecordIntegrityError",
    "record_from_v1",
    "v1_from_record",
]
