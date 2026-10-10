"""AGT-08 §5.36 与评测 §Final：`final_synthesis_contract_gold_v1` 契约硬门禁（离线切片）。

90 例金标按三种 Answer 状态各 30 例（`COMPLETE/PARTIAL/INSUFFICIENT_EVIDENCE`），覆盖中英文、
引用/Artifact、冲突/约束、拒批/禁网/能力/预算、注入与越权；最多一次 Repair 后预期状态全正确，
未核验/越权/原子性/Secret/Renderer 非确定性计数均为 0。

本切片冻结金标案例契约、确定性求值器与门禁报告，只驱动零 LLM 确定性链
（`decide_completion`），不接真实模型。签名盲测分片、Replay/故障注入与 Canary 属真实运行，
留待 EXTERNAL_REQUIRED；此处 90 例为离线确定性契约语料，不冒充签名盲测数据。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field

from .compiled_criterion import CompiledCriterionV1
from .core_types import Sha256Hex, StableKey, StrictContractV1, canonical_json_sha256
from .final_gate import (
    ConstraintCheckV1,
    CriterionEvaluationV1,
    decide_completion,
)
from .runtime_decision import FinalOutcomeKindV1

FINAL_SYNTHESIS_CONTRACT_GOLD_PROFILE = "final_synthesis_contract_gold_v1"
FINAL_SYNTHESIS_CONTRACT_GOLD_CASES = 90
FINAL_SYNTHESIS_CONTRACT_GOLD_PER_OUTCOME = 30

FINAL_GOLD_CASE_NAMESPACE = uuid5(
    NAMESPACE_URL, "urn:knowledge-system:final-synthesis-contract-gold:v1"
)

FinalGoldLanguageV1 = Literal["ZH", "EN"]


class FinalGoldCaseCategoryV1(StrEnum):
    CITATION_ARTIFACT = "CITATION_ARTIFACT"
    CONFLICT_CONSTRAINT = "CONFLICT_CONSTRAINT"
    REJECTION_DEGRADATION = "REJECTION_DEGRADATION"
    INJECTION_UNAUTHORIZED = "INJECTION_UNAUTHORIZED"


class FinalGoldCaseV1(StrictContractV1):
    """一条金标案例：确定性完成判定输入 + 期望 Answer 状态 + 覆盖维度 + JCS 摘要。"""

    contract_version: Literal["final_synthesis_contract_gold_v1"]
    case_id: UUID
    expected_outcome: FinalOutcomeKindV1
    language: FinalGoldLanguageV1
    category: FinalGoldCaseCategoryV1
    repair_round: Literal["INITIAL", "AFTER_REPAIR"]
    compiled_criteria: tuple[CompiledCriterionV1, ...] = Field(min_length=1)
    evaluations: tuple[CriterionEvaluationV1, ...] = Field(min_length=1)
    constraint_checks: tuple[ConstraintCheckV1, ...]
    output_contract_complete: bool
    substantive_claims_supported: bool
    has_critical_degradation: bool
    case_digest: Sha256Hex = "0" * 64


class FinalGoldCaseVerdictV1(StrictContractV1):
    """单例门禁结果：确定性链产出状态是否命中期望状态。"""

    case_id: UUID
    passed: bool
    expected_outcome: FinalOutcomeKindV1
    produced_outcome: FinalOutcomeKindV1


class FinalGoldGateReportV1(StrictContractV1):
    """90 例契约门禁聚合报告：三种状态各 30 例全部通过才 `passed=True`。"""

    contract_version: Literal["final_synthesis_gold_gate_report_v1"]
    profile: StableKey
    total_cases: int
    passed_cases: int
    complete_cases: int
    partial_cases: int
    insufficient_evidence_cases: int
    passed: bool
    report_digest: Sha256Hex = "0" * 64


def gold_case_id(outcome: FinalOutcomeKindV1, variant: int) -> UUID:
    """案例 ID 的确定性派生：`UUIDv5(FINAL_GOLD_CASE_NAMESPACE, outcome:variant)`。"""

    return uuid5(FINAL_GOLD_CASE_NAMESPACE, f"{outcome}:{variant}")


def freeze_final_gold_case(case: FinalGoldCaseV1) -> FinalGoldCaseV1:
    digest = canonical_json_sha256(case.model_dump(mode="json", exclude={"case_digest"}))
    return case.model_copy(update={"case_digest": digest})


def evaluate_final_gold_case(case: FinalGoldCaseV1) -> FinalGoldCaseVerdictV1:
    """驱动零 LLM 完成判定，比较产出状态与期望状态。"""

    decision = decide_completion(
        compiled_criteria=case.compiled_criteria,
        evaluations=case.evaluations,
        constraint_checks=case.constraint_checks,
        output_contract_complete=case.output_contract_complete,
        substantive_claims_supported=case.substantive_claims_supported,
        has_critical_degradation=case.has_critical_degradation,
    )
    return FinalGoldCaseVerdictV1(
        case_id=case.case_id,
        passed=decision.decision == case.expected_outcome,
        expected_outcome=case.expected_outcome,
        produced_outcome=decision.decision,
    )


def run_final_gold_gate(cases: tuple[FinalGoldCaseV1, ...]) -> FinalGoldGateReportV1:
    """校验 90 例/三态各 30 的结构后逐例求值，任一不通过即 `passed=False`。"""

    if len(cases) != FINAL_SYNTHESIS_CONTRACT_GOLD_CASES:
        raise ValueError("GOLD_GATE_CASE_COUNT_MISMATCH")
    case_ids = [case.case_id for case in cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("GOLD_GATE_DUPLICATE_CASE")
    per_outcome = {outcome: 0 for outcome in FinalOutcomeKindV1}
    for case in cases:
        per_outcome[case.expected_outcome] += 1
    if per_outcome != {
        FinalOutcomeKindV1.COMPLETE: FINAL_SYNTHESIS_CONTRACT_GOLD_PER_OUTCOME,
        FinalOutcomeKindV1.PARTIAL: FINAL_SYNTHESIS_CONTRACT_GOLD_PER_OUTCOME,
        FinalOutcomeKindV1.INSUFFICIENT_EVIDENCE: FINAL_SYNTHESIS_CONTRACT_GOLD_PER_OUTCOME,
    }:
        raise ValueError("GOLD_GATE_OUTCOME_DISTRIBUTION_MISMATCH")

    passed_cases = sum(1 for case in cases if evaluate_final_gold_case(case).passed)
    report = FinalGoldGateReportV1(
        contract_version="final_synthesis_gold_gate_report_v1",
        profile=FINAL_SYNTHESIS_CONTRACT_GOLD_PROFILE,
        total_cases=len(cases),
        passed_cases=passed_cases,
        complete_cases=per_outcome[FinalOutcomeKindV1.COMPLETE],
        partial_cases=per_outcome[FinalOutcomeKindV1.PARTIAL],
        insufficient_evidence_cases=per_outcome[FinalOutcomeKindV1.INSUFFICIENT_EVIDENCE],
        passed=passed_cases == len(cases),
        report_digest="0" * 64,
    )
    digest = canonical_json_sha256(report.model_dump(mode="json", exclude={"report_digest"}))
    return report.model_copy(update={"report_digest": digest})


__all__ = [
    "FINAL_GOLD_CASE_NAMESPACE",
    "FINAL_SYNTHESIS_CONTRACT_GOLD_CASES",
    "FINAL_SYNTHESIS_CONTRACT_GOLD_PER_OUTCOME",
    "FINAL_SYNTHESIS_CONTRACT_GOLD_PROFILE",
    "FinalGoldCaseCategoryV1",
    "FinalGoldCaseV1",
    "FinalGoldCaseVerdictV1",
    "FinalGoldGateReportV1",
    "FinalGoldLanguageV1",
    "evaluate_final_gold_case",
    "freeze_final_gold_case",
    "gold_case_id",
    "run_final_gold_gate",
]
