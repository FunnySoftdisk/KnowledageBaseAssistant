"""不可变 Context Pack 的 SQLAlchemy 权威 Store 适配器（实现 `ContextPackRepositoryPort`）。

把子批次 7 装配出的不可变 `ContextPackV1` 一次原子落库到 `workflow.context_pack` +
`workflow.context_pack_item`。显式 kwargs 互转（无 `model_validate`），持有同 Session，
不提交、不开启事务（由上层 UoW/服务提交）。本切片只落库、不读回（`input_digest` 复用留待
模型网关接线切片），也不伪造与冻结表冲突的事实：

- `ESTIMATED_DEV` Token 计数无 Tokenizer/Chat Template → `tokenizer_revision`/
  `chat_template_hash` 落 NULL（其身份已由 `input_digest` 内容寻址）。
- 强制项（SYSTEM_POLICY/TOOL_SCHEMA/OUTPUT_SCHEMA/CURRENT_MESSAGE）无单一来源、无弹性桶
  → `source_type`/`source_id`/`source_version`/`bucket` 落 NULL；弹性项有 `source_ref` 时
  逐列映射，`source_id` 严格 `UUID` 解析，非 UUID 以 `CONTEXT_ITEM_SOURCE_ID_NOT_UUID`
  失败关闭（不静默截断）。
- `rank_score` 缺省落 NULL（「无相关度分」不等同于 0.0）。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_system.modules.memory.application.ports import (
    ContextPackSaveOutcomeV1,
    ContextPackTransitionOutcomeV1,
    ContextPackTransitionResultV1,
)
from knowledge_system.modules.memory.domain.context_assembly import (
    ContextPackReuseHitV1,
    ContextPackStateV1,
    ContextPackV1,
    is_legal_context_pack_transition,
)
from knowledge_system.modules.memory.domain.context_pack import (
    ContextItemManifestV1,
    bucket_for_item_type,
)
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    ExactTokenAccountingV1,
    TokenAccountingV1,
)

from .attempt_models import ContextPackItemRecord, ContextPackRecord


class ContextPackIntegrityError(RuntimeError):
    """Context Pack 或其 Item 违反不可变不变式，无法落库。"""


def _tokenizer_fields(accounting: TokenAccountingV1) -> tuple[str | None, str | None]:
    """Token 计数 → 冻结表 Tokenizer/Chat Template 两列；ESTIMATED_DEV 落 NULL。"""

    if isinstance(accounting, ExactTokenAccountingV1):
        return accounting.tokenizer_revision, accounting.chat_template_hash
    return None, None


def _tool_schema_versions(pack: ContextPackV1) -> dict[str, object]:
    return {schema.name: schema.version for schema in pack.tool_schema_versions}


def record_from_pack(pack: ContextPackV1) -> ContextPackRecord:
    """不可变 Pack → ORM 头行（Item 由 `item_record_from_manifest` 逐条映射）。"""

    tokenizer_revision, chat_template_hash = _tokenizer_fields(pack.token_accounting)
    return ContextPackRecord(
        id=pack.id,
        task_id=pack.task_id,
        action_attempt_id=pack.action_attempt_id,
        created_by_attempt_id=pack.created_by_attempt_id,
        conversation_id=pack.conversation_id,
        actor_id=pack.actor_id,
        authorization_ref_id=pack.authorization_ref_id,
        authorization_version=pack.authorization_version,
        state=pack.state.value,
        context_policy_version=pack.policy_version,
        conversation_memory_version=pack.memory_versions.conversation_version,
        user_private_memory_version=pack.memory_versions.user_private_version,
        task_memory_version=pack.memory_versions.task_version,
        runtime_version=pack.runtime_version,
        plan_version=pack.plan_version,
        agent_manifest_version=pack.agent_manifest_version,
        model_revision=pack.model_revision,
        prompt_version=pack.prompt_version,
        tool_schema_versions=_tool_schema_versions(pack),
        output_schema_id=pack.output_schema_id,
        output_schema_version=pack.output_schema_version,
        tokenizer_revision=tokenizer_revision,
        chat_template_hash=chat_template_hash,
        max_context=pack.max_context_tokens,
        reserved_output=pack.reserved_output_tokens,
        protocol_overhead=pack.protocol_overhead_tokens,
        safety_margin=pack.safety_margin_tokens,
        input_tokens=pack.input_tokens,
        body_artifact_id=pack.body_artifact_id,
        pack_hash=pack.pack_hash,
        input_digest=pack.input_digest,
        created_at=pack.created_at,
        consumed_at=None,
    )


def item_record_from_manifest(
    pack_id: UUID, position: int, item: ContextItemManifestV1
) -> ContextPackItemRecord:
    """单个 Manifest → ORM 行；强制项无来源/桶落 NULL，`source_id` 严格 UUID 解析。"""

    source_type: str | None = None
    source_id: UUID | None = None
    source_version: str | None = None
    if item.source_ref is not None:
        source_type = item.source_ref.source_type.value
        try:
            source_id = UUID(item.source_ref.source_id)
        except (ValueError, TypeError) as exc:
            raise ContextPackIntegrityError("CONTEXT_ITEM_SOURCE_ID_NOT_UUID") from exc
        source_version = item.source_ref.source_version
    bucket = bucket_for_item_type(item.item_type)
    return ContextPackItemRecord(
        context_pack_id=pack_id,
        position=position,
        item_id=item.item_id,
        item_type=item.item_type.value,
        source_type=source_type,
        source_id=source_id,
        source_version=source_version,
        content_hash=item.content_hash,
        token_count=item.token_count,
        priority=item.priority,
        rank_score=item.rank_score,
        compression_level=item.compression_level,
        selection_reason=item.selection_reason,
        trust=item.trust,
        bucket=None if bucket is None else bucket.value,
    )


def _apply_state_transition(
    record: ContextPackRecord, next_state: ContextPackStateV1
) -> ContextPackTransitionOutcomeV1:
    """§12.1 状态翻转：同态幂等返回 IDEMPOTENT，非法迁移失败关闭，否则落 `next_state`。"""

    current = ContextPackStateV1(record.state)
    if current is next_state:
        return ContextPackTransitionOutcomeV1.IDEMPOTENT
    if not is_legal_context_pack_transition(current, next_state):
        raise ContextPackIntegrityError("CONTEXT_PACK_ILLEGAL_STATE_TRANSITION")
    record.state = next_state.value
    return ContextPackTransitionOutcomeV1.TRANSITIONED


def pack_reuse_hit_from_record(record: ContextPackRecord) -> ContextPackReuseHitV1:
    """ORM 头行 → 读回复用命中（显式 kwargs，无 `model_validate`）。

    不重建 Item 清单与 `TokenAccountingV1`（冻结表不落完整计数与来源字段）。
    """

    return ContextPackReuseHitV1(
        id=record.id,
        task_id=record.task_id,
        state=ContextPackStateV1(record.state),
        pack_hash=record.pack_hash,
        input_digest=record.input_digest,
        input_tokens=record.input_tokens,
        body_artifact_id=record.body_artifact_id,
        created_at=record.created_at,
        consumed_at=record.consumed_at,
    )


class SqlAlchemyContextPackRepository:
    """`workflow.context_pack` 权威 Store 实现；持有同 Session，不提交、不开启事务。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_pack(self, *, pack: ContextPackV1) -> ContextPackSaveOutcomeV1:
        """按 `id` 幂等落库：同 `id` 重放返回 IDEMPOTENT_REPLAY，否则头行+Item 写入同一事务。"""

        existing = await self._session.get(ContextPackRecord, pack.id)
        if existing is not None:
            return ContextPackSaveOutcomeV1.IDEMPOTENT_REPLAY
        self._session.add(record_from_pack(pack))
        # 头行先落库：Item 外键引用 `context_pack.id`，而 `context_pack`↔`task_attempt` 的
        # 环形外键使 unit-of-work 无法自动排序。两次 flush 处于同一事务，由上层 commit 原子提交。
        await self._session.flush()
        for position, item in enumerate(pack.items, start=1):
            self._session.add(item_record_from_manifest(pack.id, position, item))
        await self._session.flush()
        return ContextPackSaveOutcomeV1.INSERTED

    async def find_reusable_by_input_digest(
        self, *, task_id: UUID, input_digest: str
    ) -> ContextPackReuseHitV1 | None:
        """按 `input_digest` 内容寻址去重：命中 READY/CONSUMED 头行即复用，未命中返回 None。

        `input_digest` 已内容寻址身份/版本/授权/Token 计数材料（§4.3），故同一任务内匹配
        `input_digest` 即等价于「全部版本仍一致」；FAILED/INVALIDATED_BEFORE_USE 永不复用。
        """

        statement: Select[tuple[ContextPackRecord]] = (
            select(ContextPackRecord)
            .where(
                ContextPackRecord.task_id == task_id,
                ContextPackRecord.input_digest == input_digest,
                ContextPackRecord.state.in_(
                    (ContextPackStateV1.READY.value, ContextPackStateV1.CONSUMED.value)
                ),
            )
            .order_by(ContextPackRecord.created_at.desc(), ContextPackRecord.id.desc())
            .limit(1)
        )
        record = (await self._session.scalars(statement)).first()
        return None if record is None else pack_reuse_hit_from_record(record)

    async def find_by_id(self, *, pack_id: UUID) -> ContextPackReuseHitV1 | None:
        """按 `id` 读回头行事实（任意状态），供网关消费前校验 ID/Hash/状态；未命中返回 None。"""

        record = await self._session.get(ContextPackRecord, pack_id)
        return None if record is None else pack_reuse_hit_from_record(record)

    async def mark_consumed(
        self, *, pack_id: UUID, consumed_at: datetime
    ) -> ContextPackTransitionResultV1:
        """§12.1 首次消费门：READY→CONSUMED 并落 `consumed_at`；已 CONSUMED 幂等命中。

        FAILED/INVALIDATED_BEFORE_USE/ASSEMBLING 不得消费，以
        `CONTEXT_PACK_ILLEGAL_STATE_TRANSITION` 失败关闭；不存在以 `CONTEXT_PACK_NOT_FOUND`
        失败关闭。
        """

        record = await self._session.get(ContextPackRecord, pack_id)
        if record is None:
            raise ContextPackIntegrityError("CONTEXT_PACK_NOT_FOUND")
        outcome = _apply_state_transition(record, ContextPackStateV1.CONSUMED)
        if outcome is ContextPackTransitionOutcomeV1.TRANSITIONED:
            record.consumed_at = consumed_at
            await self._session.flush()
        return ContextPackTransitionResultV1(
            state=ContextPackStateV1.CONSUMED, outcome=outcome
        )

    async def invalidate_before_use(
        self, *, pack_id: UUID
    ) -> ContextPackTransitionResultV1:
        """§12.1 使用前失效门：READY→INVALIDATED_BEFORE_USE；已失效幂等命中。

        CONSUMED/FAILED/ASSEMBLING 不得失效，以 `CONTEXT_PACK_ILLEGAL_STATE_TRANSITION` 失败关闭。
        """

        record = await self._session.get(ContextPackRecord, pack_id)
        if record is None:
            raise ContextPackIntegrityError("CONTEXT_PACK_NOT_FOUND")
        outcome = _apply_state_transition(record, ContextPackStateV1.INVALIDATED_BEFORE_USE)
        if outcome is ContextPackTransitionOutcomeV1.TRANSITIONED:
            await self._session.flush()
        return ContextPackTransitionResultV1(
            state=ContextPackStateV1.INVALIDATED_BEFORE_USE, outcome=outcome
        )


__all__ = [
    "ContextPackIntegrityError",
    "SqlAlchemyContextPackRepository",
    "item_record_from_manifest",
    "pack_reuse_hit_from_record",
    "record_from_pack",
]
