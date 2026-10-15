"""一次性出网许可签发方（基础设施层）。

把Policy Adapter的`EgressPermitIssuanceRequestV1`翻译为PG Authority所需的`EgressPermitV1`：
签发时解析`secret_ref`（身份HMAC派生路径，确定性、不泄露输入标识）与`secret_version`
（OpenBao KV v2元数据当前版本，只读版本号不读Provider Key正文），再交
`PostgresEgressPermitAuthority.issue_ready()`落`READY`。SecretRef/SecretVersion只在签发方与
许可记录内，绝不回传Policy Adapter，也不进入签发请求载荷。

签发方不持有、不读取Provider Key；版本号由`ProviderSecretVersionResolver`端口注入以便离线
测试替换。真实OpenBao元数据读取经`OpenBaoMetadataVersionResolver`封装`read_current_version`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol

from knowledge_system.infrastructure.persistence.egress_permit_repository import (
    EgressPermitContractError,
    PostgresEgressPermitAuthority,
)
from knowledge_system.modules.model_gateway.application.egress_permit_issuance import (
    EgressPermitIssuanceDisposition,
    EgressPermitIssuanceRequestV1,
    EgressPermitIssuanceResultV1,
)
from knowledge_system.modules.model_gateway.domain.egress_permit import (
    EgressPermitStatus,
    EgressPermitV1,
)
from knowledge_system.modules.model_gateway.domain.secret_reference import (
    derive_provider_secret_path,
)

from .openbao import OpenBaoKv2Client


class ProviderSecretVersionResolver(Protocol):
    """解析Provider Secret当前KV v2版本；实现只返回版本号，不返回Key正文。"""

    async def current_version(self, secret_path: str) -> int: ...


class OpenBaoMetadataVersionResolver:
    """以OpenBao KV v2元数据端点的`current_version`解析Secret版本号。"""

    def __init__(self, client: OpenBaoKv2Client) -> None:
        self._client = client

    async def current_version(self, secret_path: str) -> int:
        return await self._client.read_current_version(secret_path=secret_path)


class PostgresEgressPermitIssuer:
    """基础设施层出网许可签发方；只解析SecretRef/版本号并落READY，不读取Provider Key。"""

    def __init__(
        self,
        *,
        authority: PostgresEgressPermitAuthority,
        version_resolver: ProviderSecretVersionResolver,
        identity_hmac_key: bytes,
        workspace_id: str,
        provider: Literal["alibaba_bailian"] = "alibaba_bailian",
        region: str = "cn-beijing",
        mount: str = "knowledge-model",
    ) -> None:
        self._authority = authority
        self._version_resolver = version_resolver
        self._identity_hmac_key = identity_hmac_key
        self._workspace_id = workspace_id
        self._provider = provider
        self._region = region
        self._mount = mount

    async def issue_ready(
        self, request: EgressPermitIssuanceRequestV1
    ) -> EgressPermitIssuanceResultV1:
        secret_path = derive_provider_secret_path(
            identity_hmac_key=self._identity_hmac_key,
            workspace_id=self._workspace_id,
            provider=self._provider,
            region=self._region,
        )
        secret_version = await self._version_resolver.current_version(secret_path)
        secret_ref = f"openbao-kv-v2://{self._mount}/{secret_path}"

        created_at = datetime.now(UTC)
        if request.expires_at <= created_at:
            raise EgressPermitContractError("EGRESS_PERMIT_EXPIRY_INVALID")
        permit = EgressPermitV1(
            model_call_id=request.model_call_id,
            route_ref=request.route_ref,
            provider=request.provider,
            provider_model=request.provider_model,
            secret_ref=secret_ref,
            secret_version=secret_version,
            body=request.body,
            authorization_version=request.authorization_version,
            execution_generation=request.execution_generation,
            cancellation_generation=request.cancellation_generation,
            status=EgressPermitStatus.READY,
            expires_at=request.expires_at,
            row_version=1,
            created_at=created_at,
        )
        disposition = await self._authority.issue_ready(permit)
        return EgressPermitIssuanceResultV1(
            disposition=EgressPermitIssuanceDisposition(disposition.value)
        )


__all__ = [
    "OpenBaoMetadataVersionResolver",
    "PostgresEgressPermitIssuer",
    "ProviderSecretVersionResolver",
]
