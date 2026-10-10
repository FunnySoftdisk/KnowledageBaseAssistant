"""DEV-06 OTel/Metric 观测离线契约：Final 链指标注册表与标签校验。

不依赖 OTel SDK/Collector/Prometheus，验证日志指标告警字典 §5 与 §6.2 的 25 项
任务终态事务 + Final 综合决策指标：
- 版本化注册表冻结名称/类型/允许标签键，命名约定（`knowledge_system_` 前缀、Counter `_total`
  后缀、`_seconds` 只用于 Gauge/Histogram）自审计；
- `metric_definition` 未注册指标失败关闭；
- `validate_metric_attributes` 拒绝未注册指标、越界标签与高基数/Secret 标签。
"""

from __future__ import annotations

import unittest

from knowledge_system.modules.tasking.domain.metric_contract import (
    FINAL_CHAIN_METRIC_CONTRACT_V1,
    MetricContractViolation,
    MetricKindV1,
    is_forbidden_label_name,
    metric_definition,
    validate_metric_attributes,
)


class MetricContractRegistryTest(unittest.TestCase):
    def test_registry_has_exactly_twenty_five_final_chain_metrics(self) -> None:
        self.assertEqual(len(FINAL_CHAIN_METRIC_CONTRACT_V1), 25)

    def test_every_metric_follows_naming_convention(self) -> None:
        for definition in FINAL_CHAIN_METRIC_CONTRACT_V1.values():
            with self.subTest(name=definition.name):
                self.assertTrue(definition.name.startswith("knowledge_system_"))
                if definition.kind is MetricKindV1.COUNTER:
                    self.assertTrue(definition.name.endswith("_total"))
                else:
                    self.assertFalse(definition.name.endswith("_total"))
                if definition.name.endswith("_seconds"):
                    self.assertIsNot(definition.kind, MetricKindV1.COUNTER)

    def test_no_definition_uses_forbidden_label(self) -> None:
        for definition in FINAL_CHAIN_METRIC_CONTRACT_V1.values():
            for label in definition.labels:
                with self.subTest(name=definition.name, label=label):
                    self.assertFalse(is_forbidden_label_name(label))


class MetricDefinitionLookupTest(unittest.TestCase):
    def test_lookup_known_metrics(self) -> None:
        definition = metric_definition("knowledge_system_tasks_total")
        self.assertIs(definition.kind, MetricKindV1.COUNTER)
        self.assertEqual(definition.labels, frozenset({"scenario_label", "terminal_status"}))

    def test_gauge_metric_kind_and_labels(self) -> None:
        definition = metric_definition("knowledge_system_task_result_staging_oldest_seconds")
        self.assertIs(definition.kind, MetricKindV1.GAUGE)
        self.assertEqual(definition.labels, frozenset({"environment"}))

    def test_histogram_token_metric(self) -> None:
        definition = metric_definition("knowledge_system_final_synthesis_input_tokens")
        self.assertIs(definition.kind, MetricKindV1.HISTOGRAM)
        self.assertEqual(definition.labels, frozenset({"request_kind", "token_bucket"}))

    def test_undefined_metric_fails_closed(self) -> None:
        with self.assertRaises(MetricContractViolation) as ctx:
            metric_definition("knowledge_system_not_a_real_metric_total")
        self.assertEqual(ctx.exception.code, "METRIC_UNDEFINED")

    def test_missing_total_suffix_rejected(self) -> None:
        with self.assertRaises(MetricContractViolation) as ctx:
            metric_definition("knowledge_system_task_result_commits")
        self.assertEqual(ctx.exception.code, "METRIC_UNDEFINED")


class ValidateMetricAttributesTest(unittest.TestCase):
    def test_valid_subset_of_labels_passes(self) -> None:
        validate_metric_attributes(
            "knowledge_system_final_synthesis_model_calls_total",
            {"request_kind": "final_synthesis", "provider_alias": "aliyun", "outcome": "success"},
        )

    def test_all_declared_labels_passes(self) -> None:
        definition = metric_definition("knowledge_system_task_result_commits_total")
        validate_metric_attributes(definition.name, {label: "value" for label in definition.labels})

    def test_unknown_metric_fails_closed(self) -> None:
        with self.assertRaises(MetricContractViolation) as ctx:
            validate_metric_attributes("knowledge_system_unknown_total", {})
        self.assertEqual(ctx.exception.code, "METRIC_UNDEFINED")

    def test_label_outside_allowlist_rejected(self) -> None:
        with self.assertRaises(MetricContractViolation) as ctx:
            validate_metric_attributes(
                "knowledge_system_tasks_total",
                {"scenario_label": "a", "model_alias": "aliyun"},
            )
        self.assertEqual(ctx.exception.code, "METRIC_LABEL_NOT_ALLOWED")

    def test_high_cardinality_label_rejected(self) -> None:
        for forbidden in ("task_id", "user_id", "trace_id", "url", "error_text"):
            with self.subTest(label=forbidden):
                with self.assertRaises(MetricContractViolation) as ctx:
                    validate_metric_attributes(
                        "knowledge_system_final_synthesis_security_rejections_total",
                        {forbidden: "x", "reason": "y"},
                    )
                self.assertEqual(ctx.exception.code, "METRIC_LABEL_FORBIDDEN")

    def test_secret_label_rejected(self) -> None:
        for forbidden in ("api_key", "authorization", "secret", "access_token"):
            with self.subTest(label=forbidden):
                with self.assertRaises(MetricContractViolation) as ctx:
                    validate_metric_attributes(
                        "knowledge_system_final_synthesis_security_rejections_total",
                        {"reason": "y", forbidden: "z"},
                    )
                self.assertEqual(ctx.exception.code, "METRIC_LABEL_FORBIDDEN")

    def test_forbidden_label_detection_is_case_insensitive(self) -> None:
        self.assertTrue(is_forbidden_label_name("API_KEY"))
        self.assertTrue(is_forbidden_label_name("  Task_Id  "))
        self.assertFalse(is_forbidden_label_name("model_alias"))

    def test_empty_attributes_pass_for_defined_metric(self) -> None:
        validate_metric_attributes("knowledge_system_tasks_total", {})


if __name__ == "__main__":
    unittest.main()
