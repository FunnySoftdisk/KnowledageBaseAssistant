"""DEV-08 子批次 12：Context Pack 消费门（模型网关硬校验 + 首次消费翻转，零 LLM/零 I/O）。

依据模块10 §4.1.1/§11（Gateway 只硬校验、不修改 Pack 正文）与模块16 §12.1（首个 ModelCall 接受时
READY→CONSUMED、CONSUMED 幂等复用、FAILED/INVALIDATED_BEFORE_USE 永不复用）。`consume_context_pack`
是「Gateway 接受 ModelCall 是并发语义线性化点」里的 Pack 一步：按 `context_pack_id` 读回存储头，校验
调用方声明的 `context_pack_hash`/`context_pack_input_digest` 与存储头 `pack_hash`/`input_digest`
一致，再借权威 Store 的 `mark_consumed` 完成 READY→CONSUMED（首次）或幂等命中（重试复用）。

本切片只校验「调用方声明 vs 存储头」，不重算 `input_digest`、不锁 Authorization/版本快照（
`MODEL_CONTEXT_STALE` 属完整线性化，留后续切片）。确定性纯编排：持有注入的
`ContextPackRepositoryPort`，不提交、不开启事务；失败以 `ControlledCallFailure` 稳定公开码关闭。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from knowledge_system.modules.memory.application.ports import (
    ContextPackRepositoryPort,
    ContextPackStateV1,
    ContextPackTransitionOutcomeV1,
)
from knowledge_system.modules.model_gateway.domain.controlled_call import ControlledCallFailure
from knowledge_system.modules.model_gateway.domain.model_call import Sha256
from knowledge_system.modules.tasking.domain.core_types import StrictContractV1


class ContextPackConsumeOutcomeV1(StrEnum):
    """消费门终局：READY→CONSUMED（首次消费）或已 CONSUMED 的幂等复用（重试）。"""

    FRESH_CONSUMED = "FRESH_CONSUMED"
    RETRY_REUSED = "RETRY_REUSED"


class ContextPackConsumeResultV1(StrictContractV1):
    """消费门结果：`state` 恒为 CONSUMED（翻转后或幂等命中时）。"""

    outcome: ContextPackConsumeOutcomeV1
    pack_id: UUID
    state: ContextPackStateV1


async def consume_context_pack(
    context_packs: ContextPackRepositoryPort,
    *,
    context_pack_id: UUID,
    context_pack_hash: Sha256,
    context_pack_input_digest: Sha256,
    consumed_at: datetime,
) -> ContextPackConsumeResultV1:
    """校验 Pack 引用并翻转 READY→CONSUMED；非法态/缺失/不匹配一律失败关闭。

    - 缺失 → `CONTEXT_PACK_NOT_FOUND`；hash 不匹配 → `CONTEXT_PACK_HASH_MISMATCH`；
      input_digest 不匹配 → `CONTEXT_PACK_INPUT_DIGEST_MISMATCH`；
      非 READY/CONSUMED（FAILED/INVALIDATED_BEFORE_USE/ASSEMBLING）→ `CONTEXT_PACK_NOT_READY`。
    - READY → `mark_consumed` 真正翻转 → FRESH_CONSUMED；CONSUMED → 幂等命中 → RETRY_REUSED。
    """

    header = await context_packs.find_by_id(pack_id=context_pack_id)
    if header is None:
        raise ControlledCallFailure("CONTEXT_PACK_NOT_FOUND")
    if header.pack_hash != context_pack_hash:
        raise ControlledCallFailure("CONTEXT_PACK_HASH_MISMATCH")
    if header.input_digest != context_pack_input_digest:
        raise ControlledCallFailure("CONTEXT_PACK_INPUT_DIGEST_MISMATCH")
    if header.state not in (ContextPackStateV1.READY, ContextPackStateV1.CONSUMED):
        raise ControlledCallFailure("CONTEXT_PACK_NOT_READY")

    transition = await context_packs.mark_consumed(
        pack_id=context_pack_id, consumed_at=consumed_at
    )
    outcome = (
        ContextPackConsumeOutcomeV1.FRESH_CONSUMED
        if transition.outcome is ContextPackTransitionOutcomeV1.TRANSITIONED
        else ContextPackConsumeOutcomeV1.RETRY_REUSED
    )
    return ContextPackConsumeResultV1(
        outcome=outcome, pack_id=context_pack_id, state=transition.state
    )


__all__ = [
    "ContextPackConsumeOutcomeV1",
    "ContextPackConsumeResultV1",
    "consume_context_pack",
]
