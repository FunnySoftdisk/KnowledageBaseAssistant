"""DEV-08 子批次 4：Context Pack 领域契约 + Token 预算与确定性裁剪规则（离线）。

覆盖 §4.3/§4.4/§11.2/§11.3/§11.4：枚举封闭性、预算算术（available_input/elastic_tokens/
bucket_limits、强制项超限失败关闭、预留超窗、负数、专用画像拒绝）、弹性比例分配（floor、
FINAL_SYNTHESIS 零记忆、专用画像）、强制项/桶映射、P0 精确去重、优先级/相关度填桶、超限裁剪
与原子项过大失败关闭。全部为同步纯函数，零 LLM/零 I/O。
"""

from __future__ import annotations

import hashlib
import unittest
from uuid import UUID, uuid5

from pydantic import ValidationError

from knowledge_system.modules.memory.public import (
    ContextBucketV1,
    ContextBudgetV1,
    ContextItemManifestV1,
    ContextItemTypeV1,
    ContextOperationProfileV1,
    allocate_elastic_budget,
    bucket_for_item_type,
    compute_context_budget,
    is_mandatory_item_type,
    select_context_items,
)

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


def _bucket(
    *,
    mandatory: int = 100,
    recent: int = 50,
    memory: int = 50,
    task_artifact: int = 50,
    evidence: int = 50,
) -> ContextBudgetV1:
    return ContextBudgetV1(
        max_context_tokens=32768,
        reserved_output_tokens=0,
        protocol_overhead_tokens=0,
        safety_margin_tokens=0,
        mandatory_tokens=mandatory,
        elastic_tokens=recent + memory + task_artifact + evidence,
        bucket_limits={
            ContextBucketV1.RECENT: recent,
            ContextBucketV1.MEMORY: memory,
            ContextBucketV1.TASK_ARTIFACT: task_artifact,
            ContextBucketV1.EVIDENCE: evidence,
            ContextBucketV1.SUMMARY: 0,
        },
    )


class ContextEnumsTests(unittest.TestCase):
    def test_bucket_values_closed(self) -> None:
        self.assertEqual(
            {bucket.value for bucket in ContextBucketV1},
            {"RECENT", "MEMORY", "TASK_ARTIFACT", "EVIDENCE", "SUMMARY"},
        )

    def test_item_type_values_closed(self) -> None:
        self.assertEqual(
            {item.value for item in ContextItemTypeV1},
            {
                "SYSTEM_POLICY",
                "TOOL_SCHEMA",
                "OUTPUT_SCHEMA",
                "CURRENT_MESSAGE",
                "RECENT_MESSAGE",
                "MEMORY",
                "TASK_ARTIFACT",
                "OBSERVATION",
                "EVIDENCE",
                "SUMMARY",
            },
        )

    def test_profile_values_closed(self) -> None:
        self.assertEqual(
            {profile.value for profile in ContextOperationProfileV1},
            {
                "KNOWLEDGE_RESEARCH",
                "MULTI_MATERIAL_ANALYSIS",
                "SYNTHESIS_ACTION",
                "FINAL_SYNTHESIS",
                "FACT_VERIFICATION",
                "EXTERNAL_RESEARCH",
                "GOVERNANCE_EXPLANATION",
                "SUPERVISOR_PLANNING",
            },
        )

    def test_manifest_rejects_extra_field(self) -> None:
        with self.assertRaises(ValidationError):
            ContextItemManifestV1(**_item("a", ContextItemTypeV1.MEMORY).model_dump(), bucket="X")


class ContextBudgetTests(unittest.TestCase):
    def test_budget_arithmetic(self) -> None:
        budget = compute_context_budget(
            max_context_tokens=32768,
            reserved_output_tokens=2048,
            protocol_overhead_tokens=512,
            safety_margin_tokens=128,
            mandatory_tokens=1000,
            profile=ContextOperationProfileV1.KNOWLEDGE_RESEARCH,
        )
        # available_input = 32768-2048-512-128 = 30080; elastic = 30080-1000 = 29080.
        self.assertEqual(budget.elastic_tokens, 29080)
        self.assertEqual(budget.bucket_limits[ContextBucketV1.RECENT], 29080 * 15 // 100)
        self.assertEqual(budget.bucket_limits[ContextBucketV1.MEMORY], 29080 * 10 // 100)
        self.assertEqual(budget.bucket_limits[ContextBucketV1.TASK_ARTIFACT], 29080 * 10 // 100)
        self.assertEqual(budget.bucket_limits[ContextBucketV1.EVIDENCE], 29080 * 65 // 100)
        self.assertEqual(budget.bucket_limits[ContextBucketV1.SUMMARY], 0)

    def test_mandatory_exceeded_fails_closed(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            compute_context_budget(
                max_context_tokens=1000,
                reserved_output_tokens=100,
                protocol_overhead_tokens=0,
                safety_margin_tokens=0,
                mandatory_tokens=1000,
                profile=ContextOperationProfileV1.KNOWLEDGE_RESEARCH,
            )
        self.assertEqual(str(ctx.exception), "MODEL_CONTEXT_MANDATORY_EXCEEDED")

    def test_reservations_exceeding_window_fails_closed(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            compute_context_budget(
                max_context_tokens=100,
                reserved_output_tokens=50,
                protocol_overhead_tokens=50,
                safety_margin_tokens=50,
                mandatory_tokens=0,
                profile=ContextOperationProfileV1.KNOWLEDGE_RESEARCH,
            )
        self.assertEqual(str(ctx.exception), "MODEL_CONTEXT_BUDGET_INVALID")

    def test_negative_field_fails_closed(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            compute_context_budget(
                max_context_tokens=-1,
                reserved_output_tokens=0,
                protocol_overhead_tokens=0,
                safety_margin_tokens=0,
                mandatory_tokens=0,
                profile=ContextOperationProfileV1.KNOWLEDGE_RESEARCH,
            )
        self.assertEqual(str(ctx.exception), "BUDGET_FIELD_NEGATIVE")

    def test_allocate_floor_rounding_is_conservative(self) -> None:
        limits = allocate_elastic_budget(ContextOperationProfileV1.KNOWLEDGE_RESEARCH, 199)
        self.assertEqual(limits[ContextBucketV1.RECENT], 199 * 15 // 100)
        self.assertEqual(limits[ContextBucketV1.EVIDENCE], 199 * 65 // 100)
        self.assertEqual(limits[ContextBucketV1.SUMMARY], 0)

    def test_allocate_final_synthesis_zero_memory(self) -> None:
        limits = allocate_elastic_budget(ContextOperationProfileV1.FINAL_SYNTHESIS, 1000)
        self.assertEqual(limits[ContextBucketV1.RECENT], 0)
        self.assertEqual(limits[ContextBucketV1.MEMORY], 0)
        self.assertEqual(limits[ContextBucketV1.TASK_ARTIFACT], 500)
        self.assertEqual(limits[ContextBucketV1.EVIDENCE], 500)

    def test_allocate_dedicated_profiles_fail_closed(self) -> None:
        for profile in (
            ContextOperationProfileV1.FACT_VERIFICATION,
            ContextOperationProfileV1.EXTERNAL_RESEARCH,
        ):
            with self.subTest(profile=profile):
                with self.assertRaises(ValueError) as ctx:
                    allocate_elastic_budget(profile, 100)
                self.assertEqual(str(ctx.exception), "PROFILE_USES_DEDICATED_CONTEXT_BUDGET")


class ContextItemMappingTests(unittest.TestCase):
    def test_mandatory_item_types(self) -> None:
        for item_type in (
            ContextItemTypeV1.SYSTEM_POLICY,
            ContextItemTypeV1.TOOL_SCHEMA,
            ContextItemTypeV1.OUTPUT_SCHEMA,
            ContextItemTypeV1.CURRENT_MESSAGE,
        ):
            with self.subTest(item_type=item_type):
                self.assertTrue(is_mandatory_item_type(item_type))
                self.assertIsNone(bucket_for_item_type(item_type))

    def test_elastic_bucket_mapping(self) -> None:
        self.assertEqual(
            bucket_for_item_type(ContextItemTypeV1.RECENT_MESSAGE), ContextBucketV1.RECENT
        )
        self.assertEqual(bucket_for_item_type(ContextItemTypeV1.MEMORY), ContextBucketV1.MEMORY)
        self.assertEqual(
            bucket_for_item_type(ContextItemTypeV1.TASK_ARTIFACT), ContextBucketV1.TASK_ARTIFACT
        )
        self.assertEqual(
            bucket_for_item_type(ContextItemTypeV1.OBSERVATION), ContextBucketV1.TASK_ARTIFACT
        )
        self.assertEqual(bucket_for_item_type(ContextItemTypeV1.EVIDENCE), ContextBucketV1.EVIDENCE)
        self.assertEqual(bucket_for_item_type(ContextItemTypeV1.SUMMARY), ContextBucketV1.SUMMARY)


class ContextSelectionTests(unittest.TestCase):
    def test_mandatory_always_selected(self) -> None:
        budget = _bucket()
        policy = _item("policy", ContextItemTypeV1.SYSTEM_POLICY, token_count=80)
        current = _item("current", ContextItemTypeV1.CURRENT_MESSAGE, token_count=20)
        result = select_context_items((policy, current), budget)
        self.assertEqual(
            [item.item_id for item in result.selected], [_uuid("policy"), _uuid("current")]
        )
        self.assertEqual(result.total_tokens, 100)
        self.assertFalse(result.overflow)

    def test_priority_order_within_bucket(self) -> None:
        budget = _bucket(recent=30)
        low = _item("low", ContextItemTypeV1.RECENT_MESSAGE, token_count=10, priority=1)
        high = _item("high", ContextItemTypeV1.RECENT_MESSAGE, token_count=10, priority=9)
        result = select_context_items((low, high), budget)
        self.assertEqual([item.item_id for item in result.selected], [_uuid("high"), _uuid("low")])

    def test_overflow_drops_low_priority(self) -> None:
        budget = _bucket(recent=30)
        keep = _item("keep", ContextItemTypeV1.RECENT_MESSAGE, token_count=10, priority=9)
        drop = _item("drop", ContextItemTypeV1.RECENT_MESSAGE, token_count=25, priority=1)
        result = select_context_items((drop, keep), budget)
        self.assertEqual([item.item_id for item in result.selected], [_uuid("keep")])
        self.assertEqual([item.item_id for item in result.dropped], [_uuid("drop")])
        self.assertTrue(result.overflow)

    def test_exact_dedup_by_content_hash(self) -> None:
        budget = _bucket(recent=100)
        first = _item("a", ContextItemTypeV1.RECENT_MESSAGE, content_hash="c" * 64)
        duplicate = _item("b", ContextItemTypeV1.RECENT_MESSAGE, content_hash="c" * 64)
        result = select_context_items((first, duplicate), budget)
        self.assertEqual(len(result.selected), 1)
        self.assertEqual(result.selected[0].item_id, _uuid("a"))

    def test_rank_score_breaks_priority_ties(self) -> None:
        budget = _bucket(recent=30)
        lower = _item("lower", ContextItemTypeV1.RECENT_MESSAGE, token_count=10, rank_score=0.1)
        higher = _item("higher", ContextItemTypeV1.RECENT_MESSAGE, token_count=10, rank_score=0.9)
        result = select_context_items((lower, higher), budget)
        self.assertEqual(
            [item.item_id for item in result.selected], [_uuid("higher"), _uuid("lower")]
        )

    def test_atomic_mandatory_too_large_fails_closed(self) -> None:
        budget = _bucket(mandatory=100)
        huge = _item("huge", ContextItemTypeV1.SYSTEM_POLICY, token_count=10000)
        result = select_context_items((huge,), budget)
        self.assertEqual(result.selected, ())
        self.assertEqual(result.error_code, "CONTEXT_ATOMIC_ITEM_TOO_LARGE")
        self.assertTrue(result.overflow)

    def test_elastic_exceeding_bucket_is_dropped(self) -> None:
        budget = _bucket(evidence=10)
        too_big = _item("big", ContextItemTypeV1.EVIDENCE, token_count=11)
        result = select_context_items((too_big,), budget)
        self.assertEqual(result.selected, ())
        self.assertEqual(result.dropped[0].item_id, _uuid("big"))
        self.assertTrue(result.overflow)
        self.assertIsNone(result.error_code)

    def test_total_tokens_sum_selected(self) -> None:
        budget = _bucket(mandatory=50, recent=30)
        policy = _item("policy", ContextItemTypeV1.SYSTEM_POLICY, token_count=40)
        recent = _item("r1", ContextItemTypeV1.RECENT_MESSAGE, token_count=10)
        recent2 = _item("r2", ContextItemTypeV1.RECENT_MESSAGE, token_count=20)
        result = select_context_items((policy, recent, recent2), budget)
        self.assertEqual(result.total_tokens, 70)


if __name__ == "__main__":
    unittest.main()
