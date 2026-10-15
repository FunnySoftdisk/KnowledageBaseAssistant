"""DEV-08：记忆召回领域契约与确定性重验规则（零 LLM/零 embedding/零 I/O）。

权威设计：模块16 §9.1/§9.2/§9.3 与 ADR-0004 §4/§5。本文件只冻结召回侧领域类型与确定性
重验谓词；不访问模型、网络、ORM 或 Temporal。当前实现 §9.2 的确定性通道——被用户 Pin 的
Active Memory（通道2）、`canonical_key` 精确匹配（通道3）与最近 Active Memory（通道5）；
全文检索与向量召回（通道4，需 FTS/pgvector/embedding）、RRF/MMR 排序（§9.3，权重由评测集
冻结）均留待后续切片。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator

from knowledge_system.modules.tasking.domain.core_types import StableKey, StrictContractV1

from .memory_types import (
    MemoryKindV1,
    MemoryRecordV1,
    MemoryScopeV1,
    MemoryStateV1,
)


class MemoryRecallChannelV1(StrEnum):
    """召回候选通道（§9.2）。全文/向量通道属后续切片（需 FTS/pgvector/embedding）。"""

    PINNED = "PINNED"
    EXACT_KEY = "EXACT_KEY"
    RECENT_ACTIVE = "RECENT_ACTIVE"


class MemorySearchHitV1(StrictContractV1):
    """一条召回命中的冻结投影：权威记录 + 来源通道。"""

    record: MemoryRecordV1
    channel: MemoryRecallChannelV1


class MemoryRecallRequestV1(StrictContractV1):
    """召回输入（§9.1，确定性子集）。`max_candidates` 硬上限候选数；`token_budget` 门禁在
    Context 装配层按 Manifest Token 账执行（§11），本切片不做 Tokenizer 估算。"""

    owner_id: UUID
    scopes: tuple[MemoryScopeV1, ...] = Field(min_length=1)
    conversation_id: UUID | None = None
    task_id: UUID | None = None
    canonical_keys: tuple[StableKey, ...] = ()
    required_kinds: tuple[MemoryKindV1, ...] = ()
    max_candidates: int = Field(ge=1)
    now: datetime

    @field_validator("now")
    @classmethod
    def normalize_now(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC_DATETIME_REQUIRED")
        return value.astimezone(UTC)


class MemoryRecallResultV1(StrictContractV1):
    """召回结果：命中（去重、冲突展开、确定性排序后）、重验被拒候选与是否截断。"""

    hits: tuple[MemorySearchHitV1, ...]
    rejected: tuple[MemoryRecordV1, ...] = ()
    truncated: bool = False


def revalidate_record(
    record: MemoryRecordV1,
    *,
    owner_id: UUID,
    scopes: frozenset[MemoryScopeV1],
    now: datetime,
) -> bool:
    """§9.2：候选返回前的确定性重验——owner、scope、state、有效期。

    来源状态/授权版本/内容哈希的复核依赖外部授权与 Artifact 事实，属召回接入切片；此处只做
    Memory 记录自身可判定的闭包。防御性重验：仓库查询已按 ACTIVE 过滤，这里再核对一次。
    """

    if record.owner_id != owner_id:
        return False
    if record.scope not in scopes:
        return False
    if record.state is not MemoryStateV1.ACTIVE:
        return False
    if record.valid_from > now:
        return False
    if record.valid_until is not None and record.valid_until <= now:
        return False
    return True


__all__ = [
    "MemoryRecallChannelV1",
    "MemoryRecallRequestV1",
    "MemoryRecallResultV1",
    "MemorySearchHitV1",
    "revalidate_record",
]
