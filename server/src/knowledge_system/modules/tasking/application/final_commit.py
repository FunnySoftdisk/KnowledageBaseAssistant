"""AGT-08 §5.34 终态事务：`workflow.task_result` 提交写模型与幂等提交结果。

`TaskResultCommitV1` 把 Finalization Episode 各产物压平为单行 `workflow.task_result` 的不可变
提交输入：Task/版本、Answer 状态与内容寻址 Artifact、Criterion/Constraint/Degradation 快照、
Final Snapshot/Attempt、Candidate/Validation/Evaluation/Gate Artifact、Renderer Profile 与
Commit Digest/Receipt。`TaskResultCommitDisposition` 区分首次提交、同一 Commit Digest 的幂等重放
与同版本不同 Digest 的对账冲突——对账冲突不能覆盖既有结果，只能进入对账。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator

from knowledge_system.modules.tasking.domain.core_types import Sha256Hex, StrictContractV1
from knowledge_system.modules.tasking.domain.final_gate import ConstraintCheckV1
from knowledge_system.modules.tasking.domain.final_result import ResultCriterionCheckV1
from knowledge_system.modules.tasking.domain.runtime_decision import FinalOutcomeKindV1


class TaskResultCommitV1(StrictContractV1):
    """不可变 `workflow.task_result` 行提交输入；行内 `contract_version` 固定为
    `task_result_v1`。"""

    task_id: UUID
    result_version: int = Field(ge=1)
    answer_status: FinalOutcomeKindV1
    answer_artifact_id: UUID
    answer_sha256: Sha256Hex
    result_envelope_artifact_id: UUID
    citation_count: int = Field(ge=0, le=200)
    result_artifact_count: int = Field(ge=0, le=50)
    criterion_checks: tuple[ResultCriterionCheckV1, ...] = Field(max_length=12)
    constraint_checks: tuple[ConstraintCheckV1, ...] = Field(max_length=32)
    degradation_flags: tuple[str, ...] = Field(max_length=16)
    final_snapshot_id: UUID
    final_attempt_id: UUID
    candidate_artifact_id: UUID
    validation_artifact_id: UUID
    criterion_evaluation_set_artifact_id: UUID
    completion_gate_decision_artifact_id: UUID
    renderer_profile: str
    renderer_profile_digest: Sha256Hex
    result_commit_digest: Sha256Hex
    commit_receipt_id: UUID
    generated_at: datetime

    @field_validator("generated_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC_DATETIME_REQUIRED")
        return value.astimezone(UTC)


class TaskResultCommitDisposition(StrEnum):
    INSERTED = "INSERTED"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    CONFLICT = "CONFLICT"


class TaskResultCommitResult(StrictContractV1):
    """幂等提交结果：首次提交/同摘要重放/同版本不同摘要对账冲突，附带既有行版本。"""

    disposition: TaskResultCommitDisposition
    task_id: UUID
    result_version: int
    result_commit_digest: Sha256Hex
    row_version: int = Field(ge=1)


__all__ = [
    "TaskResultCommitDisposition",
    "TaskResultCommitResult",
    "TaskResultCommitV1",
]
