"""DEV-08：三作用域记忆的领域契约（零 LLM/零 I/O）。

权威设计：模块16《模型上下文与记忆管理模块详细设计》§3.2/§3.3/§4.1/§7.2 与 ADR-0004
《Memory框架与存储基线》。本文件只冻结领域类型与枚举；不访问模型、网络、ORM 或 Temporal。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator

from knowledge_system.modules.tasking.domain.core_types import (
    Sha256Hex,
    StableKey,
    StrictContractV1,
    canonical_json_sha256,
)


class MemoryScopeV1(StrEnum):
    """三作用域；无 TEAM/SHARED。团队共享事实进入审核知识库（ADR-0004 §3.2）。"""

    TASK = "TASK"
    CONVERSATION = "CONVERSATION"
    USER_PRIVATE = "USER_PRIVATE"


class MemoryKindV1(StrEnum):
    GOAL = "GOAL"
    CONSTRAINT = "CONSTRAINT"
    DEFINITION = "DEFINITION"
    DECISION = "DECISION"
    PREFERENCE = "PREFERENCE"
    ENTITY_BINDING = "ENTITY_BINDING"
    OPEN_QUESTION = "OPEN_QUESTION"
    RESOLUTION = "RESOLUTION"
    SUPPORTED_FINDING = "SUPPORTED_FINDING"


class MemoryStateV1(StrEnum):
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    RETRACTED = "RETRACTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    QUARANTINED = "QUARANTINED"


class MemoryTrustV1(StrEnum):
    USER_EXPLICIT = "USER_EXPLICIT"
    USER_DERIVED = "USER_DERIVED"
    ASSISTANT_DERIVED = "ASSISTANT_DERIVED"
    EVIDENCE_SUPPORTED = "EVIDENCE_SUPPORTED"


class ProvenanceSourceTypeV1(StrEnum):
    MESSAGE = "MESSAGE"
    MEMORY_COMMAND = "MEMORY_COMMAND"
    TASK_ARTIFACT = "TASK_ARTIFACT"
    EVIDENCE = "EVIDENCE"


class DuplicateVerdictV1(StrEnum):
    SAME = "SAME"
    OVERLAP = "OVERLAP"
    CONFLICT = "CONFLICT"
    UNRELATED = "UNRELATED"


class CorrectionDispositionV1(StrEnum):
    NEW = "NEW"
    MERGE = "MERGE"
    CONFLICT = "CONFLICT"
    CORRECT = "CORRECT"


class MemoryChangeKindV1(StrEnum):
    """版本递增语义（§7.4）：按是否改变可召回视图区分。"""

    ACTIVATED = "ACTIVATED"
    CORRECTED = "CORRECTED"
    CONFLICT_MARKED = "CONFLICT_MARKED"
    PIN_CHANGED = "PIN_CHANGED"
    SCOPE_CHANGED = "SCOPE_CHANGED"
    TRUST_CHANGED = "TRUST_CHANGED"
    VALIDITY_CHANGED = "VALIDITY_CHANGED"
    INVALIDATED = "INVALIDATED"
    FORGOTTEN = "FORGOTTEN"
    PROVENANCE_REORDERED = "PROVENANCE_REORDERED"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    PROPOSED_ONLY = "PROPOSED_ONLY"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"
    EMBEDDING_REBUILD = "EMBEDDING_REBUILD"


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC_DATETIME_REQUIRED")
    return value.astimezone(UTC)


class ProvenanceRefV1(StrictContractV1):
    """记忆/摘要对应的来源引用；只携带身份与内容哈希，不授予权限。"""

    source_type: ProvenanceSourceTypeV1
    source_id: str
    source_version: str | None = None
    content_hash: Sha256Hex
    message_sequence: int | None = Field(default=None, ge=0)


class MemoryVersionSnapshotV1(StrictContractV1):
    """三个聚合的版本组合快照（§3.3）；`memory_versions_digest` 按字段顺序内容寻址。"""

    conversation_version: int = Field(default=0, ge=0)
    user_private_version: int = Field(default=0, ge=0)
    task_version: int = Field(default=0, ge=0)
    memory_versions_digest: Sha256Hex = "0" * 64


def memory_versions_digest_for(snapshot: MemoryVersionSnapshotV1) -> str:
    """§3.3：按 conversation/user_private/task 顺序以 RFC 8785 JCS+SHA-256 计算。"""

    return canonical_json_sha256(
        [snapshot.conversation_version, snapshot.user_private_version, snapshot.task_version]
    )


def freeze_memory_version_snapshot(
    snapshot: MemoryVersionSnapshotV1,
) -> MemoryVersionSnapshotV1:
    return snapshot.model_copy(
        update={"memory_versions_digest": memory_versions_digest_for(snapshot)}
    )


class MemoryProposalV1(StrictContractV1):
    """提取器输出的候选记忆（§7.2）；永远是 PROPOSED，不直接成为 ACTIVE 或进入 Context。"""

    scope: MemoryScopeV1
    kind: MemoryKindV1
    canonical_key: StableKey | None = None
    statement: str
    importance: float = Field(ge=0, le=1)
    valid_until: datetime | None = None
    trust: MemoryTrustV1
    provenance: tuple[ProvenanceRefV1, ...] = Field(min_length=1)
    correction_of: UUID | None = None

    @field_validator("valid_until")
    @classmethod
    def normalize_valid_until(cls, value: datetime | None) -> datetime | None:
        return _normalize_utc(value)


class MemoryRecordV1(StrictContractV1):
    """权威记忆记录（§4.1）；`schema_version`/`revision` 表达结构版本与追加修订。"""

    id: UUID
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
    pinned: bool = False
    valid_from: datetime
    valid_until: datetime | None = None
    supersedes_id: UUID | None = None
    conflict_group_id: UUID | None = None
    provenance: tuple[ProvenanceRefV1, ...] = Field(min_length=1)
    schema_version: int = Field(ge=1)
    revision: int = Field(ge=1)

    @field_validator("valid_from", "valid_until")
    @classmethod
    def normalize_validity(cls, value: datetime | None) -> datetime | None:
        return _normalize_utc(value)


class MemoryGovernanceViolationV1(StrictContractV1):
    """写入治理校验失败关闭的错误形状。"""

    code: str
    field: str | None = None
    detail: str


class DuplicateResolutionV1(StrictContractV1):
    """DuplicateResolver 判定（§8.2.1）；必须回填差异字段证据，不能只给结论。"""

    verdict: DuplicateVerdictV1
    differing_fields: tuple[str, ...] = ()
    rationale: str


class CorrectionPlanV1(StrictContractV1):
    """同 Key 纠正/冲突决策产物（§7.4）。"""

    disposition: CorrectionDispositionV1
    supersedes_id: UUID | None = None
    conflict_group_id: UUID | None = None
    append_provenance: bool = False


__all__ = [
    "CorrectionDispositionV1",
    "CorrectionPlanV1",
    "DuplicateResolutionV1",
    "DuplicateVerdictV1",
    "MemoryChangeKindV1",
    "MemoryGovernanceViolationV1",
    "MemoryKindV1",
    "MemoryProposalV1",
    "MemoryRecordV1",
    "MemoryScopeV1",
    "MemoryStateV1",
    "MemoryTrustV1",
    "MemoryVersionSnapshotV1",
    "ProvenanceRefV1",
    "ProvenanceSourceTypeV1",
    "freeze_memory_version_snapshot",
    "memory_versions_digest_for",
]
