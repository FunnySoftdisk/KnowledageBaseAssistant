"""DEV-06 Final 综合 Canary 的离线盲测：不接真实模型/PG/OpenBao/Bifrost。

把 `final_synthesis_canary.py` 的构建器（`make_snapshot`/`make_catalog`/`make_sources`/
`make_materials`）与 `FINAL_SYSTEM_PROMPT` 接上 `PydanticFinalModel(FunctionModel 替身)` +
`FinalSynthesisService` + 记录式提交端口，证明：Canary 的冻结快照、支持目录、引用源与确定性
系统提示是自洽的——一个严格遵循提示契约的真实模型输出（逐字复制 canonical_text 与 criterion_id）
能确定性地穿过 Claim 支持校验、语义 Criterion 求值、Citation 解析、渲染装配，直到
`FINAL_COMMITTED`/`COMPLETE`，从而在花真实付费调用前把穿链风险降到最低。
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from knowledge_system.final_synthesis_canary import (
    CANONICAL_TEXT,
    FINAL_SYSTEM_PROMPT,
    build_canonical_output,
    make_catalog,
    make_materials,
    make_snapshot,
)
from knowledge_system.infrastructure.final_model import PydanticFinalModel
from knowledge_system.modules.tasking.application.final_commit import (
    TaskResultCommitDisposition,
    TaskResultCommitResult,
    TaskResultCommitV1,
)
from knowledge_system.modules.tasking.application.final_synthesis import FinalSynthesisService
from knowledge_system.modules.tasking.domain.runtime_decision import FinalOutcomeKindV1


class RecordingCommitWriter:
    def __init__(self, disposition: str = "INSERTED"):
        self.disposition = TaskResultCommitDisposition(disposition)
        self.commits: list[TaskResultCommitV1] = []

    async def commit(self, commit: TaskResultCommitV1) -> TaskResultCommitResult:
        self.commits.append(commit)
        return TaskResultCommitResult(
            disposition=self.disposition,
            task_id=commit.task_id,
            result_version=commit.result_version,
            result_commit_digest=commit.result_commit_digest,
            row_version=1,
        )


def _responder(payload: str):
    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert messages, "FunctionModel responder received no messages"
        assert info.function_tools == [], "Final synthesis must expose no tools"
        return ModelResponse([TextPart(payload)])

    return respond


class CanaryContractSelfConsistencyTests(unittest.TestCase):
    def test_catalog_canonical_text_matches_constant(self) -> None:
        # 目录里唯一 VERIFIED_CLAIM 的 canonical_text 必须与常量一致，否则提示契约里的
        # 「逐字复制 catalog_id=="vc1" 的 canonical_text」对不上目录内容。
        catalog = make_catalog()
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0].catalog_id, "vc1")
        self.assertEqual(catalog[0].canonical_text, CANONICAL_TEXT)

    def test_system_prompt_pins_deterministic_contract(self) -> None:
        # 提示必须显式钉死逐字复制 canonical_text 与 criterion_id、目录/本地 ID 白名单。
        for token in ("canonical_text", "criterion_id", "vc1", '"c1"', "SUPPORTED"):
            self.assertIn(token, FINAL_SYSTEM_PROMPT)

    def test_canonical_output_satisfies_prompt_contract(self) -> None:
        snap = make_snapshot(uuid4())
        output = build_canonical_output(snap)
        self.assertEqual(output.claims[0].text, CANONICAL_TEXT)
        self.assertEqual(output.claims[0].support_catalog_ids, ("vc1",))
        self.assertEqual(
            output.semantic_criterion_assessments[0].criterion_id,
            snap.compiled_criteria[0].criterion_id,
        )
        self.assertEqual(output.sections[0].blocks[0].claim_local_ids, ("c1",))


class CanaryEndToEndBlindTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_compliant_candidate_reaches_commit(self) -> None:
        snap = make_snapshot(uuid4())
        model = PydanticFinalModel(
            FunctionModel(_responder(build_canonical_output(snap).model_dump_json())),
            system_prompt=FINAL_SYSTEM_PROMPT,
        )
        writer = RecordingCommitWriter()
        result = await FinalSynthesisService(model, writer).finalize(make_materials(snap))

        self.assertEqual(result.outcome, "FINAL_COMMITTED")
        self.assertEqual(result.answer_status, FinalOutcomeKindV1.COMPLETE)
        self.assertEqual(result.result_version, 1)
        self.assertEqual(result.commit_disposition, TaskResultCommitDisposition.INSERTED)
        self.assertIsNotNone(result.result_commit_digest)
        self.assertRegex(result.result_commit_digest, r"^[0-9a-f]{64}$")
        self.assertEqual(len(writer.commits), 1)
        self.assertEqual(writer.commits[0].citation_count, 1)

    async def test_commit_digest_deterministic_for_same_snapshot(self) -> None:
        # 同一快照 + 同一提示契约输出 + 同一 materials（generated_at 由调用方固定）→ 同一
        # 内容寻址 Commit Digest，供重放稳定。
        snap = make_snapshot(uuid4())
        materials = make_materials(snap)
        payload = build_canonical_output(snap).model_dump_json()

        first = await FinalSynthesisService(
            PydanticFinalModel(FunctionModel(_responder(payload)), FINAL_SYSTEM_PROMPT),
            RecordingCommitWriter(),
        ).finalize(materials)
        second = await FinalSynthesisService(
            PydanticFinalModel(FunctionModel(_responder(payload)), FINAL_SYSTEM_PROMPT),
            RecordingCommitWriter(),
        ).finalize(materials)

        self.assertEqual(first.result_commit_digest, second.result_commit_digest)


if __name__ == "__main__":
    unittest.main()
