"""AGT-06 ActionBinder 动作事实：冻结的规范事实子类型与十个 Normalizer（零LLM、零I/O）。

模块08 §3.2 冻结 `ActionBinderV1` 首版只允许十个 Release 注册 Normalizer
（`opaque_string_v1/resource_ref_v1/network_target_v1/absolute_uri_v1/workspace_path_v1/
recipient_set_v1/ordered_sequence_v1/identifier_set_v1/utc_timestamp_v1/byte_count_v1`），
把模型 Tool 参数经 RFC 6901 投影后规范化为不含 Secret/凭据的规范动作事实。

Normalizer 只做“严格校验 + 规范化”，不做类型强转；禁止 JSONPath/JMESPath、
动态脚本与 Rego 猜测。业务规范化完成后由 ActionBinder 用 RFC 8785 JCS 计算 Digest；
JCS 本身不改变 Unicode 字符串或数组顺序。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from pydantic import Field, JsonValue, StringConstraints

from .core_types import Sha256Hex, StrictContractV1

_OPAQUE_STRING_MAX = 4096
_ORDERED_SEQUENCE_MAX = 256
_IDENTIFIER_SET_MAX = 256
_RECIPIENT_SET_MAX = 64

# 规范事实标识可容纳 URI/路径/不透明字符串，比 StableKey 更长；仍非空且无控制语义。
FactIdentifier = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
HostName = Annotated[str, StringConstraints(min_length=1, max_length=253)]

_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_CONTROL_CHARS = frozenset(chr(i) for i in range(32))


class CanonicalResourceRefV1(StrictContractV1):
    """规范化资源引用：身份+类别+可选完整性摘要；不含路径外泄、Secret 或凭据。"""

    resource_kind: Literal["ARTIFACT", "WORKSPACE_PATH", "ABSOLUTE_URI", "OPAQUE"]
    identifier: FactIdentifier
    integrity_digest: Sha256Hex | None = None


class CanonicalNetworkTargetV1(StrictContractV1):
    """规范化网络目标；host 按 DNS 大小写不敏感规则折叠为小写，不改变其余值。"""

    scheme: Literal["http", "https", "ws", "wss", "tcp", "udp"]
    host: HostName
    port: int | None = Field(default=None, ge=1, le=65535)
    path: str | None = Field(default=None, max_length=2048)


class CanonicalRecipientV1(StrictContractV1):
    recipient_kind: Literal["USER_ID", "TEAM_ID", "CHANNEL", "EMAIL", "ABSOLUTE_URI", "OPAQUE"]
    identifier: FactIdentifier


class DataFlowFactV1(StrictContractV1):
    """数据输入/出网事实；ARTIFACT 来自资源引用，ABSOLUTE_URI/OPAQUE 来自对应 Normalizer。"""

    payload_kind: Literal["ARTIFACT", "ABSOLUTE_URI", "OPAQUE"]
    identifier: FactIdentifier
    byte_count: int | None = Field(default=None, ge=0)
    integrity_digest: Sha256Hex | None = None


class CanonicalActionFactsV1(StrictContractV1):
    """动作的规范事实集合；服务端注入 action_family/effect_class/idempotency_mode/
    risk_floor/purpose_code，模型不得声明这些权威字段。"""

    action_family: str = Field(min_length=1, max_length=128)
    resources: tuple[CanonicalResourceRefV1, ...] = Field(max_length=32)
    network_targets: tuple[CanonicalNetworkTargetV1, ...] = Field(max_length=32)
    recipients: tuple[CanonicalRecipientV1, ...] = Field(max_length=64)
    data_inputs: tuple[DataFlowFactV1, ...] = Field(max_length=32)
    data_egress: tuple[DataFlowFactV1, ...] = Field(max_length=32)
    effect_class: Literal["READ_ONLY", "WRITE_REVERSIBLE", "WRITE_IRREVERSIBLE", "UNKNOWN"]
    idempotency_mode: Literal["READ", "PROVIDER_KEY", "NONE", "UNKNOWN"]
    risk_floor: Literal["R0", "R1", "R2", "R3"]
    normalized_arguments: dict[str, JsonValue]
    purpose_code: str = Field(min_length=1, max_length=128)


def _require_str(value: Any, code: str) -> str:
    if not isinstance(value, str):
        raise ValueError(code)
    return value


def _require_nonempty_str(value: Any, code: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(code)
    return value


def _require_object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(code)
    return value


def _require_list(value: Any, code: str, max_length: int) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(code)
    if len(value) > max_length:
        raise ValueError(code)
    return value


def _has_control_or_whitespace(value: str) -> bool:
    return any(ch in _CONTROL_CHARS or ch.isspace() for ch in value)


def _normalize_opaque_string(value: Any) -> str:
    """非空不透明字符串；拒绝任何非字符串输入，不做解释或类型强转。"""

    return _require_nonempty_str(value, "OPAQUE_STRING_INVALID", _OPAQUE_STRING_MAX)


def _normalize_resource_ref(value: Any) -> CanonicalResourceRefV1:
    """严格校验规范资源引用，不接受简写或隐式补全（禁止类型强转）。"""

    obj = _require_object(value, "RESOURCE_REF_INVALID")
    allowed = {"resource_kind", "identifier", "integrity_digest"}
    if set(obj) - allowed or "resource_kind" not in obj or "identifier" not in obj:
        raise ValueError("RESOURCE_REF_INVALID")
    return CanonicalResourceRefV1.model_validate(obj)


def _normalize_network_target(value: Any) -> CanonicalNetworkTargetV1:
    obj = _require_object(value, "NETWORK_TARGET_INVALID")
    allowed = {"scheme", "host", "port", "path"}
    if set(obj) - allowed or "scheme" not in obj or "host" not in obj:
        raise ValueError("NETWORK_TARGET_INVALID")
    host = _require_nonempty_str(obj["host"], "NETWORK_TARGET_INVALID", 253)
    if any(ch in host for ch in "/:@ \t"):
        raise ValueError("NETWORK_TARGET_INVALID")
    return CanonicalNetworkTargetV1.model_validate({**obj, "host": host.lower()})


def _normalize_absolute_uri(value: Any) -> str:
    text = _require_nonempty_str(value, "ABSOLUTE_URI_INVALID", 4096)
    if not _URI_SCHEME_RE.match(text) or _has_control_or_whitespace(text):
        raise ValueError("ABSOLUTE_URI_INVALID")
    return text


def _normalize_workspace_path(value: Any) -> str:
    text = _require_nonempty_str(value, "WORKSPACE_PATH_INVALID", 4096)
    if (
        text.startswith("/")
        or text.startswith("\\")
        or re.match(r"^[A-Za-z]:", text)
        or "\\" in text
        or _has_control_or_whitespace(text)
        or any(segment == ".." for segment in text.split("/"))
    ):
        raise ValueError("WORKSPACE_PATH_INVALID")
    return text


def _normalize_recipient_set(value: Any) -> tuple[CanonicalRecipientV1, ...]:
    items = _require_list(value, "RECIPIENT_SET_INVALID", _RECIPIENT_SET_MAX)
    recipients: list[CanonicalRecipientV1] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        obj = _require_object(item, "RECIPIENT_SET_INVALID")
        if set(obj) != {"recipient_kind", "identifier"}:
            raise ValueError("RECIPIENT_SET_INVALID")
        recipient = CanonicalRecipientV1.model_validate(obj)
        key = (recipient.recipient_kind, recipient.identifier)
        if key not in seen:
            seen.add(key)
            recipients.append(recipient)
    return tuple(sorted(recipients, key=lambda r: (r.recipient_kind, r.identifier)))


def _normalize_ordered_sequence(value: Any) -> tuple[JsonValue, ...]:
    items = _require_list(value, "ORDERED_SEQUENCE_INVALID", _ORDERED_SEQUENCE_MAX)
    return tuple(items)


def _normalize_identifier_set(value: Any) -> tuple[str, ...]:
    items = _require_list(value, "IDENTIFIER_SET_INVALID", _IDENTIFIER_SET_MAX)
    identifiers: set[str] = set()
    for item in items:
        identifiers.add(_require_nonempty_str(item, "IDENTIFIER_SET_INVALID", 128))
    return tuple(sorted(identifiers))


def _normalize_utc_timestamp(value: Any) -> str:
    text = _require_nonempty_str(value, "UTC_TIMESTAMP_INVALID", 64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("UTC_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("UTC_TIMESTAMP_INVALID")
    return parsed.astimezone(UTC).replace(tzinfo=None).isoformat() + "Z"


def _normalize_byte_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("BYTE_COUNT_INVALID")
    count = cast(int, value)
    if count < 0:
        raise ValueError("BYTE_COUNT_INVALID")
    return count


NORMALIZERS: dict[str, Callable[[Any], Any]] = {
    "opaque_string_v1": _normalize_opaque_string,
    "resource_ref_v1": _normalize_resource_ref,
    "network_target_v1": _normalize_network_target,
    "absolute_uri_v1": _normalize_absolute_uri,
    "workspace_path_v1": _normalize_workspace_path,
    "recipient_set_v1": _normalize_recipient_set,
    "ordered_sequence_v1": _normalize_ordered_sequence,
    "identifier_set_v1": _normalize_identifier_set,
    "utc_timestamp_v1": _normalize_utc_timestamp,
    "byte_count_v1": _normalize_byte_count,
}

NORMALIZER_IDS: frozenset[str] = frozenset(NORMALIZERS)


def normalize(normalizer_id: str, value: Any) -> Any:
    """按冻结 Registry 选择 Normalizer；未知 ID 一律失败关闭。"""

    normalizer = NORMALIZERS.get(normalizer_id)
    if normalizer is None:
        raise ValueError("NORMALIZER_NOT_REGISTERED")
    return normalizer(value)
