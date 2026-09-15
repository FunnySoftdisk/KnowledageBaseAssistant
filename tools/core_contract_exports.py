"""从已批准单一源导出片段；仅stdout/只读核验，不签发Release或READY。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from knowledge_system.modules.model_gateway.application.token_accounting import (
    TOKEN_ACCOUNTING_ADAPTER,
)
from knowledge_system.modules.tasking.domain.core_types import (
    ArtifactRefV1,
    CapabilityRefV1,
    ConstraintRefV1,
    PolicySnapshotRefV1,
    ResourceRefV1,
    SchemaRefV1,
    TaskAttachmentBindingRefV1,
)
from knowledge_system.modules.tasking.domain.goal_contracts import (
    GoalGateDecisionV1,
    GoalUnderstandingContextV1,
    GoalUnderstandingV1,
)
from knowledge_system.modules.tasking.domain.input_contracts import (
    CreateTaskRequestV1,
    TaskInputSnapshotV1,
)
from knowledge_system.modules.tasking.domain.planning_fragments import (
    CRITERION_TARGET_ADAPTER,
    CapabilityPlanningViewV1,
    CapabilityRequirementV1,
    CriterionDraftV1,
    HypothesisDraftV1,
    InitialPlanDraftV1,
    InitialPlanningContextV1,
    InitialPlanValidationV1,
    IterationPolicyV1,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPORT_ROOT = PROJECT_ROOT / "contracts/core"


def serialized(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def draft_2020_12(schema: dict[str, Any]) -> dict[str, Any]:
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}


def export_payloads() -> dict[str, bytes]:
    raw_schemas = {
        "token-accounting-v1.schema.json": TOKEN_ACCOUNTING_ADAPTER.json_schema(),
        "artifact-ref-v1.schema.json": ArtifactRefV1.model_json_schema(),
        "schema-ref-v1.schema.json": SchemaRefV1.model_json_schema(),
        "capability-ref-v1.schema.json": CapabilityRefV1.model_json_schema(),
        "task-attachment-binding-ref-v1.schema.json": (
            TaskAttachmentBindingRefV1.model_json_schema()
        ),
        "resource-ref-v1.schema.json": ResourceRefV1.model_json_schema(),
        "constraint-ref-v1.schema.json": ConstraintRefV1.model_json_schema(),
        "policy-snapshot-ref-v1.schema.json": PolicySnapshotRefV1.model_json_schema(),
        "create-task-request-v1.schema.json": CreateTaskRequestV1.model_json_schema(),
        "task-input-snapshot-v1.schema.json": TaskInputSnapshotV1.model_json_schema(),
        "goal-understanding-context-v1.schema.json": (
            GoalUnderstandingContextV1.model_json_schema()
        ),
        "goal-understanding-v1.schema.json": GoalUnderstandingV1.model_json_schema(),
        "goal-gate-decision-v1.schema.json": GoalGateDecisionV1.model_json_schema(),
        "capability-requirement-v1.schema.json": CapabilityRequirementV1.model_json_schema(),
        "iteration-policy-v1.schema.json": IterationPolicyV1.model_json_schema(),
        "hypothesis-draft-v1.schema.json": HypothesisDraftV1.model_json_schema(),
        "criterion-target-v1.schema.json": CRITERION_TARGET_ADAPTER.json_schema(),
        "criterion-draft-v1.schema.json": CriterionDraftV1.model_json_schema(),
        "initial-plan-draft-v1.schema.json": InitialPlanDraftV1.model_json_schema(),
        "capability-planning-view-v1.schema.json": CapabilityPlanningViewV1.model_json_schema(),
        "initial-planning-context-v1.schema.json": InitialPlanningContextV1.model_json_schema(),
        "initial-plan-validation-v1.schema.json": InitialPlanValidationV1.model_json_schema(),
    }
    schemas = {name: draft_2020_12(schema) for name, schema in raw_schemas.items()}
    payloads = {name: serialized(schema) for name, schema in schemas.items()}
    payloads["manifest.json"] = serialized(
        {
            "scope": "M1_APPROVED_CORE_INPUT_GOAL_PLAN",
            "initial_plan_draft_contract_built": True,
            "pure_semantic_checks_built": True,
            "compiled_criterion_contract_built": False,
            "orm_mapping_built": False,
            "migration_pg_pass": False,
            "full_initial_plan_contract_built": False,
            "release_eligible": False,
            "signature_verified": False,
            "runtime_ready": False,
            "artifacts": [
                {
                    "file": name,
                    "size_bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
                for name, body in sorted(payloads.items())
            ],
        }
    )
    return payloads


def check_exports(directory: Path) -> list[str]:
    failures = []
    for name, expected in export_payloads().items():
        path = directory / name
        try:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
                failures.append(name)
        except OSError:
            failures.append(name)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dump", action="store_true", help="stdout输出源码导出字节供保存"
    )
    args = parser.parse_args()
    if args.dump:
        print(
            json.dumps(
                {name: body.decode("utf-8") for name, body in export_payloads().items()}
            )
        )
        return 0
    failures = check_exports(EXPORT_ROOT)
    print(
        json.dumps(
            {
                "check": "CORE_SOURCE_EXPORT_CONSISTENCY_ONLY",
                "mismatched_files": failures,
                "source_fragment_schemas": len(export_payloads()) - 1,
                "runtime_ready": False,
                "model_call_performed": False,
            }
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
