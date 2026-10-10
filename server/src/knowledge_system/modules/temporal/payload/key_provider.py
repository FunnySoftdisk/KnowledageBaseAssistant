"""DEV-03D：AES-256密钥提供端口。

密钥材料只经此端口进出Codec；Codec把`kid`写进History metadata，但绝不写密钥字节。
`InMemoryPayloadKeyProvider`仅为本地dev server验收生成一次性随机密钥，不持久化、
不写文件、不进Git；真实密钥提供（OpenBao/Broker）随Secret接线落地后实现同一端口，
Codec不感知来源。
"""

from __future__ import annotations

import os
from typing import Protocol

from knowledge_system.modules.temporal.payload.errors import UnknownPayloadKeyError

AES_256_GCM_KEY_BYTES = 32


class PayloadKeyProvider(Protocol):
    """提供AES-256密钥材料；`current_key_id`标识加密用当前密钥，`get_key`按ID取密钥。"""

    @property
    def current_key_id(self) -> str: ...

    async def get_key(self, key_id: str) -> bytes: ...


class InMemoryPayloadKeyProvider:
    """单密钥内存实现：密钥材料永不出进程，仅用于测试/本地dev server。

    密钥在构造时生成（或由调用方显式传入测试密钥），从不序列化，也不暴露为文本。
    """

    def __init__(self, *, key_id: str = "local-dev-v1", key: bytes | None = None) -> None:
        if key is None:
            key = os.urandom(AES_256_GCM_KEY_BYTES)
        if len(key) != AES_256_GCM_KEY_BYTES:
            raise ValueError("PAYLOAD_KEY_WRONG_SIZE")
        if not key_id:
            raise ValueError("PAYLOAD_KEY_ID_EMPTY")
        self._key_id = key_id
        self._key = key

    @property
    def current_key_id(self) -> str:
        return self._key_id

    async def get_key(self, key_id: str) -> bytes:
        if key_id != self._key_id:
            raise UnknownPayloadKeyError(f"unknown payload key id: {key_id}")
        return self._key


__all__ = [
    "AES_256_GCM_KEY_BYTES",
    "InMemoryPayloadKeyProvider",
    "PayloadKeyProvider",
]
