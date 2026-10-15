"""OpenBao KV v2 Provider Key适配器。

客户端固定HTTPS、禁重定向、`trust_env=False`，Token按调用取得；对外错误只暴露稳定码，避免把
响应正文、Token或Provider Key带入异常和日志。操作者写入使用CAS，Broker读取要求准确版本。
"""

from __future__ import annotations

import os
import re
import ssl
import stat
from pathlib import Path
from typing import Protocol

import httpx

from knowledge_system.modules.model_gateway.application.secrets import (
    ProviderSecretReadAuthorizer,
    ProviderSecretReadRequestV1,
    SecretWriteReceiptV1,
)

_SECRET_PATH = re.compile(r"^model-provider/[0-9a-f]{64}$")
_SECRET_REF = re.compile(
    r"^openbao-kv-v2://(?P<mount>[a-z][a-z0-9_-]{1,63})/"
    r"(?P<path>model-provider/[0-9a-f]{64})$"
)


class OpenBaoSecretError(RuntimeError):
    """稳定失败码；不得附带上游响应或Secret。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OpenBaoTokenProvider(Protocol):
    async def token(self) -> bytearray: ...


class OpenBaoAgentFileTokenProvider:
    """Read the short-lived Agent sink token on demand without caching it."""

    def __init__(self, token_file: Path) -> None:
        self._token_file = token_file

    async def token(self) -> bytearray:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self._token_file, flags)
        except OSError:
            raise OpenBaoSecretError("OPENBAO_AGENT_TOKEN_UNAVAILABLE") from None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise OpenBaoSecretError("OPENBAO_AGENT_TOKEN_PERMISSIONS_INVALID")
            token = bytearray(os.read(descriptor, 513))
        finally:
            os.close(descriptor)
        if len(token) > 512:
            _zeroize(token)
            raise OpenBaoSecretError("OPENBAO_AGENT_TOKEN_INVALID")
        while token and token[-1] in (10, 13):
            token.pop()
        if not token or any(value < 33 or value == 127 for value in token):
            _zeroize(token)
            raise OpenBaoSecretError("OPENBAO_AGENT_TOKEN_INVALID")
        return token


class OpenBaoKv2Client:
    """最小KV v2读写客户端；不缓存Token或Provider Key。"""

    def __init__(
        self,
        *,
        base_url: str,
        mount: str,
        token_provider: OpenBaoTokenProvider,
        transport: httpx.AsyncBaseTransport | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        parsed = httpx.URL(base_url)
        if parsed.scheme != "https" or parsed.query or parsed.fragment:
            raise OpenBaoSecretError("OPENBAO_BASE_URL_INVALID")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", mount):
            raise OpenBaoSecretError("OPENBAO_MOUNT_INVALID")
        self._base_url = str(parsed).rstrip("/")
        self._mount = mount
        self._token_provider = token_provider
        self._transport = transport
        if cert is not None:
            if not isinstance(verify, str):
                raise OpenBaoSecretError("OPENBAO_MTLS_CA_FILE_REQUIRED")
            try:
                context = ssl.create_default_context(cafile=verify)
                context.minimum_version = ssl.TLSVersion.TLSv1_3
                context.load_cert_chain(*cert)
            except (OSError, ssl.SSLError, ValueError):
                raise OpenBaoSecretError("OPENBAO_MTLS_MATERIAL_INVALID") from None
            self._verify: bool | str | ssl.SSLContext = context
        else:
            self._verify = verify
        self._timeout_seconds = timeout_seconds

    async def write_provider_key(
        self,
        *,
        secret_path: str,
        api_key: bytearray,
        expected_version: int,
    ) -> SecretWriteReceiptV1:
        self._validate_path(secret_path)
        if expected_version < 0:
            raise OpenBaoSecretError("OPENBAO_CAS_VERSION_INVALID")
        self._validate_key(api_key)
        token = await self._token_provider.token()
        try:
            response = await self._request(
                "POST",
                secret_path,
                token,
                json_body={
                    "options": {"cas": expected_version},
                    "data": {"api_key": api_key.decode("utf-8")},
                },
            )
            if response.status_code != 200:
                raise OpenBaoSecretError(self._write_error_code(response.status_code))
            try:
                version = response.json()["data"]["version"]
            except (ValueError, KeyError, TypeError):
                raise OpenBaoSecretError("OPENBAO_WRITE_RESPONSE_INVALID") from None
            if type(version) is not int or version < 1:
                raise OpenBaoSecretError("OPENBAO_WRITE_RESPONSE_INVALID")
            return SecretWriteReceiptV1(
                secret_ref=f"openbao-kv-v2://{self._mount}/{secret_path}",
                version=version,
            )
        finally:
            _zeroize(token)

    async def read_current_version(self, *, secret_path: str) -> int:
        """读取KV v2元数据的`current_version`；只返回版本号，不触碰Provider Key正文。

        供出网许可签发方在基础设施层解析`secret_version`。元数据端点只暴露版本号与历史版本
        元信息，不含`data`正文，因此该读取不构成「读取Provider Key」。
        """
        self._validate_path(secret_path)
        token = await self._token_provider.token()
        try:
            response = await self._request("GET", secret_path, token, segment="metadata")
            if response.status_code != 200:
                raise OpenBaoSecretError(self._metadata_error_code(response.status_code))
            try:
                current_version = response.json()["data"]["current_version"]
            except (ValueError, KeyError, TypeError):
                raise OpenBaoSecretError("OPENBAO_METADATA_RESPONSE_INVALID") from None
            if type(current_version) is not int or current_version < 1:
                raise OpenBaoSecretError("OPENBAO_METADATA_RESPONSE_INVALID")
            return current_version
        finally:
            _zeroize(token)

    async def read_provider_key(self, *, secret_path: str, version: int) -> bytearray:
        self._validate_path(secret_path)
        if version < 1:
            raise OpenBaoSecretError("OPENBAO_SECRET_VERSION_INVALID")
        token = await self._token_provider.token()
        try:
            response = await self._request(
                "GET",
                secret_path,
                token,
                params={"version": str(version)},
            )
            if response.status_code != 200:
                raise OpenBaoSecretError(self._read_error_code(response.status_code))
            try:
                data = response.json()["data"]
                actual_version = data["metadata"]["version"]
                value = data["data"]["api_key"]
            except (ValueError, KeyError, TypeError):
                raise OpenBaoSecretError("OPENBAO_READ_RESPONSE_INVALID") from None
            if actual_version != version or not isinstance(value, str):
                raise OpenBaoSecretError("OPENBAO_READ_RESPONSE_INVALID")
            encoded = bytearray(value, "utf-8")
            self._validate_key(encoded)
            return encoded
        finally:
            _zeroize(token)

    async def _request(
        self,
        method: str,
        secret_path: str,
        token: bytearray,
        json_body: dict[str, object] | None = None,
        params: dict[str, str] | None = None,
        segment: str = "data",
    ) -> httpx.Response:
        if not token:
            raise OpenBaoSecretError("OPENBAO_TOKEN_EMPTY")
        try:
            token_text = token.decode("utf-8")
        except UnicodeDecodeError:
            raise OpenBaoSecretError("OPENBAO_TOKEN_ENCODING_INVALID") from None
        url = f"{self._base_url}/v1/{self._mount}/{segment}/{secret_path}"
        try:
            async with httpx.AsyncClient(
                verify=self._verify,
                transport=self._transport,
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                return await client.request(
                    method,
                    url,
                    headers={"X-Vault-Token": token_text, "Content-Type": "application/json"},
                    json=json_body,
                    params=params,
                )
        except httpx.HTTPError:
            raise OpenBaoSecretError("OPENBAO_REQUEST_FAILED") from None

    @staticmethod
    def _validate_path(secret_path: str) -> None:
        if _SECRET_PATH.fullmatch(secret_path) is None:
            raise OpenBaoSecretError("OPENBAO_SECRET_PATH_INVALID")

    @staticmethod
    def _validate_key(api_key: bytearray) -> None:
        if not isinstance(api_key, bytearray) or not 16 <= len(api_key) <= 512:
            raise OpenBaoSecretError("PROVIDER_API_KEY_INVALID")
        try:
            text = api_key.decode("utf-8")
        except UnicodeDecodeError:
            raise OpenBaoSecretError("PROVIDER_API_KEY_INVALID") from None
        if text.strip() != text or any(ord(char) < 33 or ord(char) == 127 for char in text):
            raise OpenBaoSecretError("PROVIDER_API_KEY_INVALID")

    @staticmethod
    def _write_error_code(status_code: int) -> str:
        if status_code in {401, 403}:
            return "OPENBAO_WRITE_FORBIDDEN"
        if status_code == 404:
            return "OPENBAO_MOUNT_NOT_FOUND"
        if status_code in {400, 409}:
            return "OPENBAO_CAS_CONFLICT"
        return "OPENBAO_WRITE_FAILED"

    @staticmethod
    def _read_error_code(status_code: int) -> str:
        if status_code in {401, 403}:
            return "OPENBAO_READ_FORBIDDEN"
        if status_code == 404:
            return "OPENBAO_SECRET_NOT_FOUND"
        return "OPENBAO_READ_FAILED"

    @staticmethod
    def _metadata_error_code(status_code: int) -> str:
        if status_code in {401, 403}:
            return "OPENBAO_METADATA_FORBIDDEN"
        if status_code == 404:
            return "OPENBAO_SECRET_NOT_FOUND"
        return "OPENBAO_METADATA_FAILED"


class AuthorizedOpenBaoSecretBroker:
    """Authorize every read, then resolve one exact KV v2 SecretRef version."""

    def __init__(
        self,
        *,
        reader: OpenBaoKv2Client,
        authorizer: ProviderSecretReadAuthorizer,
        expected_mount: str = "knowledge-model",
    ) -> None:
        self._reader = reader
        self._authorizer = authorizer
        self._expected_mount = expected_mount

    async def read_provider_key(self, request: ProviderSecretReadRequestV1) -> bytearray:
        if not await self._authorizer.authorize(request):
            raise OpenBaoSecretError("PROVIDER_SECRET_READ_NOT_AUTHORIZED")
        match = _SECRET_REF.fullmatch(request.secret_ref)
        if match is None:
            raise OpenBaoSecretError("OPENBAO_SECRET_REF_INVALID")
        if match.group("mount") != self._expected_mount:
            raise OpenBaoSecretError("OPENBAO_SECRET_REF_MOUNT_MISMATCH")
        return await self._reader.read_provider_key(
            secret_path=match.group("path"),
            version=request.secret_version,
        )


def _zeroize(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


__all__ = [
    "AuthorizedOpenBaoSecretBroker",
    "OpenBaoAgentFileTokenProvider",
    "OpenBaoKv2Client",
    "OpenBaoSecretError",
    "OpenBaoTokenProvider",
]
