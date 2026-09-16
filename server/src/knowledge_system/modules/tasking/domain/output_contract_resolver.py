"""output_contract_resolver_v1：不读取正文、模型、History或Memory的纯解析器。"""

from __future__ import annotations

from typing import Literal

import langcodes

from .core_types import ResourceRefV1, SchemaRefV1, StrictContractV1
from .input_contracts import OutputContractV1


class OutputContractRegistrySnapshotV1(StrictContractV1):
    schema_refs: tuple[SchemaRefV1, ...]
    template_or_skill_refs: tuple[ResourceRefV1, ...]


class FixedWorkflowOutputProfileV1(StrictContractV1):
    decision: Literal["ADMIT_FIXED_WORKFLOW", "SUPERVISOR"]
    default_contract: OutputContractV1 | None


class OutputContractResolutionV1(StrictContractV1):
    resolver_version: Literal["output_contract_resolver_v1"]
    contract: OutputContractV1
    route_decision: Literal["ADMIT_FIXED_WORKFLOW", "SUPERVISOR"]
    reason_code: Literal["OUTPUT_CONTRACT_MISMATCH"] | None


class OutputContractResolutionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical_language(value: str) -> str:
    try:
        canonical = langcodes.standardize_tag(value)
    except (LookupError, ValueError) as error:
        raise OutputContractResolutionError("OUTPUT_CONTRACT_INVALID") from error
    if not langcodes.tag_is_valid(canonical):
        raise OutputContractResolutionError("OUTPUT_CONTRACT_INVALID")
    return canonical


def _canonical_contract(contract: OutputContractV1) -> OutputContractV1:
    return contract.model_copy(update={"language": _canonical_language(contract.language)})


def _validate_registry(
    contract: OutputContractV1, registry: OutputContractRegistrySnapshotV1
) -> None:
    schema_ref = contract.schema_ref
    if schema_ref is not None and schema_ref not in registry.schema_refs:
        same_identity = any(
            candidate.schema_id == schema_ref.schema_id
            and candidate.schema_version == schema_ref.schema_version
            for candidate in registry.schema_refs
        )
        code = "OUTPUT_CONTRACT_REF_STALE" if same_identity else "OUTPUT_CONTRACT_REF_NOT_FOUND"
        raise OutputContractResolutionError(code)
    template_ref = contract.template_or_skill_ref
    if template_ref is not None and template_ref not in registry.template_or_skill_refs:
        raise OutputContractResolutionError("OUTPUT_CONTRACT_REF_NOT_FOUND")


def resolve_output_contract(
    *,
    explicit_contract: OutputContractV1 | None,
    authenticated_locale: str,
    fixed_workflow: FixedWorkflowOutputProfileV1,
    registry: OutputContractRegistrySnapshotV1,
) -> OutputContractResolutionV1:
    """执行已批准的两阶段Output Contract规则。"""

    if explicit_contract is None:
        base_contract = OutputContractV1(
            format="MARKDOWN",
            delivery="INLINE",
            language=_canonical_language(authenticated_locale),
            schema_ref=None,
            template_or_skill_ref=None,
            citations="REQUIRED",
            max_output_bytes=1_048_576,
        )
    else:
        base_contract = _canonical_contract(explicit_contract)
    _validate_registry(base_contract, registry)

    if fixed_workflow.decision == "SUPERVISOR":
        if fixed_workflow.default_contract is not None:
            raise OutputContractResolutionError("OUTPUT_CONTRACT_PROFILE_INVALID")
        return OutputContractResolutionV1(
            resolver_version="output_contract_resolver_v1",
            contract=base_contract,
            route_decision="SUPERVISOR",
            reason_code=None,
        )

    if fixed_workflow.default_contract is None:
        raise OutputContractResolutionError("OUTPUT_CONTRACT_PROFILE_INVALID")
    fixed_contract = _canonical_contract(fixed_workflow.default_contract)
    _validate_registry(fixed_contract, registry)
    if explicit_contract is None:
        return OutputContractResolutionV1(
            resolver_version="output_contract_resolver_v1",
            contract=fixed_contract,
            route_decision="ADMIT_FIXED_WORKFLOW",
            reason_code=None,
        )
    if base_contract == fixed_contract:
        return OutputContractResolutionV1(
            resolver_version="output_contract_resolver_v1",
            contract=base_contract,
            route_decision="ADMIT_FIXED_WORKFLOW",
            reason_code=None,
        )
    return OutputContractResolutionV1(
        resolver_version="output_contract_resolver_v1",
        contract=base_contract,
        route_decision="SUPERVISOR",
        reason_code="OUTPUT_CONTRACT_MISMATCH",
    )
