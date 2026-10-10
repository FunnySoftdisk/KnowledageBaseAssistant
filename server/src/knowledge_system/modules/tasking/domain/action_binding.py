"""AGT-06 ActionBinder：严格 Schema 校验 + RFC 6901 投影 + 冻结 Normalizer + 三类 Digest。

`bind_action` 是零 LLM、零 I/O 的确定性绑定器：
1. 以冻结 `CapabilityBindingSnapshotV1` 精确反解 `model_tool_name` 得到
   `tool_id/catalog_version/capability_ref/input_schema_digest/output_schema_digest`；
2. 用 `jsonschema.Draft202012Validator` 严格校验参数，不做类型强转、不应用 default；
3. 按 `CapabilityActionProfileV1.projection_rules` 的 RFC 6901 指针投影并规范化；
4. 用 RFC 8785 JCS 计算 `arguments_digest/semantic_action_digest/
   execution_request_digest/effect_fingerprint`。

离线切片仅支持 `source_kind == "ARGUMENT"`（从已校验参数投影）；Plan/Task/Context/Profile
常量来源需在线解析，离线一律失败关闭。模型声明任何权威字段一律无效。
"""

from __future__ import annotations

from typing import Any, Literal, cast
from uuid import UUID

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
from pydantic import Field, JsonValue, model_validator

from .action_candidate import ProposedActionV1
from .action_facts import (
    NORMALIZER_IDS,
    CanonicalActionFactsV1,
    CanonicalNetworkTargetV1,
    CanonicalRecipientV1,
    CanonicalResourceRefV1,
    DataFlowFactV1,
    normalize,
)
from .core_types import (
    ArtifactRefV1,
    CapabilityRefV1,
    Sha256Hex,
    StableKey,
    StrictContractV1,
    VersionedRefV1,
    canonical_json_sha256,
)

_TARGET_FACT_NORMALIZERS: dict[str, frozenset[str]] = {
    "RESOURCE": frozenset({"resource_ref_v1"}),
    "NETWORK_TARGET": frozenset({"network_target_v1"}),
    "RECIPIENT": frozenset({"recipient_set_v1"}),
    "DATA_INPUT": frozenset({"resource_ref_v1", "absolute_uri_v1", "opaque_string_v1"}),
    "DATA_EGRESS": frozenset({"resource_ref_v1", "absolute_uri_v1", "opaque_string_v1"}),
    "EFFECT_TARGET": frozenset({"network_target_v1", "absolute_uri_v1", "opaque_string_v1"}),
    "COST_DRIVER": frozenset({"byte_count_v1", "opaque_string_v1"}),
}


class ActionProjectionRuleV1(StrictContractV1):
    source_kind: Literal[
        "ARGUMENT", "PLAN_ITEM", "TASK_RESOURCE", "CONTEXT_DATA_LABEL", "PROFILE_CONSTANT"
    ]
    source_pointer: str | None = None
    target_fact: Literal[
        "RESOURCE",
        "NETWORK_TARGET",
        "RECIPIENT",
        "DATA_INPUT",
        "DATA_EGRESS",
        "EFFECT_TARGET",
        "COST_DRIVER",
    ]
    normalizer_id: StableKey
    required: bool

    @model_validator(mode="after")
    def validate_rule(self) -> ActionProjectionRuleV1:
        if self.normalizer_id not in NORMALIZER_IDS:
            raise ValueError("NORMALIZER_NOT_REGISTERED")
        if self.source_kind == "ARGUMENT" and self.source_pointer is None:
            raise ValueError("ARGUMENT_RULE_REQUIRES_POINTER")
        if (
            self.source_pointer is not None
            and not self.source_pointer.startswith("/")
            and (self.source_pointer != "")
        ):
            raise ValueError("PROJECTION_POINTER_INVALID")
        return self


class CapabilityActionProfileV1(StrictContractV1):
    contract_version: Literal["capability_action_profile_v1"]
    profile_id: UUID
    version: int = Field(ge=1)
    capability_ref: CapabilityRefV1
    tool_id: UUID | None = None
    input_schema_digest: Sha256Hex
    action_family: str = Field(min_length=1, max_length=128)
    risk_floor: Literal["R0", "R1", "R2", "R3"]
    effect_class: Literal["READ_ONLY", "WRITE_REVERSIBLE", "WRITE_IRREVERSIBLE", "UNKNOWN"]
    idempotency_mode: Literal["READ", "PROVIDER_KEY", "NONE", "UNKNOWN"]
    projection_rules: tuple[ActionProjectionRuleV1, ...] = Field(max_length=64)
    semantic_argument_pointers: tuple[str, ...] = Field(max_length=32)
    effect_argument_pointers: tuple[str, ...] = Field(max_length=32)
    normalizer_profile: str = Field(min_length=1, max_length=64)


class CapabilityToolBindingV1(StrictContractV1):
    capability_ref: CapabilityRefV1
    tool_id: UUID
    catalog_version: StableKey
    model_tool_name: StableKey
    input_schema_digest: Sha256Hex
    output_schema_digest: Sha256Hex
    tool_policy_profile_ref: VersionedRefV1
    disclosure: Literal["EAGER", "DEFERRED"]
    model_status: Literal["COMPATIBLE", "MODEL_INCOMPATIBLE"]


class CapabilityBindingSnapshotV1(StrictContractV1):
    """capability_toolset_binding_v1 冻结快照；`agent_manifest_ref/parent_grant_refs/
    tool_policy_profile_ref` 以 VersionedRefV1（身份/版本/摘要）承载，不授予权限。"""

    contract_version: Literal["capability_toolset_binding_v1"]
    snapshot_id: UUID
    task_id: UUID
    task_attempt_id: UUID
    agent_manifest_ref: VersionedRefV1
    plan_version: int = Field(ge=1)
    parent_grant_refs: tuple[VersionedRefV1, ...] = Field(max_length=32)
    model_capability: StableKey
    provider_capabilities_digest: Sha256Hex
    bindings: tuple[CapabilityToolBindingV1, ...] = Field(max_length=16)
    eager_max: Literal[8] = 8
    search_max_results: Literal[5] = 5
    search_max_calls_per_attempt: Literal[4] = 4
    revealed_max_per_attempt: Literal[16] = 16
    single_definition_max_bytes: Literal[32768] = 32768
    step_definitions_max_bytes: Literal[131072] = 131072
    integrity_digest: Sha256Hex


class ActionBindingV1(StrictContractV1):
    contract_version: Literal["action_binding_v1"]
    proposal_ref: ArtifactRefV1
    exact_capability_ref: CapabilityRefV1
    tool_id: UUID | None = None
    catalog_version: StableKey | None = None
    input_schema_digest: Sha256Hex
    output_schema_digest: Sha256Hex
    action_profile_ref: VersionedRefV1
    canonical_facts: CanonicalActionFactsV1
    arguments_digest: Sha256Hex
    semantic_action_digest: Sha256Hex
    execution_request_digest: Sha256Hex
    effect_fingerprint: Sha256Hex | None = None


def project_pointer(document: Any, pointer: str) -> Any:
    """RFC 6901 JSON Pointer 投影；`""` 取整文档，`~0`/`~1` 转义 `~`/`/`，数组索引无前导零。"""

    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError("PROJECTION_POINTER_INVALID")
    current: Any = document
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise ValueError("PROJECTION_POINTER_MISSING")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise ValueError("PROJECTION_POINTER_INVALID")
            index = int(token)
            if index >= len(current):
                raise ValueError("PROJECTION_POINTER_MISSING")
            current = current[index]
        else:
            raise ValueError("PROJECTION_POINTER_INVALID")
    return current


def validate_arguments_against_schema(input_schema: dict[str, Any], arguments: Any) -> None:
    """Draft 2020-12 严格校验：只报告错误，不强转类型、不应用 default、不改写输入。"""

    try:
        validator = Draft202012Validator(input_schema)
    except SchemaError as exc:
        raise ValueError("INPUT_SCHEMA_INVALID") from exc
    if list(validator.iter_errors(arguments)):
        raise ValueError("ARGUMENT_SCHEMA_INVALID")


def capability_profile_digest(profile: CapabilityActionProfileV1) -> Sha256Hex:
    return canonical_json_sha256(profile.model_dump(mode="json"))


def arguments_digest_for(arguments: dict[str, Any]) -> Sha256Hex:
    return canonical_json_sha256(arguments)


def _dump_many(models: tuple[StrictContractV1, ...]) -> list[dict[str, Any]]:
    return [model.model_dump(mode="json") for model in models]


def _project_fields(pointers: tuple[str, ...], arguments: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for pointer in pointers:
        fields[pointer] = project_pointer(arguments, pointer)
    return fields


def semantic_action_digest_for(
    profile: CapabilityActionProfileV1,
    facts: CanonicalActionFactsV1,
    arguments: dict[str, Any],
) -> Sha256Hex:
    """跨 Tool 阻断同义绕过；不含 Tool/Capability/Catalog 名称与 Grant/Policy/重试/Secret。"""

    payload = {
        "action_family": facts.action_family,
        "purpose_code": facts.purpose_code,
        "effect_class": facts.effect_class,
        "resources": _dump_many(facts.resources),
        "network_targets": _dump_many(facts.network_targets),
        "recipients": _dump_many(facts.recipients),
        "data_inputs": _dump_many(facts.data_inputs),
        "data_egress": _dump_many(facts.data_egress),
        "semantic_fields": _project_fields(profile.semantic_argument_pointers, arguments),
    }
    return canonical_json_sha256(payload)


def execution_request_digest_for(
    binding: CapabilityToolBindingV1,
    profile: CapabilityActionProfileV1,
    facts: CanonicalActionFactsV1,
    arguments_digest: Sha256Hex,
) -> Sha256Hex:
    """覆盖准确能力版本、完整规范参数、资源/网络/数据流及运行绑定。"""

    payload = {
        "capability_ref": binding.capability_ref.model_dump(mode="json"),
        "tool_id": str(binding.tool_id),
        "catalog_version": binding.catalog_version,
        "input_schema_digest": binding.input_schema_digest,
        "output_schema_digest": binding.output_schema_digest,
        "action_profile_ref": {
            "ref_id": str(profile.profile_id),
            "version": profile.version,
            "integrity_digest": capability_profile_digest(profile),
        },
        "arguments_digest": arguments_digest,
        "canonical_facts": facts.model_dump(mode="json"),
    }
    return canonical_json_sha256(payload)


def effect_fingerprint_for(
    profile: CapabilityActionProfileV1,
    facts: CanonicalActionFactsV1,
    arguments: dict[str, Any],
    task_id: UUID,
    sequence_no: int,
) -> Sha256Hex:
    """防止换 Provider 重复产生同一副作用；排除 Tool/Capability/Grant/Policy/重试/Secret。"""

    payload = {
        "task_id": str(task_id),
        "action_family": facts.action_family,
        "effect_class": facts.effect_class,
        "resources": _dump_many(facts.resources),
        "network_targets": _dump_many(facts.network_targets),
        "recipients": _dump_many(facts.recipients),
        "data_egress": _dump_many(facts.data_egress),
        "effect_fields": _project_fields(profile.effect_argument_pointers, arguments),
        "sequence_no": sequence_no,
    }
    return canonical_json_sha256(payload)


def _resolve_binding(
    proposal: ProposedActionV1, snapshot: CapabilityBindingSnapshotV1
) -> CapabilityToolBindingV1:
    if proposal.binding_snapshot_id != snapshot.snapshot_id:
        raise ValueError("BINDING_SNAPSHOT_ID_MISMATCH")
    if proposal.binding_snapshot_digest != snapshot.integrity_digest:
        raise ValueError("BINDING_SNAPSHOT_DIGEST_MISMATCH")
    for binding in snapshot.bindings:
        if binding.model_tool_name == proposal.model_tool_name:
            return binding
    raise ValueError("TOOL_NOT_DISCLOSED_IN_SNAPSHOT")


def _resolve_arguments(proposal: ProposedActionV1) -> dict[str, Any]:
    payload = proposal.arguments_payload
    if payload.payload_kind != "INLINE_JSON" or payload.inline_json is None:
        raise ValueError("ARGUMENT_PAYLOAD_REQUIRES_RESOLUTION")
    arguments = payload.inline_json
    if not isinstance(arguments, dict):
        raise ValueError("ARGUMENTS_MUST_BE_OBJECT")
    return cast(dict[str, Any], arguments)


def _verify_profile(profile: CapabilityActionProfileV1, binding: CapabilityToolBindingV1) -> None:
    if profile.capability_ref != binding.capability_ref:
        raise ValueError("PROFILE_CAPABILITY_MISMATCH")
    if profile.tool_id is not None and profile.tool_id != binding.tool_id:
        raise ValueError("PROFILE_TOOL_MISMATCH")
    if profile.input_schema_digest != binding.input_schema_digest:
        raise ValueError("PROFILE_INPUT_SCHEMA_DIGEST_MISMATCH")


def _to_json(value: Any) -> JsonValue:
    if isinstance(value, StrictContractV1):
        return cast(JsonValue, value.model_dump(mode="json"))
    if isinstance(value, tuple):
        return [_to_json(item) for item in value]
    if isinstance(value, list):
        return [_to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_json(item) for key, item in value.items()}
    return cast(JsonValue, value)


def _build_data_flow_fact(normalizer_id: str, normalized: Any) -> DataFlowFactV1:
    if normalizer_id == "resource_ref_v1":
        if not isinstance(normalized, CanonicalResourceRefV1):
            raise ValueError("TARGET_FACT_NORMALIZER_MISMATCH")
        return DataFlowFactV1(
            payload_kind="ARTIFACT",
            identifier=normalized.identifier,
            integrity_digest=normalized.integrity_digest,
        )
    if normalizer_id == "absolute_uri_v1":
        return DataFlowFactV1(payload_kind="ABSOLUTE_URI", identifier=normalized)
    if normalizer_id == "opaque_string_v1":
        return DataFlowFactV1(payload_kind="OPAQUE", identifier=normalized)
    raise ValueError("TARGET_FACT_NORMALIZER_MISMATCH")


def _route_fact(
    target_fact: str,
    normalizer_id: str,
    normalized: Any,
    resources: list[CanonicalResourceRefV1],
    network_targets: list[CanonicalNetworkTargetV1],
    recipients: list[CanonicalRecipientV1],
    data_inputs: list[DataFlowFactV1],
    data_egress: list[DataFlowFactV1],
) -> None:
    allowed = _TARGET_FACT_NORMALIZERS.get(target_fact)
    if allowed is None or normalizer_id not in allowed:
        raise ValueError("TARGET_FACT_NORMALIZER_MISMATCH")

    if target_fact == "RESOURCE":
        if not isinstance(normalized, CanonicalResourceRefV1):
            raise ValueError("TARGET_FACT_NORMALIZER_MISMATCH")
        resources.append(normalized)
    elif target_fact == "NETWORK_TARGET":
        if not isinstance(normalized, CanonicalNetworkTargetV1):
            raise ValueError("TARGET_FACT_NORMALIZER_MISMATCH")
        network_targets.append(normalized)
    elif target_fact == "RECIPIENT":
        if not isinstance(normalized, tuple):
            raise ValueError("TARGET_FACT_NORMALIZER_MISMATCH")
        recipients.extend(normalized)
    elif target_fact in ("DATA_INPUT", "DATA_EGRESS"):
        fact = _build_data_flow_fact(normalizer_id, normalized)
        (data_inputs if target_fact == "DATA_INPUT" else data_egress).append(fact)


def _assemble_facts(
    profile: CapabilityActionProfileV1,
    arguments: dict[str, Any],
    purpose_code: str,
) -> CanonicalActionFactsV1:
    resources: list[CanonicalResourceRefV1] = []
    network_targets: list[CanonicalNetworkTargetV1] = []
    recipients: list[CanonicalRecipientV1] = []
    data_inputs: list[DataFlowFactV1] = []
    data_egress: list[DataFlowFactV1] = []
    normalized_arguments: dict[str, JsonValue] = {}

    for rule in profile.projection_rules:
        if rule.source_kind != "ARGUMENT":
            raise ValueError("SOURCE_KIND_UNSUPPORTED_OFFLINE")
        try:
            raw = project_pointer(arguments, rule.source_pointer or "")
        except ValueError:
            if rule.required:
                raise
            continue
        normalized = normalize(rule.normalizer_id, raw)
        _route_fact(
            rule.target_fact,
            rule.normalizer_id,
            normalized,
            resources,
            network_targets,
            recipients,
            data_inputs,
            data_egress,
        )
        normalized_arguments[rule.source_pointer or ""] = _to_json(normalized)

    return CanonicalActionFactsV1(
        action_family=profile.action_family,
        resources=tuple(resources),
        network_targets=tuple(network_targets),
        recipients=tuple(recipients),
        data_inputs=tuple(data_inputs),
        data_egress=tuple(data_egress),
        effect_class=profile.effect_class,
        idempotency_mode=profile.idempotency_mode,
        risk_floor=profile.risk_floor,
        normalized_arguments=normalized_arguments,
        purpose_code=purpose_code,
    )


def bind_action(
    *,
    proposal: ProposedActionV1,
    snapshot: CapabilityBindingSnapshotV1,
    profile: CapabilityActionProfileV1,
    input_schema: dict[str, Any],
    proposal_ref: ArtifactRefV1,
    purpose_code: str,
    sequence_no: int,
) -> ActionBindingV1:
    """确定性绑定：严格校验 -> 反解名称 -> 投影规范化 -> 计算三类 Digest。"""

    if sequence_no < 1:
        raise ValueError("SEQUENCE_NO_INVALID")

    binding = _resolve_binding(proposal, snapshot)
    arguments = _resolve_arguments(proposal)
    validate_arguments_against_schema(input_schema, arguments)
    _verify_profile(profile, binding)

    facts = _assemble_facts(profile, arguments, purpose_code)

    args_digest = arguments_digest_for(arguments)
    profile_ref = VersionedRefV1(
        ref_id=profile.profile_id,
        version=profile.version,
        integrity_digest=capability_profile_digest(profile),
    )
    return ActionBindingV1(
        contract_version="action_binding_v1",
        proposal_ref=proposal_ref,
        exact_capability_ref=binding.capability_ref,
        tool_id=binding.tool_id,
        catalog_version=binding.catalog_version,
        input_schema_digest=binding.input_schema_digest,
        output_schema_digest=binding.output_schema_digest,
        action_profile_ref=profile_ref,
        canonical_facts=facts,
        arguments_digest=args_digest,
        semantic_action_digest=semantic_action_digest_for(profile, facts, arguments),
        execution_request_digest=execution_request_digest_for(binding, profile, facts, args_digest),
        effect_fingerprint=effect_fingerprint_for(
            profile, facts, arguments, proposal.task_id, sequence_no
        ),
    )
