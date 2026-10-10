"""DEV-03D：外置载荷Bundle端口（加密前≥64 KiB的Payload不进History）。

Codec把超阈值载荷交给`PayloadBundleStore.put`，History只保留小型`PayloadBundleRefV1`
（身份UUID + SHA-256 + 原始字节数），不含正文、不含Secret。真实实现映射到
`content.artifact`（同一Blob/加密画像），本批先以内存替身验证编解码闭环。
"""

from __future__ import annotations

import hashlib
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import Field

from knowledge_system.modules.tasking.domain.core_types import Sha256Hex, StrictContractV1
from knowledge_system.modules.temporal.payload.errors import PayloadIntegrityError


class PayloadBundleRefV1(StrictContractV1):
    """Codec外置载荷的最小引用；`sha256`在取回时逐字节复核。"""

    bundle_id: UUID
    sha256: Sha256Hex
    size_bytes: int = Field(ge=0)


class PayloadBundleStore(Protocol):
    """外置载荷存储端口；`put`铸币引用，`get`按引用取回并校验完整性。"""

    async def put(self, data: bytes) -> PayloadBundleRefV1: ...

    async def get(self, ref: PayloadBundleRefV1) -> bytes: ...


class InMemoryPayloadBundleStore:
    """内存替身：`put`铸币UUID并算SHA-256，`get`校验摘要与大小后再返回。"""

    def __init__(self) -> None:
        self._blobs: dict[UUID, bytes] = {}

    async def put(self, data: bytes) -> PayloadBundleRefV1:
        ref = PayloadBundleRefV1(
            bundle_id=uuid4(),
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )
        self._blobs[ref.bundle_id] = data
        return ref

    async def get(self, ref: PayloadBundleRefV1) -> bytes:
        data = self._blobs.get(ref.bundle_id)
        if data is None:
            raise PayloadIntegrityError(f"bundle not found: {ref.bundle_id}")
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise PayloadIntegrityError(f"bundle digest mismatch: {ref.bundle_id}")
        if len(data) != ref.size_bytes:
            raise PayloadIntegrityError(f"bundle size mismatch: {ref.bundle_id}")
        return data


__all__ = [
    "InMemoryPayloadBundleStore",
    "PayloadBundleRefV1",
    "PayloadBundleStore",
]
