"""DEV-03D：Temporal `PayloadCodec`——AES-256-GCM加密与64 KiB外置。

进History的编码约定：

- 内联载荷（<阈值）：`encoding=binary/encrypted`，`data`为AES-256-GCM密文，
  metadata携带`kid`（密钥ID）与`nonce`（Base64的12字节随机数）；密钥字节永不入
  metadata/History。
- 外置载荷（≥阈值）：`encoding=binary/artifact_ref`，`data`为明文`PayloadBundleRefV1`
  JSON（身份UUID+SHA-256+大小），正文交给`PayloadBundleStore`，不携带Secret。
- 解码把两种编码都还原为原Payload Converter的`json/plain`，再由Pydantic反序列化。

失败关闭：未知`kid`、GCM认证失败（`InvalidTag`）、Bundle摘要/大小不符、超阈值却未配置
外置存储，一律抛`PayloadCodecError`子类，不静默丢数据、不猜测密钥。
"""

from __future__ import annotations

import base64
import os
from collections.abc import Sequence

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec

from knowledge_system.modules.temporal.payload.bundle_store import (
    PayloadBundleRefV1,
    PayloadBundleStore,
)
from knowledge_system.modules.temporal.payload.errors import (
    PayloadCodecError,
    PayloadIntegrityError,
)
from knowledge_system.modules.temporal.payload.key_provider import PayloadKeyProvider

ENCRYPTED_ENCODING = b"binary/encrypted"
BUNDLE_REF_ENCODING = b"binary/artifact_ref"
PLAIN_ENCODING = b"json/plain"
NONCE_BYTES = 12
DEFAULT_MAX_INLINE_BYTES = 64 * 1024

# 加密/外置前把原`encoding`（如`json/plain`、`binary/null`）存入`orig-encoding`，解码时
# 原样还原，避免把`binary/null`（None值）误归一为`json/plain`导致空字节反序列化失败。
ORIGINAL_ENCODING = "orig-encoding"


def _clone(payload: Payload) -> Payload:
    """深拷贝一个`Payload`；Codec不得原地改写上游传入对象。"""

    out = Payload()
    out.CopyFrom(payload)
    return out


class EncryptionPayloadCodec(PayloadCodec):
    """内联加密 + 超阈值外置，二者共用同一Codec（1:1映射，不增删Payload个数）。"""

    def __init__(
        self,
        key_provider: PayloadKeyProvider,
        bundle_store: PayloadBundleStore | None = None,
        *,
        max_inline_bytes: int = DEFAULT_MAX_INLINE_BYTES,
    ) -> None:
        if max_inline_bytes < 0:
            raise ValueError("MAX_INLINE_BYTES_NEGATIVE")
        self._key_provider = key_provider
        self._bundle_store = bundle_store
        self._max_inline_bytes = max_inline_bytes

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [await self._encode_one(payload) for payload in payloads]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [await self._decode_one(payload) for payload in payloads]

    async def _encode_one(self, payload: Payload) -> Payload:
        encoding = payload.metadata.get("encoding", b"")
        if encoding in (ENCRYPTED_ENCODING, BUNDLE_REF_ENCODING):
            # 已编码：幂等，直接透传，避免二次加密/二次外置。
            return _clone(payload)
        out = _clone(payload)
        data = out.data
        if len(data) >= self._max_inline_bytes:
            out.data = (await self._externalize(data)).model_dump_json().encode("utf-8")
            out.metadata[ORIGINAL_ENCODING] = encoding
            out.metadata["encoding"] = BUNDLE_REF_ENCODING
            return out
        key_id = self._key_provider.current_key_id
        key = await self._key_provider.get_key(key_id)
        nonce = os.urandom(NONCE_BYTES)
        out.data = AESGCM(key).encrypt(nonce, data, None)
        out.metadata[ORIGINAL_ENCODING] = encoding
        out.metadata["encoding"] = ENCRYPTED_ENCODING
        out.metadata["kid"] = key_id.encode("utf-8")
        out.metadata["nonce"] = base64.b64encode(nonce)
        return out

    async def _decode_one(self, payload: Payload) -> Payload:
        encoding = payload.metadata.get("encoding", b"")
        if encoding == ENCRYPTED_ENCODING:
            return await self._decrypt(payload)
        if encoding == BUNDLE_REF_ENCODING:
            return await self._retrieve(payload)
        return _clone(payload)

    async def _decrypt(self, payload: Payload) -> Payload:
        kid_bytes = payload.metadata.get("kid")
        nonce_bytes = payload.metadata.get("nonce")
        if kid_bytes is None or nonce_bytes is None:
            raise PayloadCodecError("ENCRYPTED_PAYLOAD_MISSING_METADATA")
        key_id = kid_bytes.decode("utf-8")
        key = await self._key_provider.get_key(key_id)
        try:
            nonce = base64.b64decode(nonce_bytes, validate=True)
        except ValueError as exc:
            raise PayloadCodecError("ENCRYPTED_PAYLOAD_BAD_NONCE") from exc
        try:
            plaintext = AESGCM(key).decrypt(nonce, payload.data, None)
        except InvalidTag as exc:
            raise PayloadIntegrityError("GCM_AUTHENTICATION_FAILED") from exc
        out = _clone(payload)
        out.data = plaintext
        out.metadata["encoding"] = payload.metadata.get(ORIGINAL_ENCODING, PLAIN_ENCODING)
        out.metadata.pop(ORIGINAL_ENCODING, None)
        out.metadata.pop("kid", None)
        out.metadata.pop("nonce", None)
        return out

    async def _retrieve(self, payload: Payload) -> Payload:
        if self._bundle_store is None:
            raise PayloadCodecError("BUNDLE_REF_WITHOUT_STORE")
        ref = PayloadBundleRefV1.model_validate_json(payload.data.decode("utf-8"))
        data = await self._bundle_store.get(ref)
        out = _clone(payload)
        out.data = data
        out.metadata["encoding"] = payload.metadata.get(ORIGINAL_ENCODING, PLAIN_ENCODING)
        out.metadata.pop(ORIGINAL_ENCODING, None)
        return out

    async def _externalize(self, data: bytes) -> PayloadBundleRefV1:
        if self._bundle_store is None:
            raise PayloadCodecError("PAYLOAD_TOO_LARGE_WITHOUT_BUNDLE_STORE")
        return await self._bundle_store.put(data)


__all__ = [
    "BUNDLE_REF_ENCODING",
    "DEFAULT_MAX_INLINE_BYTES",
    "ENCRYPTED_ENCODING",
    "NONCE_BYTES",
    "PLAIN_ENCODING",
    "EncryptionPayloadCodec",
]
