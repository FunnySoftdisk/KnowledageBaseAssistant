"""记忆与上下文写侧端口（DEV-08 子批次 2）。

依据 ADR-0004§1：`MemoryRepositoryPort` 是项目实现并保持权威的 Store 端口，隔离上游 Mem0 实体；
`MemoryEnginePort`（propose/search/reconcile）留待 Mem0 Adapter 切片，本切片只冻结写侧 Repository
端口与写侧契约。全部为 Protocol，零 LLM/零 I/O；持久化实现属基础设施层。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import Field

from knowledge_system.modules.tasking.domain.core_types import (
    Sha256Hex,
    StableKey,
    StrictContractV1,
)

from ..domain.context_assembly import (
    ContextPackReuseHitV1,
    ContextPackStateV1,
    ContextPackV1,
)
from ..domain.memory_types import (
    MemoryKindV1,
    MemoryRecordV1,
    MemoryScopeV1,
    MemoryStateV1,
    MemoryTrustV1,
    ProvenanceRefV1,
)


class GovernedMemoryCandidateV1(StrictContractV1):
    """写入 Gate 裁决后交 Repository 持久化的候选（§4.1/§7.3）。

    `state` 已由确定性 Gate 解析为 ACTIVE 或 QUARANTINED；`id`/`schema_version`/`revision` 由
    Repository 生成，不在此候选内。
    """

    owner_id: UUID
    conversation_id: UUID | None = None
    task_id: UUID | None = None
    scope: MemoryScopeV1
    kind: MemoryKindV1
    state: MemoryStateV1
    trust: MemoryTrustV1
    canonical_key: StableKey | None = None
    content: str
    canonical_hash: Sha256Hex
    importance: float = Field(ge=0, le=1)
    valid_from: datetime
    valid_until: datetime | None = None
    supersedes_id: UUID | None = None
    conflict_group_id: UUID | None = None
    provenance: tuple[ProvenanceRefV1, ...] = Field(min_length=1)


class MemoryRevisionCommandV1(StrictContractV1):
    """纠正命令：追加修订并使旧 Revision 进入 SUPERSEDED（§7.4，不原地改正文）。"""

    record_id: UUID
    corrected_content: str
    trust: MemoryTrustV1
    provenance: tuple[ProvenanceRefV1, ...] = Field(min_length=1)


class MemoryForgetCommandV1(StrictContractV1):
    """遗忘命令：先落 Tombstone 再异步清理正文/向量/缓存（§7.5/ADR-0004§6）。"""

    record_id: UUID
    reason: str


class MemoryPinCommandV1(StrictContractV1):
    """Pin/Unpin 命令（§6 用户控制，`PIN_CHANGED` 版本递增语义）：切换 `pinned` 标志。"""

    record_id: UUID
    pinned: bool


class ForgetReceiptV1(StrictContractV1):
    record_id: UUID
    tombstone_id: UUID
    forgotten_at: datetime


class ContextPackSaveOutcomeV1(StrEnum):
    """不可变 Context Pack 持久化结果：首次落库或按 `id` 幂等重放。"""

    INSERTED = "INSERTED"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"


class ContextPackTransitionOutcomeV1(StrEnum):
    """§12.1 状态翻转结果：真正迁移或同态幂等命中。"""

    TRANSITIONED = "TRANSITIONED"
    IDEMPOTENT = "IDEMPOTENT"


class ContextPackTransitionResultV1(StrictContractV1):
    """§12.1 消费/失效门的结果：翻转后（或幂等命中时）的终态 + 是否真正迁移。"""

    state: ContextPackStateV1
    outcome: ContextPackTransitionOutcomeV1


class ContextPackRepositoryPort(Protocol):
    """不可变 Context Pack 权威 Store 端口；实现属基础设施层，Pack 与 Item 一次原子落库。"""

    async def save_pack(self, *, pack: ContextPackV1) -> ContextPackSaveOutcomeV1: ...

    async def find_reusable_by_input_digest(
        self, *, task_id: UUID, input_digest: str
    ) -> ContextPackReuseHitV1 | None: ...

    async def find_by_id(self, *, pack_id: UUID) -> ContextPackReuseHitV1 | None: ...

    async def mark_consumed(
        self, *, pack_id: UUID, consumed_at: datetime
    ) -> ContextPackTransitionResultV1: ...

    async def invalidate_before_use(
        self, *, pack_id: UUID
    ) -> ContextPackTransitionResultV1: ...


class MemoryRepositoryPort(Protocol):
    """Memory 权威 Store 端口；实现属基础设施层，所有查询按 owner+scope 过滤（ADR-0004§4）。"""

    async def find_active_by_hash(
        self, *, owner_id: UUID, scope: MemoryScopeV1, canonical_hash: str
    ) -> MemoryRecordV1 | None: ...

    async def find_active_by_key(
        self, *, owner_id: UUID, scope: MemoryScopeV1, canonical_key: str
    ) -> MemoryRecordV1 | None: ...

    async def find_recent_active(
        self, *, owner_id: UUID, scope: MemoryScopeV1, limit: int
    ) -> tuple[MemoryRecordV1, ...]: ...

    async def find_pinned(
        self, *, owner_id: UUID, scope: MemoryScopeV1, limit: int
    ) -> tuple[MemoryRecordV1, ...]: ...

    async def find_active_by_conflict_group(
        self, *, owner_id: UUID, conflict_group_id: UUID
    ) -> tuple[MemoryRecordV1, ...]: ...

    async def commit_candidate(self, candidate: GovernedMemoryCandidateV1) -> MemoryRecordV1: ...

    async def append_provenance(
        self, record_id: UUID, provenance: tuple[ProvenanceRefV1, ...]
    ) -> MemoryRecordV1: ...

    async def revise(self, command: MemoryRevisionCommandV1) -> MemoryRecordV1: ...

    async def forget(self, command: MemoryForgetCommandV1) -> ForgetReceiptV1: ...

    async def pin(self, command: MemoryPinCommandV1) -> MemoryRecordV1: ...


__all__ = [
    "ContextPackRepositoryPort",
    "ContextPackReuseHitV1",
    "ContextPackSaveOutcomeV1",
    "ContextPackStateV1",
    "ContextPackTransitionOutcomeV1",
    "ContextPackTransitionResultV1",
    "ForgetReceiptV1",
    "GovernedMemoryCandidateV1",
    "MemoryForgetCommandV1",
    "MemoryPinCommandV1",
    "MemoryRepositoryPort",
    "MemoryRevisionCommandV1",
]
