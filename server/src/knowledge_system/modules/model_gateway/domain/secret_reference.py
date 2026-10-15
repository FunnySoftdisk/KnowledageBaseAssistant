"""Provider SecretRef的确定性、不可逆路径派生。

路径只暴露HMAC标识，不包含Workspace、邮箱、姓名、API Key或原始业务UUID。HMAC密钥属于
部署级标识密钥，不是Provider Key；调用方负责从受控Secret来源提供，并确保至少256 bit。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Final

_WORKSPACE_PATTERN: Final = re.compile(r"^ws-[a-z0-9]{1,64}$")
_CODE_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


class SecretReferenceViolation(ValueError):
    """稳定失败码；异常不携带原始标识或Secret。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def derive_provider_secret_path(
    *,
    identity_hmac_key: bytes,
    workspace_id: str,
    provider: str,
    region: str,
    purpose: str = "model-api-key",
) -> str:
    """生成`model-provider/<64 hex>` KV相对路径，不泄露输入标识。"""

    if len(identity_hmac_key) < 32:
        raise SecretReferenceViolation("SECRET_REF_HMAC_KEY_TOO_SHORT")
    if _WORKSPACE_PATTERN.fullmatch(workspace_id) is None:
        raise SecretReferenceViolation("SECRET_REF_WORKSPACE_INVALID")
    if any(_CODE_PATTERN.fullmatch(value) is None for value in (provider, region, purpose)):
        raise SecretReferenceViolation("SECRET_REF_CODE_INVALID")
    material = "\x00".join(("provider-secret-v1", provider, region, workspace_id, purpose)).encode()
    digest = hmac.new(identity_hmac_key, material, hashlib.sha256).hexdigest()
    return f"model-provider/{digest}"


__all__ = [
    "SecretReferenceViolation",
    "derive_provider_secret_path",
]
