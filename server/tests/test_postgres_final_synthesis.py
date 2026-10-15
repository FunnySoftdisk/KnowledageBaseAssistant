"""真实PostgreSQL上 Final 综合 → 终态提交的垂直切片验收。

以 `SqlAlchemyTaskResultWriter` + `FakeFinalModel` 驱动 `FinalSynthesisService`：真实候选装配、
校验、求值、引用、渲染、装配后经幂等提交落库。先以离线 Recording 替身干跑一次拿到内容寻址的
Answer/Envelope Artifact ID，再据此落库依赖 Artifact，随后以真实 Writer 走通
`FINAL_COMMITTED(INSERTED)` 与同候选重放 `IDEMPOTENT_REPLAY`。
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.task_result_writer import (
    SqlAlchemyTaskResultWriter,
)
from knowledge_system.modules.tasking.application.final_commit import (
    TaskResultCommitDisposition,
    TaskResultCommitResult,
    TaskResultCommitV1,
)
from knowledge_system.modules.tasking.application.final_synthesis import (
    FinalSynthesisMaterials,
    FinalSynthesisService,
)
from knowledge_system.modules.tasking.domain.compiled_criterion import (
    EVALUATOR_PROFILE_EVIDENCE_GROUNDED,
    CompiledCriterionV1,
    EvidenceSupportPredicateV1,
)
from knowledge_system.modules.tasking.domain.core_types import (
    PolicySnapshotRefV1,
    SchemaRefV1,
    VersionedRefV1,
)
from knowledge_system.modules.tasking.domain.final_admission import FinalSupportCatalogEntryV1
from knowledge_system.modules.tasking.domain.final_candidate import (
    FinalClaimDraftV1,
    FinalParagraphBlockV1,
    FinalSectionDraftV1,
    FinalSynthesisCandidateV1,
    FinalSynthesisModelOutputV1,
    SemanticCriterionAssessmentV1,
    assemble_final_candidate,
)
from knowledge_system.modules.tasking.domain.final_citation import CitationSourceV1
from knowledge_system.modules.tasking.domain.final_evaluation import CriterionEvaluationStateV1
from knowledge_system.modules.tasking.domain.final_synthesis import (
    FinalProtectedBudgetV1,
    FinalSynthesisSnapshotV1,
    freeze_final_synthesis_snapshot,
)
from knowledge_system.modules.tasking.domain.input_contracts import OutputContractV1
from knowledge_system.modules.tasking.domain.planning_fragments import CriterionSourceKindV1

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
DIGEST = "a" * 64
ZERO_SHA = "0" * 64
GENERATED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _vref(ref_id: UUID) -> VersionedRefV1:
    return VersionedRefV1(ref_id=ref_id, version=1, integrity_digest=ZERO_SHA)


def _criterion() -> CompiledCriterionV1:
    return CompiledCriterionV1(
        criterion_id=uuid4(),
        statement="关键事实有据可查",
        source_kind=CriterionSourceKindV1.GOAL,
        source_refs=("gc1",),
        requirement_level="REQUIRED",
        completion_role="CORE_DELIVERABLE",
        protected=True,
        evaluation_mode="EVIDENCE_GROUNDED_SEMANTIC",
        predicate=EvidenceSupportPredicateV1(
            kind="EVIDENCE_SUPPORT", producer_plan_item_id=uuid4(), min_citations=1
        ),
        applicability=None,
        evaluator_profile=EVALUATOR_PROFILE_EVIDENCE_GROUNDED,
        digest=ZERO_SHA,
    )


def _contract() -> OutputContractV1:
    return OutputContractV1(
        format="MARKDOWN",
        delivery="INLINE",
        language="zh-CN",
        schema_ref=None,
        template_or_skill_ref=None,
        citations="OPTIONAL",
        max_output_bytes=100_000,
    )


def make_snapshot(task_id: UUID) -> FinalSynthesisSnapshotV1:
    return freeze_final_synthesis_snapshot(
        FinalSynthesisSnapshotV1(
            contract_version="final_synthesis_snapshot_v1",
            task_id=task_id,
            runtime_decision_id=uuid4(),
            input_id=uuid4(),
            plan_version=1,
            work_version=1,
            execution_generation=0,
            cancellation_generation=0,
            event_high_watermark=0,
            compiled_criteria=(_criterion(),),
            effective_constraint_refs=(),
            output_contract=_contract(),
            adopted_result_refs=(),
            gap_summaries=(),
            conflict_ids=(),
            unresolved_subgoal_ids=(),
            permanent_blocker_ids=(),
            citation_candidate_catalog_ids=(),
            result_artifact_candidate_catalog_ids=(),
            policy_snapshot_ref=PolicySnapshotRefV1(
                policy_snapshot_id=uuid4(),
                policy_revision=1,
                policy_digest=ZERO_SHA,
                observed_at=datetime.now(UTC),
            ),
            source_visibility=(),
            authorization_revoked=False,
            degraded=False,
            final_protected_budget=FinalProtectedBudgetV1(
                model_requests=2, input_tokens=0, output_tokens=0
            ),
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
            builder_profile="final-synthesis-context-builder-v1",
            builder_schema_ref=SchemaRefV1(
                schema_id="final-synthesis", schema_version="v1", schema_digest=ZERO_SHA
            ),
            snapshot_digest=ZERO_SHA,
        )
    )


def make_catalog() -> tuple[FinalSupportCatalogEntryV1, ...]:
    return (
        FinalSupportCatalogEntryV1(
            catalog_id="vc1",
            kind="VERIFIED_CLAIM",
            source_ref=_vref(UUID("00000000-0000-4000-8000-000000000900")),
            canonical_text="事实A",
            verdict="SUPPORTED",
            artifact_kind=None,
            evidence_ref=_vref(UUID("00000000-0000-4000-8000-000000000001")),
        ),
    )


def make_sources() -> tuple[CitationSourceV1, ...]:
    return (
        CitationSourceV1(
            evidence_ref_id=UUID("00000000-0000-4000-8000-000000000001"),
            source_domain="EXTERNAL_WEB",
            locator_kind="WEB",
            locator_value="https://example.com",
            preview=None,
        ),
    )


def make_candidate(snap: FinalSynthesisSnapshotV1) -> FinalSynthesisCandidateV1:
    criterion_id = snap.compiled_criteria[0].criterion_id
    output = FinalSynthesisModelOutputV1(
        contract_version="final_synthesis_model_output_v1",
        title="报告标题",
        claims=(
            FinalClaimDraftV1(
                local_claim_id="c1",
                claim_kind="SOURCE_FACT",
                text="事实A",
                support_catalog_ids=("vc1",),
                criterion_ids=(),
                conflict_set_ids=(),
            ),
        ),
        sections=(
            FinalSectionDraftV1(
                local_section_id="s1",
                heading="概述",
                blocks=(FinalParagraphBlockV1(block_kind="PARAGRAPH", claim_local_ids=("c1",)),),
            ),
        ),
        semantic_criterion_assessments=(
            SemanticCriterionAssessmentV1(
                criterion_id=criterion_id,
                assessment="SUPPORTED",
                supporting_catalog_ids=("vc1",),
                claim_local_ids=("c1",),
                note="证据支持",
            ),
        ),
        gap_reports=(),
        selected_result_artifact_catalog_ids=(),
    )
    return assemble_final_candidate(output, snap)


def make_materials(
    snap: FinalSynthesisSnapshotV1,
    artifact_ids: dict[str, UUID],
) -> FinalSynthesisMaterials:
    return FinalSynthesisMaterials(
        snapshot=snap,
        catalog=make_catalog(),
        citation_sources=make_sources(),
        evaluation_state=CriterionEvaluationStateV1(
            completed_item_ids=(), adopted_outputs=(), constraint_checks=()
        ),
        degradation_flags=(),
        output_contract_complete=True,
        final_attempt_id=uuid4(),
        candidate_artifact_id=artifact_ids["candidate"],
        validation_artifact_id=artifact_ids["validation"],
        criterion_evaluation_set_artifact_id=artifact_ids["evaluation_set"],
        completion_gate_decision_artifact_id=artifact_ids["gate_decision"],
        generated_at=GENERATED_AT,
    )


class RecordingCommitWriter:
    def __init__(self) -> None:
        self.commits: list[TaskResultCommitV1] = []

    async def commit(self, commit: TaskResultCommitV1) -> TaskResultCommitResult:
        self.commits.append(commit)
        return TaskResultCommitResult(
            disposition=TaskResultCommitDisposition.INSERTED,
            task_id=commit.task_id,
            result_version=commit.result_version,
            result_commit_digest=commit.result_commit_digest,
            row_version=1,
        )


class FakeFinalModel:
    def __init__(self, candidate: FinalSynthesisCandidateV1) -> None:
        self._candidate = candidate

    async def generate_candidate(self, snapshot, catalog, repair_hints=()):
        return self._candidate


async def seed_foundation(
    engine: AsyncEngine, ids: dict[str, UUID], artifacts: dict[str, UUID]
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO iam.organization (id,name,status,version) "
                "VALUES (:id,:name,'ACTIVE',1)"
            ),
            {"id": ids["org"], "name": f"test-{ids['org']}"},
        )
        username = f"u-{ids['user']}"
        await connection.execute(
            text(
                "INSERT INTO iam.user_account "
                "(id,organization_id,username,normalized_username,display_name,password_hash,"
                "status,must_change_password,failed_login_count,credential_version,version) "
                "VALUES (:id,:org,:username,:username,:username,'hash','ACTIVE',false,0,1,1)"
            ),
            {"id": ids["user"], "org": ids["org"], "username": username},
        )
        for artifact_id in artifacts.values():
            await connection.execute(
                text(
                    "INSERT INTO content.artifact "
                    "(artifact_id,organization_id,owner_id,purpose,artifact_type,storage_backend,"
                    "object_key,size_bytes,sha256,detected_media_type,schema_id,schema_version,"
                    "schema_digest,data_labels_json,encryption_profile,state,legal_hold,row_version,"
                    "created_by_actor_id) VALUES "
                    "(:id,:org,:owner,'QUERY','TEXT','S3',:object_key,1,:digest,'text/plain',"
                    "'schema','v1',:digest,'{}'::jsonb,'dev','AVAILABLE',false,1,:owner)"
                ),
                {
                    "id": artifact_id,
                    "org": ids["org"],
                    "owner": ids["user"],
                    "object_key": f"test/{artifact_id}",
                    "digest": DIGEST,
                },
            )
        await connection.execute(
            text(
                "INSERT INTO workflow.conversation "
                "(id,owner_id,organization_id,title,mode,conversation_memory_version,"
                "memory_policy) VALUES (:id,:owner,:org,'test','internal',0,'OFF')"
            ),
            {"id": ids["conversation"], "owner": ids["user"], "org": ids["org"]},
        )
        deadline = datetime.now(UTC) + timedelta(hours=1)
        await connection.execute(
            text(
                "INSERT INTO workflow.intelligent_task "
                "(id,organization_id,owner_id,conversation_id,query_artifact_id,"
                "initial_input_id,current_input_id,authorization_ref_id,status,"
                "temporal_workflow_id,last_event_sequence,budget,budget_used,deadline_at,version) "
                "VALUES (:id,:org,:owner,:conversation,:query,:input,:input,:auth,'PLANNING',"
                ":temporal,0,'{}'::jsonb,'{}'::jsonb,:deadline,1)"
            ),
            {
                "id": ids["task"],
                "org": ids["org"],
                "owner": ids["user"],
                "conversation": ids["conversation"],
                "query": artifacts["query"],
                "input": ids["input"],
                "auth": uuid4(),
                "temporal": f"agent-task-v1-{ids['task']}",
                "deadline": deadline,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.conversation_message "
                "(id,conversation_id,role,message_kind,content_artifact_id,task_id,sequence,"
                "revision,content_hash,state) VALUES "
                "(:id,:conversation,'USER','USER_QUERY',:artifact,:task,1,1,:digest,'ACTIVE')"
            ),
            {
                "id": ids["message"],
                "conversation": ids["conversation"],
                "artifact": artifacts["query"],
                "task": ids["task"],
                "digest": DIGEST,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.task_input_snapshot "
                "(input_id,task_id,input_revision,message_id,message_artifact_id,actor_id,"
                "organization_id,conversation_id,attachment_binding_refs_json,"
                "knowledge_scope_json,network_policy,network_policy_source,output_contract_json,"
                "selected_resource_refs_json,task_constraint_refs_json,policy_snapshot_ref_json,"
                "locale,timezone,contract_version,snapshot_digest) VALUES "
                "(:input,:task,1,:message,:query,:owner,:org,:conversation,"
                "'[]'::jsonb,'{}'::jsonb,'DENY','SYSTEM_DEFAULT','{}'::jsonb,"
                "'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,'zh-CN','Asia/Shanghai',"
                "'task_input_snapshot_v1',:digest)"
            ),
            {
                "input": ids["input"],
                "task": ids["task"],
                "message": ids["message"],
                "query": artifacts["query"],
                "owner": ids["user"],
                "org": ids["org"],
                "conversation": ids["conversation"],
                "digest": DIGEST,
            },
        )


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class PostgresFinalSynthesisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.engine = build_async_engine(DatabaseEngineSettings(TEST_DATABASE_URL))
        self.session_factory = build_session_factory(self.engine)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_final_commit_inserted_then_idempotent_replay(self) -> None:
        task_id = uuid4()
        snapshot = make_snapshot(task_id)
        candidate = make_candidate(snapshot)
        materials = make_materials(
            snapshot,
            {
                "candidate": uuid4(),
                "validation": uuid4(),
                "evaluation_set": uuid4(),
                "gate_decision": uuid4(),
            },
        )

        # 干跑一次拿到内容寻址的 Answer/Envelope Artifact ID，据此落库依赖。
        dry_writer = RecordingCommitWriter()
        await FinalSynthesisService(FakeFinalModel(candidate), dry_writer).finalize(materials)
        commit = dry_writer.commits[0]

        ids = {
            name: uuid4()
            for name in ("org", "user", "conversation", "task", "message", "input")
        }
        artifacts = {
            "query": uuid4(),
            "answer": commit.answer_artifact_id,
            "envelope": commit.result_envelope_artifact_id,
            "candidate": commit.candidate_artifact_id,
            "validation": commit.validation_artifact_id,
            "evaluation_set": commit.criterion_evaluation_set_artifact_id,
            "gate_decision": commit.completion_gate_decision_artifact_id,
        }
        ids["task"] = task_id
        await seed_foundation(self.engine, ids, artifacts)

        writer = SqlAlchemyTaskResultWriter(self.session_factory)
        service = FinalSynthesisService(FakeFinalModel(candidate), writer)

        first = await service.finalize(materials)
        self.assertEqual(first.outcome, "FINAL_COMMITTED")
        self.assertEqual(first.commit_disposition, TaskResultCommitDisposition.INSERTED)
        self.assertRegex(first.result_commit_digest, r"^[0-9a-f]{64}$")

        replay = await service.finalize(materials)
        self.assertEqual(replay.outcome, "FINAL_COMMITTED")
        self.assertEqual(replay.commit_disposition, TaskResultCommitDisposition.IDEMPOTENT_REPLAY)

        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT answer_status, citation_count, result_commit_digest, row_version "
                        "FROM workflow.task_result WHERE task_id=:task"
                    ),
                    {"task": task_id},
                )
            ).one()
        self.assertEqual(row[0], "COMPLETE")
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], commit.result_commit_digest)
        self.assertEqual(row[3], 1)


if __name__ == "__main__":
    unittest.main()
