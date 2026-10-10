"""DEV-03D Payload Codec单元验收：AES-256-GCM加密、64 KiB外置与失败关闭。

不启动Temporal dev server：直接以`build_data_converter(codec)`和裸`Payload`驱动Codec，
验证内联加密/外置/幂等/篡改拒绝/未知密钥/摘要不符，以及密钥材料永不入metadata。
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from temporalio.api.common.v1 import Payload

from knowledge_system.modules.temporal.domain.contracts import (
    AttemptDispatchV1,
    TaskRefV1,
)
from knowledge_system.modules.temporal.payload import (
    AES_256_GCM_KEY_BYTES,
    BUNDLE_REF_ENCODING,
    ENCRYPTED_ENCODING,
    EncryptionPayloadCodec,
    InMemoryPayloadBundleStore,
    InMemoryPayloadKeyProvider,
    PayloadBundleRefV1,
    PayloadCodecError,
    PayloadIntegrityError,
    UnknownPayloadKeyError,
    build_data_converter,
)

_TEST_KEY = b"k" * AES_256_GCM_KEY_BYTES


def _codec(
    *,
    key_id: str = "test-key",
    key: bytes = _TEST_KEY,
    bundle_store: InMemoryPayloadBundleStore | None = None,
    max_inline_bytes: int = 64 * 1024,
) -> EncryptionPayloadCodec:
    return EncryptionPayloadCodec(
        InMemoryPayloadKeyProvider(key_id=key_id, key=key),
        bundle_store,
        max_inline_bytes=max_inline_bytes,
    )


def _value() -> TaskRefV1:
    return TaskRefV1(task_id=uuid4())


class Dev03PayloadCodecTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_payload_roundtrips_encrypted(self) -> None:
        codec = _codec()
        converter = build_data_converter(codec)
        value = _value()

        (payload,) = await converter.encode([value])
        self.assertEqual(payload.metadata["encoding"], ENCRYPTED_ENCODING)
        self.assertEqual(payload.metadata["kid"], b"test-key")
        self.assertIn("nonce", payload.metadata)
        # 密文不是原文JSON，且密钥字节不在metadata/History。
        self.assertNotIn(b"task_id", payload.data)
        self.assertNotIn(_TEST_KEY, payload.data + payload.SerializeToString())

        (decoded,) = await converter.decode([payload], [TaskRefV1])
        self.assertEqual(decoded, value)

    async def test_ciphertext_has_random_nonce_per_encode(self) -> None:
        codec = _codec()
        value = _value()
        first = (await build_data_converter(codec).encode([value]))[0]
        second = (await build_data_converter(codec).encode([value]))[0]
        self.assertNotEqual(first.data, second.data)
        self.assertNotEqual(first.metadata["nonce"], second.metadata["nonce"])

    async def test_encode_idempotent_on_encrypted_and_bundle_ref(self) -> None:
        codec = _codec(bundle_store=InMemoryPayloadBundleStore(), max_inline_bytes=1)
        value = _value()
        (encoded,) = await build_data_converter(codec).encode([value])
        self.assertEqual(encoded.metadata["encoding"], BUNDLE_REF_ENCODING)
        # 再次encode已编码载荷：不改写（不二次外置/二次加密）。
        (re_encoded,) = await codec.encode([encoded])
        self.assertEqual(re_encoded.data, encoded.data)
        self.assertEqual(re_encoded.metadata["encoding"], BUNDLE_REF_ENCODING)

    async def test_decode_unknown_key_fails_closed(self) -> None:
        writer = _codec(key_id="writer-key")
        (payload,) = await build_data_converter(writer).encode([_value()])
        reader = _codec(key_id="reader-key")
        with self.assertRaises(UnknownPayloadKeyError):
            await build_data_converter(reader).decode([payload], [TaskRefV1])

    async def test_decode_tampered_ciphertext_fails_closed(self) -> None:
        codec = _codec()
        (payload,) = await build_data_converter(codec).encode([_value()])
        payload.data = payload.data[:-1] + bytes([payload.data[-1] ^ 0xFF])
        with self.assertRaises(PayloadIntegrityError):
            await build_data_converter(codec).decode([payload], [TaskRefV1])

    async def test_decode_missing_metadata_fails_closed(self) -> None:
        codec = _codec()
        payload = Payload()
        payload.metadata["encoding"] = ENCRYPTED_ENCODING
        payload.data = b"x" * 16
        with self.assertRaises(PayloadCodecError):
            await codec.decode([payload])

    async def test_decode_bad_nonce_fails_closed(self) -> None:
        codec = _codec()
        payload = Payload()
        payload.metadata["encoding"] = ENCRYPTED_ENCODING
        payload.metadata["kid"] = b"test-key"
        payload.metadata["nonce"] = b"not-base64!"
        payload.data = b"x" * 16
        with self.assertRaises(PayloadCodecError):
            await codec.decode([payload])

    async def test_oversized_payload_is_externalized(self) -> None:
        store = InMemoryPayloadBundleStore()
        codec = _codec(bundle_store=store, max_inline_bytes=8)
        value = _value()

        (payload,) = await build_data_converter(codec).encode([value])
        self.assertEqual(payload.metadata["encoding"], BUNDLE_REF_ENCODING)
        self.assertNotIn(b"task_id", payload.data)
        ref = PayloadBundleRefV1.model_validate_json(payload.data.decode("utf-8"))
        self.assertEqual(ref.size_bytes, len(await store.get(ref)))

        (decoded,) = await build_data_converter(codec).decode([payload], [TaskRefV1])
        self.assertEqual(decoded, value)

    async def test_oversized_without_store_fails_closed(self) -> None:
        codec = _codec(max_inline_bytes=8)
        with self.assertRaises(PayloadCodecError):
            await build_data_converter(codec).encode([_value()])

    async def test_bundle_ref_without_store_fails_closed_on_decode(self) -> None:
        store = InMemoryPayloadBundleStore()
        writer = _codec(bundle_store=store, max_inline_bytes=8)
        (payload,) = await build_data_converter(writer).encode([_value()])
        reader = _codec(max_inline_bytes=8)
        with self.assertRaises(PayloadCodecError):
            await build_data_converter(reader).decode([payload], [TaskRefV1])

    async def test_none_value_preserves_binary_null_encoding(self) -> None:
        # 回归：None活动结果以`binary/null`进入Codec，解码必须还原`binary/null`而非
        # 误归一为`json/plain`——否则空字节被当JSON反序列化→EOF错误→workflow任务死循环。
        codec = _codec()
        (payload,) = await build_data_converter(codec).encode([None])
        self.assertEqual(payload.metadata["encoding"], ENCRYPTED_ENCODING)
        self.assertEqual(payload.metadata["orig-encoding"], b"binary/null")
        (decoded,) = await build_data_converter(codec).decode(
            [payload], [AttemptDispatchV1]
        )
        self.assertIsNone(decoded)

    async def test_plain_payload_passes_through(self) -> None:
        codec = _codec()
        payload = Payload()
        payload.metadata["encoding"] = b"json/plain"
        payload.data = b'{"task_id":"00000000-0000-0000-0000-000000000000"}'
        (decoded,) = await codec.decode([payload])
        self.assertEqual(decoded.metadata["encoding"], b"json/plain")
        self.assertEqual(decoded.data, payload.data)

    async def test_key_provider_rejects_wrong_size_and_unknown_id(self) -> None:
        with self.assertRaises(ValueError):
            InMemoryPayloadKeyProvider(key=b"short")
        provider = InMemoryPayloadKeyProvider(key_id="k", key=_TEST_KEY)
        self.assertEqual(provider.current_key_id, "k")
        self.assertEqual(await provider.get_key("k"), _TEST_KEY)
        with self.assertRaises(UnknownPayloadKeyError):
            await provider.get_key("other")

    async def test_bundle_store_rejects_missing_and_tampered(self) -> None:
        store = InMemoryPayloadBundleStore()
        ref = await store.put(b"hello bundle")
        self.assertEqual(await store.get(ref), b"hello bundle")
        # 篡改内容：摘要不符被拒绝。
        tampered = PayloadBundleRefV1(
            bundle_id=ref.bundle_id,
            sha256="0" * 64,
            size_bytes=ref.size_bytes,
        )
        with self.assertRaises(PayloadIntegrityError):
            await store.get(tampered)
        # 未知ID：拒绝。
        missing = PayloadBundleRefV1(bundle_id=uuid4(), sha256="0" * 64, size_bytes=0)
        with self.assertRaises(PayloadIntegrityError):
            await store.get(missing)


if __name__ == "__main__":
    unittest.main()
