"""DEV-08 子批次 7：Context Pack 不可变契约与确定性装配编排的离线盲测（零 LLM/零 I/O）。

覆盖 §4.3/§11.3：`ContextPackStateV1` 枚举封闭、`assemble_context_pack` 装配出 READY 不可变 Pack、
`pack_hash`（Manifest+预算字段内容寻址）随 Item 变化、`input_digest`（输入身份内容寻址）随身份/版本/
Token 计数材料变化且与候选无关、强制项超限/预留超窗失败关闭、弹性项超限裁剪不失败、naive created_at
与 extra 字段失败关闭、Token 计数模式切换与 Tool Schema 顺序不影响语义。全部同步纯函数，零 LLM/
零 I/O。
"""

from __future__ import annotations

import hashlib
import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError

from knowledge_system.modules.memory.public import (
    ContextAssemblyInputV1,
    ContextItemManifestV1,
    ContextItemTypeV1,
    ContextOperationProfileV1,
    ContextPackStateV1,
    ContextPackV1,
    MemoryVersionSnapshotV1,
    ToolSchemaVersionV1,
    assemble_context_pack,
    compute_input_digest,
    compute_pack_hash,
)
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    EstimatedDevTokenAccountingV1,
    ExactTokenAccountingV1,
)

_NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
_PACK_ID = uuid4()
_TASK_ID = uuid4()
_CREATED_BY = uuid4()
_ACTOR = uuid4()
_BODY = uuid4()


def _exact_accounting(**overrides: object) -> ExactTokenAccountingV1:
    defaults: dict[str, object] = {
        "mode": "EXACT",
        "estimated": False,
        "exact_input_tokens": 1000,
        "tokenizer_revision": "tok-v1",
        "chat_template_hash": "a" * 64,
    }
    defaults.update(overrides)
    return ExactTokenAccountingV1(**defaults)


def _estimated_accounting(**overrides: object) -> EstimatedDevTokenAccountingV1:
    defaults: dict[str, object] = {
        "mode": "ESTIMATED_DEV",
        "estimated": True,
        "estimated_input_tokens": 1000,
        "estimator_revision": "est-v1",
        "serialized_request_bytes": 4096,
        "protocol_overhead_tokens": 512,
    }
    defaults.update(overrides)
    return EstimatedDevTokenAccountingV1(**defaults)


_NAMESPACE = UUID(int=0)


def _uuid(label: str) -> UUID:
    return uuid5(_NAMESPACE, label)


def _item(
    item_id: str, item_type: ContextItemTypeV1, **overrides: object
) -> ContextItemManifestV1:
    defaults: dict[str, object] = {
        "content_hash": hashlib.sha256(item_id.encode("utf-8")).hexdigest(),
        "token_count": 10,
        "priority": 0,
        "rank_score": None,
        "compression_level": 0,
        "selection_reason": "test",
        "trust": "SYSTEM",
    }
    defaults.update(overrides)
    return ContextItemManifestV1(item_id=_uuid(item_id), item_type=item_type, **defaults)


def _request(**overrides: object) -> ContextAssemblyInputV1:
    defaults: dict[str, object] = {
        "id": _PACK_ID,
        "task_id": _TASK_ID,
        "action_attempt_id": None,
        "created_by_attempt_id": _CREATED_BY,
        "conversation_id": None,
        "actor_id": _ACTOR,
        "authorization_ref_id": None,
        "authorization_version": None,
        "policy_version": "ctx-policy-v1",
        "memory_versions": MemoryVersionSnapshotV1(),
        "runtime_version": "rt-v1",
        "plan_version": None,
        "agent_manifest_version": "agent-v1",
        "model_revision": "model-v1",
        "prompt_version": "prompt-v1",
        "tool_schema_versions": (),
        "output_schema_id": None,
        "output_schema_version": None,
        "token_accounting": _exact_accounting(),
        "body_artifact_id": _BODY,
        "created_at": _NOW,
        "max_context_tokens": 32768,
        "reserved_output_tokens": 2048,
        "protocol_overhead_tokens": 512,
        "safety_margin_tokens": 128,
        "operation_profile": ContextOperationProfileV1.KNOWLEDGE_RESEARCH,
        "mandatory_items": (
            _item("policy", ContextItemTypeV1.SYSTEM_POLICY, token_count=1000),
        ),
        "elastic_candidates": (
            _item("mem1", ContextItemTypeV1.MEMORY, token_count=100),
        ),
    }
    defaults.update(overrides)
    return ContextAssemblyInputV1(**defaults)


class ContextPackStateTests(unittest.TestCase):
    def test_state_values_closed(self) -> None:
        self.assertEqual(
            {state.value for state in ContextPackStateV1},
            {"ASSEMBLING", "READY", "CONSUMED", "FAILED", "INVALIDATED_BEFORE_USE"},
        )


class AssembleContextPackTests(unittest.TestCase):
    def test_assemble_produces_ready_pack(self) -> None:
        pack = assemble_context_pack(_request())
        self.assertEqual(pack.state, ContextPackStateV1.READY)
        self.assertEqual(pack.id, _PACK_ID)
        self.assertEqual(pack.task_id, _TASK_ID)
        self.assertEqual(pack.input_tokens, 1100)
        self.assertEqual(
            [item.item_id for item in pack.items], [_uuid("policy"), _uuid("mem1")]
        )

    def test_deterministic_same_input(self) -> None:
        first = assemble_context_pack(_request())
        second = assemble_context_pack(_request())
        self.assertEqual(first.pack_hash, second.pack_hash)
        self.assertEqual(first.input_digest, second.input_digest)

    def test_pack_hash_changes_when_items_change(self) -> None:
        base = assemble_context_pack(_request())
        changed = assemble_context_pack(
            _request(
                elastic_candidates=(
                    _item("mem1", ContextItemTypeV1.MEMORY, content_hash="c" * 64),
                ),
            )
        )
        self.assertNotEqual(base.pack_hash, changed.pack_hash)

    def test_input_digest_changes_when_identity_changes(self) -> None:
        base = assemble_context_pack(_request())
        changed = assemble_context_pack(_request(model_revision="model-v2"))
        self.assertNotEqual(base.input_digest, changed.input_digest)

    def test_input_digest_changes_when_memory_versions_change(self) -> None:
        base = assemble_context_pack(_request())
        changed = assemble_context_pack(
            _request(memory_versions=MemoryVersionSnapshotV1(conversation_version=1))
        )
        self.assertNotEqual(base.input_digest, changed.input_digest)

    def test_input_digest_ignores_elastic_candidates(self) -> None:
        base = assemble_context_pack(_request())
        changed = assemble_context_pack(
            _request(
                elastic_candidates=(
                    _item("mem1", ContextItemTypeV1.MEMORY, content_hash="d" * 64),
                ),
            )
        )
        self.assertEqual(base.input_digest, changed.input_digest)
        self.assertNotEqual(base.pack_hash, changed.pack_hash)

    def test_mandatory_exceeded_fails_closed(self) -> None:
        request = _request(
            max_context_tokens=1000,
            reserved_output_tokens=100,
            protocol_overhead_tokens=0,
            safety_margin_tokens=0,
            mandatory_items=(
                _item("policy", ContextItemTypeV1.SYSTEM_POLICY, token_count=1000),
            ),
        )
        with self.assertRaises(ValueError) as ctx:
            assemble_context_pack(request)
        self.assertEqual(str(ctx.exception), "MODEL_CONTEXT_MANDATORY_EXCEEDED")

    def test_reservations_exceeding_window_fails_closed(self) -> None:
        request = _request(
            max_context_tokens=100,
            reserved_output_tokens=50,
            protocol_overhead_tokens=50,
            safety_margin_tokens=50,
            mandatory_items=(),
        )
        with self.assertRaises(ValueError) as ctx:
            assemble_context_pack(request)
        self.assertEqual(str(ctx.exception), "MODEL_CONTEXT_BUDGET_INVALID")

    def test_elastic_overflow_drops_items_not_failed(self) -> None:
        pack = assemble_context_pack(
            _request(
                elastic_candidates=(
                    _item("big", ContextItemTypeV1.EVIDENCE, token_count=100000),
                ),
            )
        )
        self.assertEqual(pack.state, ContextPackStateV1.READY)
        self.assertEqual([item.item_id for item in pack.items], [_uuid("policy")])
        self.assertEqual(pack.input_tokens, 1000)

    def test_token_accounting_mode_changes_input_digest(self) -> None:
        exact = assemble_context_pack(_request(token_accounting=_exact_accounting()))
        estimated = assemble_context_pack(_request(token_accounting=_estimated_accounting()))
        self.assertNotEqual(exact.input_digest, estimated.input_digest)

    def test_tool_schema_order_independent_digest(self) -> None:
        a = ToolSchemaVersionV1(name="tools.search", version="v1")
        b = ToolSchemaVersionV1(name="tools.answer", version="v2")
        first = assemble_context_pack(_request(tool_schema_versions=(a, b)))
        second = assemble_context_pack(_request(tool_schema_versions=(b, a)))
        self.assertEqual(first.input_digest, second.input_digest)


class ContextPackContractTests(unittest.TestCase):
    def test_input_rejects_naive_created_at(self) -> None:
        with self.assertRaises(ValidationError):
            _request(created_at=datetime(2026, 10, 15, 12, 0, 0))

    def test_input_rejects_extra_field(self) -> None:
        with self.assertRaises(ValidationError):
            ContextAssemblyInputV1(**_request().model_dump(), bogus="x")

    def test_pack_rejects_extra_field(self) -> None:
        pack = assemble_context_pack(_request())
        with self.assertRaises(ValidationError):
            ContextPackV1(**pack.model_dump(), bogus="x")

    def test_pack_hash_covers_budget_fields(self) -> None:
        base = compute_pack_hash(
            items=_request().mandatory_items,
            max_context_tokens=32768,
            reserved_output_tokens=2048,
            protocol_overhead_tokens=512,
            safety_margin_tokens=128,
            input_tokens=1000,
        )
        changed = compute_pack_hash(
            items=_request().mandatory_items,
            max_context_tokens=32768,
            reserved_output_tokens=4096,
            protocol_overhead_tokens=512,
            safety_margin_tokens=128,
            input_tokens=1000,
        )
        self.assertNotEqual(base, changed)

    def test_input_digest_deterministic(self) -> None:
        self.assertEqual(compute_input_digest(_request()), compute_input_digest(_request()))


if __name__ == "__main__":
    unittest.main()
