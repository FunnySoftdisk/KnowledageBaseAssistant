"""DEV-03D：Payload Codec的失败关闭异常。

Codec从不静默丢数据：未知密钥、GCM认证失败、Bundle摘要/大小不符、超阈值却无外置
存储一律抛`PayloadCodecError`，让Temporal以失败而不是损坏数据收敛。
"""

from __future__ import annotations


class PayloadCodecError(Exception):
    """Payload Codec编解码失败（密钥/格式/配置），一律失败关闭。"""


class UnknownPayloadKeyError(PayloadCodecError):
    """密钥ID未知：无法解密，拒绝静默丢弃或猜测。"""


class PayloadIntegrityError(PayloadCodecError):
    """AES-GCM认证失败或Bundle摘要/大小不符：载荷被篡改或损坏。"""


__all__ = [
    "PayloadCodecError",
    "PayloadIntegrityError",
    "UnknownPayloadKeyError",
]
