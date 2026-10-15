"""DEV-08：Context Pack 不可变契约与确定性装配编排（零 LLM/零 I/O）。

权威设计：模块16 §4.3/§5.1/§11.3。本文件把「不可变 Context Pack」（§20 实施顺序第 2 步）冻结为
领域契约：`ContextPackV1` 是每次模型调用的不可变输入快照，创建后正文与 Manifest 不可修改；重试
仅在 `input_digest` 及全部版本一致时复用。本切片只做确定性装配核心——预算（§4.4）→ 选择
（§11.3/§11.4）→ `pack_hash`（正文+Manifest+预算字段内容寻址）→ `input_digest`（输入身份内容寻址）。

零 LLM/零 I/O：Token 精确计数按 `TokenAccountingV1`（模块10 单一源类型）由调用方供给；授权 Scope、
Task Artifact、Evidence、近期 Raw Message 的加载与「授权范围/依赖引用/强制 Citation 规则」这几类
未入 `ContextItemTypeV1` 的强制项、以及 §4.3 完整 `input_digest` 公式中的 grant_version/
delegated_scope/budget_envelope/sorted_capability_schema_versions 组件，均留待接入装配/模型网关接线
切片。本文件不把它们伪装为已冻结事实。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator

from knowledge_system.modules.model_gateway.domain.token_accounting import TokenAccountingV1
from knowledge_system.modules.tasking.domain.core_types import (
    Sha256Hex,
    StrictContractV1,
    canonical_json_sha256,
)

from .context_pack import (
    ContextItemManifestV1,
    ContextOperationProfileV1,
    compute_context_budget,
    select_context_items,
)
from .memory_types import MemoryVersionSnapshotV1, memory_versions_digest_for


class ContextPackStateV1(StrEnum):
    """Context Pack 状态（§4.3 / 物理表 `state_allowed` CHECK）。"""

    ASSEMBLING = "ASSEMBLING"
    READY = "READY"
    CONSUMED = "CONSUMED"
    FAILED = "FAILED"
    INVALIDATED_BEFORE_USE = "INVALIDATED_BEFORE_USE"


class ToolSchemaVersionV1(StrictContractV1):
    """单个 Tool Schema 的名称/版本绑定；`input_digest` 按名称排序后内容寻址。"""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC_DATETIME_REQUIRED")
    return value.astimezone(UTC)


class ContextPackV1(StrictContractV1):
    """不可变 Context Pack（§4.3）：已排序、去重、压缩并完成预算校验的 Context Item 集合。"""

    id: UUID
    task_id: UUID
    action_attempt_id: UUID | None = None
    created_by_attempt_id: UUID | None = None
    conversation_id: UUID | None = None
    actor_id: UUID
    authorization_ref_id: UUID | None = None
    authorization_version: int | None = Field(default=None, ge=0)
    policy_version: str = Field(min_length=1)
    memory_versions: MemoryVersionSnapshotV1
    runtime_version: str = Field(min_length=1)
    plan_version: int | None = Field(default=None, ge=0)
    agent_manifest_version: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    tool_schema_versions: tuple[ToolSchemaVersionV1, ...] = ()
    output_schema_id: str | None = None
    output_schema_version: str | None = None
    token_accounting: TokenAccountingV1
    max_context_tokens: int = Field(ge=0)
    reserved_output_tokens: int = Field(ge=0)
    protocol_overhead_tokens: int = Field(ge=0)
    safety_margin_tokens: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    items: tuple[ContextItemManifestV1, ...]
    body_artifact_id: UUID
    pack_hash: Sha256Hex
    input_digest: Sha256Hex
    state: ContextPackStateV1
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return _normalize_utc(value)


class ContextPackReuseHitV1(StrictContractV1):
    """按 `input_digest` 复用不可变 Context Pack 的读回命中（§4.3/§20 复用判断）。

    只回传冻结头行事实（`id`/`state`/`pack_hash`/`input_digest`/`input_tokens`/`body_artifact_id`/
    `created_at`/`consumed_at`）。不重建 `TokenAccountingV1`（冻结表不落估算器分支的
    `estimator_revision`/`serialized_request_bytes`/`estimated_input_tokens` 与 EXACT 分支的
    `exact_input_tokens`）与 Item 清单（`context_pack_item` 未持久化 `source_ref.content_hash`/
    `message_sequence`），这些留待模型网关接线消费切片从 Body Artifact 与 Memory 权威 Store 按引用
    重读。
    """

    id: UUID
    task_id: UUID
    state: ContextPackStateV1
    pack_hash: Sha256Hex
    input_digest: Sha256Hex
    input_tokens: int = Field(ge=0)
    body_artifact_id: UUID
    created_at: datetime
    consumed_at: datetime | None = None

    @field_validator("created_at", "consumed_at")
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _normalize_utc(value)


class ContextAssemblyInputV1(StrictContractV1):
    """`assemble_context_pack` 的确定性输入：Pack 身份 + 预算 + 画像 + 候选 Item。

    `mandatory_items` 的 `item_type` 必须是 §11.2 强制项四类（SYSTEM_POLICY/TOOL_SCHEMA/
    OUTPUT_SCHEMA/CURRENT_MESSAGE）；`elastic_candidates` 为其余弹性项。`mandatory_tokens` 不单独
    收取，由 `mandatory_items` 的 `token_count` 求和派生，保证与选择结果一致。
    """

    id: UUID
    task_id: UUID
    action_attempt_id: UUID | None = None
    created_by_attempt_id: UUID | None = None
    conversation_id: UUID | None = None
    actor_id: UUID
    authorization_ref_id: UUID | None = None
    authorization_version: int | None = Field(default=None, ge=0)
    policy_version: str = Field(min_length=1)
    memory_versions: MemoryVersionSnapshotV1
    runtime_version: str = Field(min_length=1)
    plan_version: int | None = Field(default=None, ge=0)
    agent_manifest_version: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    tool_schema_versions: tuple[ToolSchemaVersionV1, ...] = ()
    output_schema_id: str | None = None
    output_schema_version: str | None = None
    token_accounting: TokenAccountingV1
    body_artifact_id: UUID
    created_at: datetime
    max_context_tokens: int = Field(ge=0)
    reserved_output_tokens: int = Field(ge=0)
    protocol_overhead_tokens: int = Field(ge=0)
    safety_margin_tokens: int = Field(ge=0)
    operation_profile: ContextOperationProfileV1
    mandatory_items: tuple[ContextItemManifestV1, ...]
    elastic_candidates: tuple[ContextItemManifestV1, ...]

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return _normalize_utc(value)


def compute_pack_hash(
    *,
    items: tuple[ContextItemManifestV1, ...],
    max_context_tokens: int,
    reserved_output_tokens: int,
    protocol_overhead_tokens: int,
    safety_margin_tokens: int,
    input_tokens: int,
) -> Sha256Hex:
    """§4.3：`pack_hash = SHA256(canonical_manifest + budget_fields)`（RFC 8785 JCS+SHA-256）。"""

    return canonical_json_sha256(
        {
            "items": [item.model_dump(mode="json") for item in items],
            "budget": {
                "max_context_tokens": max_context_tokens,
                "reserved_output_tokens": reserved_output_tokens,
                "protocol_overhead_tokens": protocol_overhead_tokens,
                "safety_margin_tokens": safety_margin_tokens,
                "input_tokens": input_tokens,
            },
        }
    )


def compute_input_digest(request: ContextAssemblyInputV1) -> Sha256Hex:
    """§4.3 输入身份内容寻址（冻结子集）：身份/版本/授权/记忆快照/Token 计数材料任一变化即换 Pack。

    已覆盖：task/attempt/actor/conversation/authorization(version)/policy/memory_versions/runtime/
    plan_version/agent_manifest/model/prompt/sorted_tool_schema/output_schema/token_accounting(含
    tokenizer_revision+chat_template_hash 或 estimator_revision+serialized_request_bytes)/
    body_artifact。

    未覆盖（接入装配/模型网关接线切片补）：plan_item/iteration/branch/base_work_version/agent_instance/
    action、sorted_artifact/evidence_ref/version/hash、grant_version、delegated_scope_digest、
    budget_envelope_digest、sorted_capability_schema_versions。
    """

    return canonical_json_sha256(
        {
            "task_id": str(request.task_id),
            "action_attempt_id": (
                None if request.action_attempt_id is None else str(request.action_attempt_id)
            ),
            "created_by_attempt_id": (
                None
                if request.created_by_attempt_id is None
                else str(request.created_by_attempt_id)
            ),
            "conversation_id": (
                None if request.conversation_id is None else str(request.conversation_id)
            ),
            "actor_id": str(request.actor_id),
            "authorization_ref_id": (
                None if request.authorization_ref_id is None else str(request.authorization_ref_id)
            ),
            "authorization_version": request.authorization_version,
            "policy_version": request.policy_version,
            "memory_versions_digest": memory_versions_digest_for(request.memory_versions),
            "runtime_version": request.runtime_version,
            "plan_version": request.plan_version,
            "agent_manifest_version": request.agent_manifest_version,
            "model_revision": request.model_revision,
            "prompt_version": request.prompt_version,
            "tool_schema_versions": sorted(
                [ts.name, ts.version] for ts in request.tool_schema_versions
            ),
            "output_schema_id": request.output_schema_id,
            "output_schema_version": request.output_schema_version,
            "token_accounting": request.token_accounting.model_dump(mode="json"),
            "body_artifact_id": str(request.body_artifact_id),
        }
    )


def assemble_context_pack(request: ContextAssemblyInputV1) -> ContextPackV1:
    """§11.3 确定性装配：预算 → 选择 → 内容寻址 → 冻结为 READY 的不可变 Pack。

    强制项超限由 `compute_context_budget` 以 `MODEL_CONTEXT_MANDATORY_EXCEEDED` 失败关闭；单个强制
    原子项超过可用输入窗口由 `select_context_items` 以 `CONTEXT_ATOMIC_ITEM_TOO_LARGE` 失败关闭；
    弹性项超限按既定规则裁剪（非 Required，可删），不视为装配失败。
    """

    mandatory_tokens = sum(item.token_count for item in request.mandatory_items)
    budget = compute_context_budget(
        max_context_tokens=request.max_context_tokens,
        reserved_output_tokens=request.reserved_output_tokens,
        protocol_overhead_tokens=request.protocol_overhead_tokens,
        safety_margin_tokens=request.safety_margin_tokens,
        mandatory_tokens=mandatory_tokens,
        profile=request.operation_profile,
    )
    selection = select_context_items(
        request.mandatory_items + request.elastic_candidates, budget
    )
    if selection.error_code is not None:
        raise ValueError(selection.error_code)

    pack_hash = compute_pack_hash(
        items=selection.selected,
        max_context_tokens=request.max_context_tokens,
        reserved_output_tokens=request.reserved_output_tokens,
        protocol_overhead_tokens=request.protocol_overhead_tokens,
        safety_margin_tokens=request.safety_margin_tokens,
        input_tokens=selection.total_tokens,
    )
    return ContextPackV1(
        id=request.id,
        task_id=request.task_id,
        action_attempt_id=request.action_attempt_id,
        created_by_attempt_id=request.created_by_attempt_id,
        conversation_id=request.conversation_id,
        actor_id=request.actor_id,
        authorization_ref_id=request.authorization_ref_id,
        authorization_version=request.authorization_version,
        policy_version=request.policy_version,
        memory_versions=request.memory_versions,
        runtime_version=request.runtime_version,
        plan_version=request.plan_version,
        agent_manifest_version=request.agent_manifest_version,
        model_revision=request.model_revision,
        prompt_version=request.prompt_version,
        tool_schema_versions=request.tool_schema_versions,
        output_schema_id=request.output_schema_id,
        output_schema_version=request.output_schema_version,
        token_accounting=request.token_accounting,
        max_context_tokens=request.max_context_tokens,
        reserved_output_tokens=request.reserved_output_tokens,
        protocol_overhead_tokens=request.protocol_overhead_tokens,
        safety_margin_tokens=request.safety_margin_tokens,
        input_tokens=selection.total_tokens,
        items=selection.selected,
        body_artifact_id=request.body_artifact_id,
        pack_hash=pack_hash,
        input_digest=compute_input_digest(request),
        state=ContextPackStateV1.READY,
        created_at=request.created_at,
    )


def is_legal_context_pack_transition(
    current: ContextPackStateV1, next_: ContextPackStateV1
) -> bool:
    """§12.1 合法状态迁移：ASSEMBLING→READY/FAILED、READY→CONSUMED/INVALIDATED_BEFORE_USE。

    CONSUMED/FAILED/INVALIDATED_BEFORE_USE 为终态（无出边）；同态幂等重放不在此表（由 Store 按
    IDEMPOTENT 处理），不得从 CONSUMED 回退 READY。
    """

    return (current, next_) in {
        (ContextPackStateV1.ASSEMBLING, ContextPackStateV1.READY),
        (ContextPackStateV1.ASSEMBLING, ContextPackStateV1.FAILED),
        (ContextPackStateV1.READY, ContextPackStateV1.CONSUMED),
        (ContextPackStateV1.READY, ContextPackStateV1.INVALIDATED_BEFORE_USE),
    }


__all__ = [
    "ContextAssemblyInputV1",
    "ContextPackReuseHitV1",
    "ContextPackStateV1",
    "ContextPackV1",
    "ToolSchemaVersionV1",
    "assemble_context_pack",
    "compute_input_digest",
    "compute_pack_hash",
    "is_legal_context_pack_transition",
]
