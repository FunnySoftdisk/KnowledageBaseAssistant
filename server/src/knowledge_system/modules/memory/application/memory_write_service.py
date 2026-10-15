"""DEV-08 子批次 2：记忆写入确定性编排（零 LLM/零 I/O）。

把子批次 1 的写入治理纯规则按模块16 §7.3 顺序编排为 `validate_and_commit`：
来源/所有权（Provenance 存在性）→ Scope/Trust 合法性 → 敏感信息/DLP → 确定性规范化与精确去重
→ 同 Key 冲突/纠正 → 有效期 → ACTIVE/QUARANTINED 裁决，最终投影为 `GovernedMemoryCandidateV1`
交 `MemoryRepositoryPort` 提交。近似重复候选（需 Embedding/检索）、语义级「模糊 vs 明确」精判、
`correction_of` 按 ID 的完整存在性校验（需 `find_by_id`）均留后续切片。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from knowledge_system.modules.tasking.domain.core_types import Sha256Hex, StrictContractV1

from ..domain.memory_governance import (
    assess_proposal_legality,
    clamp_importance,
    compute_canonical_hash,
    normalize_memory_statement,
    plan_correction_or_conflict,
    reject_sensitive_content,
)
from ..domain.memory_types import (
    CorrectionDispositionV1,
    MemoryGovernanceViolationV1,
    MemoryKindV1,
    MemoryProposalV1,
    MemoryRecordV1,
    MemoryStateV1,
    MemoryTrustV1,
)
from .errors import MemoryError
from .ports import GovernedMemoryCandidateV1, MemoryRepositoryPort


class MemoryWriteOutcomeV1(StrEnum):
    ACTIVATED = "ACTIVATED"
    QUARANTINED = "QUARANTINED"
    MERGED = "MERGED"
    REJECTED = "REJECTED"


class MemoryWriteResultV1(StrictContractV1):
    """`validate_and_commit` 的结果：终局 + 已提交/复用的记录 + 拒绝证据。"""

    outcome: MemoryWriteOutcomeV1
    record: MemoryRecordV1 | None = None
    violations: tuple[MemoryGovernanceViolationV1, ...] = ()
    importance_clamped: bool = False
    disposition: CorrectionDispositionV1 | None = None
    canonical_hash: Sha256Hex | None = None


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MemoryError("UTC_DATETIME_REQUIRED")
    return value.astimezone(UTC)


def _resolve_activation_state(proposal: MemoryProposalV1, *, explicit: bool) -> MemoryStateV1:
    """§7.3：明确、安全、来源完整 → ACTIVE；推断/模糊 → QUARANTINED（确定性信任代理）。"""

    if explicit or proposal.trust is MemoryTrustV1.USER_EXPLICIT:
        return MemoryStateV1.ACTIVE
    if (
        proposal.trust is MemoryTrustV1.EVIDENCE_SUPPORTED
        and proposal.kind is MemoryKindV1.SUPPORTED_FINDING
    ):
        return MemoryStateV1.ACTIVE
    if proposal.trust is MemoryTrustV1.USER_DERIVED:
        return MemoryStateV1.ACTIVE
    return MemoryStateV1.QUARANTINED


class MemoryWriteService:
    """确定性写入编排：候选 → 治理 Gate → 提交/隔离/合并/拒绝。"""

    def __init__(self, repository: MemoryRepositoryPort) -> None:
        self._repository = repository

    async def validate_and_commit(
        self,
        proposal: MemoryProposalV1,
        *,
        owner_id: UUID,
        conversation_id: UUID | None,
        task_id: UUID | None,
        explicit: bool,
        now: datetime,
    ) -> MemoryWriteResultV1:
        now = _require_utc(now)

        # §7.3-3：Scope 与 Trust 合法
        legality = assess_proposal_legality(proposal, explicit=explicit)
        if legality:
            return MemoryWriteResultV1(
                outcome=MemoryWriteOutcomeV1.REJECTED, violations=legality
            )

        # §7.3-4：敏感信息/DLP
        if reject_sensitive_content(proposal.statement):
            return MemoryWriteResultV1(
                outcome=MemoryWriteOutcomeV1.REJECTED,
                violations=(
                    MemoryGovernanceViolationV1(
                        code="SENSITIVE_CONTENT_REJECTED",
                        field="statement",
                        detail="密码/Token/API Key/私钥/安全策略命中内容直接拒绝",
                    ),
                ),
            )

        # §7.3-5：确定性规范化 + 精确去重
        clamped_importance, was_clamped = clamp_importance(
            proposal.trust, proposal.kind, explicit=explicit, proposed=proposal.importance
        )
        canonical_hash = compute_canonical_hash(
            proposal.scope,
            proposal.kind,
            proposal.canonical_key,
            normalize_memory_statement(proposal.statement),
        )
        existing = await self._repository.find_active_by_hash(
            owner_id=owner_id, scope=proposal.scope, canonical_hash=canonical_hash
        )
        if existing is not None:
            merged = await self._repository.append_provenance(existing.id, proposal.provenance)
            return MemoryWriteResultV1(
                outcome=MemoryWriteOutcomeV1.MERGED,
                record=merged,
                importance_clamped=was_clamped,
                disposition=CorrectionDispositionV1.MERGE,
                canonical_hash=canonical_hash,
            )

        # §7.3-6：同 Key 冲突/纠正
        disposition = CorrectionDispositionV1.NEW
        supersedes_id: UUID | None = None
        conflict_group_id: UUID | None = None
        if proposal.canonical_key is not None:
            existing = await self._repository.find_active_by_key(
                owner_id=owner_id, scope=proposal.scope, canonical_key=proposal.canonical_key
            )
            if existing is not None:
                plan = plan_correction_or_conflict(proposal, existing)
                disposition = plan.disposition
                supersedes_id = plan.supersedes_id
                conflict_group_id = plan.conflict_group_id
                if disposition is CorrectionDispositionV1.MERGE:
                    merged = await self._repository.append_provenance(
                        existing.id, proposal.provenance
                    )
                    return MemoryWriteResultV1(
                        outcome=MemoryWriteOutcomeV1.MERGED,
                        record=merged,
                        importance_clamped=was_clamped,
                        disposition=disposition,
                        canonical_hash=canonical_hash,
                    )
                if disposition is CorrectionDispositionV1.CONFLICT and conflict_group_id is None:
                    conflict_group_id = uuid4()

        # §7.3-8：有效期
        if proposal.valid_until is not None and proposal.valid_until <= now:
            return MemoryWriteResultV1(
                outcome=MemoryWriteOutcomeV1.REJECTED,
                violations=(
                    MemoryGovernanceViolationV1(
                        code="MEMORY_VALID_UNTIL_IN_PAST",
                        field="valid_until",
                        detail="候选 valid_until 不晚于写入时刻",
                    ),
                ),
                importance_clamped=was_clamped,
                canonical_hash=canonical_hash,
            )

        # §7.3-9：ACTIVE 或 QUARANTINED（纠正/冲突必为 ACTIVE，双方保留）
        if disposition in (
            CorrectionDispositionV1.CORRECT,
            CorrectionDispositionV1.CONFLICT,
        ):
            state = MemoryStateV1.ACTIVE
        else:
            state = _resolve_activation_state(proposal, explicit=explicit)

        candidate = GovernedMemoryCandidateV1(
            owner_id=owner_id,
            conversation_id=conversation_id,
            task_id=task_id,
            scope=proposal.scope,
            kind=proposal.kind,
            state=state,
            trust=proposal.trust,
            canonical_key=proposal.canonical_key,
            content=proposal.statement,
            canonical_hash=canonical_hash,
            importance=clamped_importance,
            valid_from=now,
            valid_until=proposal.valid_until,
            supersedes_id=supersedes_id,
            conflict_group_id=conflict_group_id,
            provenance=proposal.provenance,
        )
        record = await self._repository.commit_candidate(candidate)
        outcome = (
            MemoryWriteOutcomeV1.ACTIVATED
            if state is MemoryStateV1.ACTIVE
            else MemoryWriteOutcomeV1.QUARANTINED
        )
        return MemoryWriteResultV1(
            outcome=outcome,
            record=record,
            importance_clamped=was_clamped,
            disposition=disposition,
            canonical_hash=canonical_hash,
        )


__all__ = [
    "MemoryWriteOutcomeV1",
    "MemoryWriteResultV1",
    "MemoryWriteService",
]
