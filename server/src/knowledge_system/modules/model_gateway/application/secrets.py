"""Provider Key安全交接端口；Secret正文不进入DTO、日志或回执。"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import Field

from knowledge_system.modules.tasking.domain.core_types import StrictContractV1


class SecretWriteReceiptV1(StrictContractV1):
    """安全写入回执；只证明指定KV版本写入成功，不包含Secret正文。"""

    secret_ref: str = Field(min_length=1, max_length=256)
    version: int = Field(ge=1)


class ProviderSecretWriter(Protocol):
    """操作者侧无回显入口调用的最小KV v2写端口。"""

    async def write_provider_key(
        self,
        *,
        secret_path: str,
        api_key: bytearray,
        expected_version: int,
    ) -> SecretWriteReceiptV1: ...


class ProviderSecretReader(Protocol):
    """Broker侧运行期读取端口；调用方必须在读取前重验当前授权。"""

    async def read_provider_key(self, *, secret_path: str, version: int) -> bytearray: ...


class ProviderSecretReadRequestV1(StrictContractV1):
    """Broker取密请求；只含授权与定位信息，不含Secret正文。"""

    model_call_id: UUID
    route_ref: str = Field(min_length=1, max_length=256)
    secret_ref: str = Field(min_length=1, max_length=256)
    secret_version: int = Field(ge=1)


class ProviderSecretReadAuthorizer(Protocol):
    """Broker在每次OpenBao读取前重验当前调用资格。"""

    async def authorize(self, request: ProviderSecretReadRequestV1) -> bool: ...


class ProviderSecretBroker(Protocol):
    """Bridge唯一允许调用的Provider Key取用端口。"""

    async def read_provider_key(self, request: ProviderSecretReadRequestV1) -> bytearray: ...


__all__ = [
    "ProviderSecretBroker",
    "ProviderSecretReader",
    "ProviderSecretReadAuthorizer",
    "ProviderSecretReadRequestV1",
    "ProviderSecretWriter",
    "SecretWriteReceiptV1",
]
