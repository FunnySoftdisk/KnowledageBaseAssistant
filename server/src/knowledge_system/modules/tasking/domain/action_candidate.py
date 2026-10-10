"""AGT-06 建议型Action候选、运行组限额画像与ID派生（零LLM、零I/O）。

模型唯一可控内容是 binding snapshot 已披露的 model_tool_name 与 arguments；
Task/Plan/Item/Agent/Snapshot 引用、限额画像、ID 与 Digest 均由服务端生成。
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, JsonValue, StringConstraints, model_validator

from .core_types import (
    ArtifactRefV1,
    Sha256Hex,
    StableKey,
    StrictContractV1,
    canonical_json_sha256,
)

ACTION_ATTEMPT_NAMESPACE = uuid5(NAMESPACE_URL, "urn:knowledge-system:action-attempt:v1")
CAPABILITY_INVOCATION_NAMESPACE = uuid5(
    NAMESPACE_URL, "urn:knowledge-system:capability-invocation:v1"
)

ActionLimitProfileId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*_v[1-9][0-9]*$"),
]

# 平台绝对上限：Tool Search / 业务Capability / 框架Tool Call / 模型请求。
PLATFORM_TOOL_SEARCH_CAP = 4
PLATFORM_PROPOSED_ACTION_CAP = 9
PLATFORM_FRAMEWORK_TOOL_CALL_CAP = 12
PLATFORM_MODEL_REQUEST_CAP = 12


class InlineJsonV1(StrictContractV1):
    """参数载荷严格二选一；INLINE_JSON 只允许 <64 KiB 的 JSON，两分支均不含 Secret。"""

    payload_kind: Literal["INLINE_JSON", "ARTIFACT_REF"]
    inline_json: JsonValue | None = None
    artifact_ref: ArtifactRefV1 | None = None
    serialized_bytes: int = Field(ge=0, lt=65536)

    @model_validator(mode="after")
    def validate_branch(self) -> InlineJsonV1:
        if self.payload_kind == "INLINE_JSON":
            if self.inline_json is None or self.artifact_ref is not None:
                raise ValueError("INLINE_JSON_RELATION_INVALID")
        elif self.artifact_ref is None or self.inline_json is not None:
            raise ValueError("ARTIFACT_REF_RELATION_INVALID")
        return self


class ProposedActionV1(StrictContractV1):
    contract_version: Literal["proposed_action_v1"]
    task_id: UUID
    origin_task_attempt_id: UUID
    plan_version: int = Field(ge=1)
    plan_item_id: UUID
    action_run_group_id: UUID
    agent_instance_id: UUID
    binding_snapshot_id: UUID
    binding_snapshot_digest: Sha256Hex
    origin_pydantic_run_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    model_step: int = Field(ge=1)
    model_tool_call_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    tool_call_ordinal: int = Field(ge=1)
    model_tool_name: StableKey
    arguments_payload: InlineJsonV1
    arguments_digest: Sha256Hex


class ActionRunLimitProfileV1(StrictContractV1):
    profile_id: ActionLimitProfileId
    agent_registration_keys: tuple[StableKey, ...] = Field(max_length=32)
    max_tool_search_count: int = Field(ge=0, le=4)
    max_proposed_action_count: int = Field(ge=0, le=9)
    max_framework_tool_call_count: int = Field(ge=0, le=12)
    max_model_request_count: int = Field(ge=0, le=12)
    profile_digest: Sha256Hex

    @model_validator(mode="after")
    def validate_limits(self) -> ActionRunLimitProfileV1:
        if (
            self.max_framework_tool_call_count
            != self.max_tool_search_count + self.max_proposed_action_count
        ):
            raise ValueError("FRAMEWORK_LIMIT_MUST_EQUAL_TOOL_PLUS_PROPOSED")
        return self


class ActionRunGroupV1(StrictContractV1):
    action_run_group_id: UUID
    task_id: UUID
    plan_version: int = Field(ge=1)
    plan_item_id: UUID
    iteration_no: int = Field(ge=1, le=3)
    origin_task_attempt_id: UUID
    resumed_task_attempt_ids: tuple[UUID, ...] = Field(max_length=8)
    limit_profile_id: ActionLimitProfileId
    limit_profile_digest: Sha256Hex
    max_tool_search_count: int = Field(ge=0, le=4)
    max_proposed_action_count: int = Field(ge=0, le=9)
    max_framework_tool_call_count: int = Field(ge=0, le=12)
    max_model_request_count: int = Field(ge=0, le=12)
    tool_search_count: int = Field(ge=0, le=4)
    proposed_action_count: int = Field(ge=0, le=9)
    framework_tool_call_count: int = Field(ge=0, le=12)
    model_request_count: int = Field(ge=0, le=12)
    execution_accepted_count: int = Field(ge=0, le=9)
    completed_action_count: int = Field(ge=0, le=9)

    @model_validator(mode="after")
    def validate_counters(self) -> ActionRunGroupV1:
        if self.framework_tool_call_count != self.tool_search_count + self.proposed_action_count:
            raise ValueError("FRAMEWORK_CALL_COUNT_MISMATCH")
        if self.max_framework_tool_call_count != (
            self.max_tool_search_count + self.max_proposed_action_count
        ):
            raise ValueError("FRAMEWORK_LIMIT_MUST_EQUAL_TOOL_PLUS_PROPOSED")
        if self.tool_search_count > self.max_tool_search_count:
            raise ValueError("TOOL_SEARCH_LIMIT_EXCEEDED")
        if self.proposed_action_count > self.max_proposed_action_count:
            raise ValueError("PROPOSED_ACTION_LIMIT_EXCEEDED")
        if self.framework_tool_call_count > self.max_framework_tool_call_count:
            raise ValueError("FRAMEWORK_CALL_LIMIT_EXCEEDED")
        if self.model_request_count > self.max_model_request_count:
            raise ValueError("MODEL_REQUEST_LIMIT_EXCEEDED")
        if self.execution_accepted_count > self.proposed_action_count:
            raise ValueError("EXECUTION_ACCEPTED_EXCEEDS_PROPOSED")
        if self.completed_action_count > self.execution_accepted_count:
            raise ValueError("COMPLETED_EXCEEDS_ACCEPTED")
        return self


def _freeze_limit_profile(
    profile_id: str,
    tool_search: int,
    proposed: int,
    framework: int,
    model_request: int,
) -> ActionRunLimitProfileV1:
    profile = ActionRunLimitProfileV1(
        profile_id=profile_id,
        agent_registration_keys=(),
        max_tool_search_count=tool_search,
        max_proposed_action_count=proposed,
        max_framework_tool_call_count=framework,
        max_model_request_count=model_request,
        profile_digest="0" * 64,
    )
    digest = canonical_json_sha256(profile.model_dump(mode="json", exclude={"profile_digest"}))
    return profile.model_copy(update={"profile_digest": digest})


ACTION_LIMIT_PROFILE_DEFAULT = _freeze_limit_profile("default_action_v1", 4, 4, 8, 10)
ACTION_LIMIT_PROFILE_KNOWLEDGE_RESEARCH = _freeze_limit_profile(
    "knowledge_research_action_v1", 0, 4, 4, 7
)
ACTION_LIMIT_PROFILE_SCIENTIFIC_ANALYSIS = _freeze_limit_profile(
    "scientific_analysis_action_v1", 0, 4, 4, 7
)
ACTION_LIMIT_PROFILE_SYNTHESIS = _freeze_limit_profile("synthesis_action_v1", 0, 4, 4, 7)
ACTION_LIMIT_PROFILE_FACT_VERIFICATION = _freeze_limit_profile(
    "fact_verification_action_v1", 0, 6, 6, 10
)
ACTION_LIMIT_PROFILE_EXTERNAL_RESEARCH = _freeze_limit_profile(
    "external_research_action_v1", 0, 9, 9, 12
)

ACTION_LIMIT_PROFILES: dict[str, ActionRunLimitProfileV1] = {
    profile.profile_id: profile
    for profile in (
        ACTION_LIMIT_PROFILE_DEFAULT,
        ACTION_LIMIT_PROFILE_KNOWLEDGE_RESEARCH,
        ACTION_LIMIT_PROFILE_SCIENTIFIC_ANALYSIS,
        ACTION_LIMIT_PROFILE_SYNTHESIS,
        ACTION_LIMIT_PROFILE_FACT_VERIFICATION,
        ACTION_LIMIT_PROFILE_EXTERNAL_RESEARCH,
    )
}


def new_action_run_group(
    *,
    action_run_group_id: UUID,
    task_id: UUID,
    plan_version: int,
    plan_item_id: UUID,
    iteration_no: int,
    origin_task_attempt_id: UUID,
    limit_profile: ActionRunLimitProfileV1,
    resumed_task_attempt_ids: tuple[UUID, ...] = (),
) -> ActionRunGroupV1:
    """创建运行组并冻结签名画像的 ID、Digest 与四项最大值，计数从零开始。"""

    return ActionRunGroupV1(
        action_run_group_id=action_run_group_id,
        task_id=task_id,
        plan_version=plan_version,
        plan_item_id=plan_item_id,
        iteration_no=iteration_no,
        origin_task_attempt_id=origin_task_attempt_id,
        resumed_task_attempt_ids=resumed_task_attempt_ids,
        limit_profile_id=limit_profile.profile_id,
        limit_profile_digest=limit_profile.profile_digest,
        max_tool_search_count=limit_profile.max_tool_search_count,
        max_proposed_action_count=limit_profile.max_proposed_action_count,
        max_framework_tool_call_count=limit_profile.max_framework_tool_call_count,
        max_model_request_count=limit_profile.max_model_request_count,
        tool_search_count=0,
        proposed_action_count=0,
        framework_tool_call_count=0,
        model_request_count=0,
        execution_accepted_count=0,
        completed_action_count=0,
    )


def action_attempt_id(origin_task_attempt_id: UUID, model_tool_call_id: str) -> UUID:
    """action_attempt_id 在 Deferred 恢复与 Activity 重试中保持不变。"""

    if not model_tool_call_id:
        raise ValueError("ACTION_ATTEMPT_ID_INPUT_INVALID")
    name = f"{str(origin_task_attempt_id).lower()}:{model_tool_call_id}"
    return uuid5(ACTION_ATTEMPT_NAMESPACE, name)


def invocation_id(action_attempt_id_value: UUID) -> UUID:
    return uuid5(CAPABILITY_INVOCATION_NAMESPACE, str(action_attempt_id_value).lower())
