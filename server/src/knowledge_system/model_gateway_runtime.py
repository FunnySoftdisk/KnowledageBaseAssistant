"""Runnable mTLS processes for model-egress admission, secret brokering and bridging."""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from knowledge_system.infrastructure.model_gateway.apps import (
    create_admission_app,
    create_bridge_app,
    create_secret_broker_app,
)
from knowledge_system.infrastructure.model_gateway.bridge import BailianEgressBridge
from knowledge_system.infrastructure.model_gateway.internal_http import (
    HttpModelEgressAdmission,
    HttpProviderSecretBroker,
    build_workload_mtls_context,
)
from knowledge_system.infrastructure.model_gateway.openbao import (
    AuthorizedOpenBaoSecretBroker,
    OpenBaoAgentFileTokenProvider,
    OpenBaoKv2Client,
)
from knowledge_system.infrastructure.persistence.egress_permit_repository import (
    PostgresEgressPermitAuthority,
)
from knowledge_system.infrastructure.persistence.engine import (
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)

logger = logging.getLogger(__name__)


class ModelGatewayRole(StrEnum):
    ADMISSION = "admission"
    BROKER = "broker"
    BRIDGE = "bridge"


class ModelGatewayRuntimeSettingsError(RuntimeError):
    """Stable configuration failure without including configuration values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ModelGatewayRuntimeSettings:
    role: ModelGatewayRole
    host: str
    port: int
    server_cert_file: Path
    server_key_file: Path
    client_ca_file: Path
    database_url: str | None = None
    openbao_base_url: str | None = None
    openbao_mount: str = "knowledge-model"
    openbao_agent_token_file: Path | None = None
    openbao_ca_file: Path | None = None
    openbao_client_cert_file: Path | None = None
    openbao_client_key_file: Path | None = None
    bailian_base_url: str | None = None
    admission_base_url: str | None = None
    broker_base_url: str | None = None
    bridge_client_ca_file: Path | None = None
    bridge_client_cert_file: Path | None = None
    bridge_client_key_file: Path | None = None

    def __post_init__(self) -> None:
        if not self.host or not 1 <= self.port <= 65_535:
            raise ModelGatewayRuntimeSettingsError("MODEL_GATEWAY_LISTEN_ADDRESS_INVALID")
        if self.role in {ModelGatewayRole.ADMISSION, ModelGatewayRole.BROKER}:
            if not self.database_url:
                raise ModelGatewayRuntimeSettingsError("MODEL_GATEWAY_DATABASE_URL_REQUIRED")
        if self.role is ModelGatewayRole.BROKER and any(
            value is None
            for value in (
                self.openbao_base_url,
                self.openbao_agent_token_file,
                self.openbao_ca_file,
                self.openbao_client_cert_file,
                self.openbao_client_key_file,
            )
        ):
            raise ModelGatewayRuntimeSettingsError("MODEL_GATEWAY_OPENBAO_SETTINGS_REQUIRED")
        if self.role is ModelGatewayRole.BRIDGE and any(
            value is None
            for value in (
                self.bailian_base_url,
                self.admission_base_url,
                self.broker_base_url,
                self.bridge_client_ca_file,
                self.bridge_client_cert_file,
                self.bridge_client_key_file,
            )
        ):
            raise ModelGatewayRuntimeSettingsError("MODEL_GATEWAY_BRIDGE_SETTINGS_REQUIRED")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> ModelGatewayRuntimeSettings:
        source = os.environ if env is None else env

        def required(name: str) -> str:
            value = source.get(name, "").strip()
            if not value:
                raise ModelGatewayRuntimeSettingsError(f"{name}_REQUIRED")
            return value

        def database_url() -> str:
            direct = source.get("KNOWLEDGE_DATABASE_URL", "").strip()
            file_name = source.get("KNOWLEDGE_DATABASE_URL_FILE", "").strip()
            if bool(direct) == bool(file_name):
                raise ModelGatewayRuntimeSettingsError(
                    "MODEL_GATEWAY_DATABASE_URL_SOURCE_INVALID"
                )
            if direct:
                return direct
            return _read_secret_text_file(
                Path(file_name),
                "MODEL_GATEWAY_DATABASE_URL_FILE_INVALID",
            )

        try:
            role = ModelGatewayRole(required("KNOWLEDGE_MODEL_GATEWAY_ROLE"))
            port = int(source.get("KNOWLEDGE_MODEL_GATEWAY_PORT", "8443"))
        except ValueError:
            raise ModelGatewayRuntimeSettingsError("MODEL_GATEWAY_ENV_VALUE_INVALID") from None

        common: dict[str, object] = {
            "role": role,
            "host": source.get("KNOWLEDGE_MODEL_GATEWAY_HOST", "0.0.0.0"),
            "port": port,
            "server_cert_file": Path(required("KNOWLEDGE_MODEL_GATEWAY_TLS_CERT_FILE")),
            "server_key_file": Path(required("KNOWLEDGE_MODEL_GATEWAY_TLS_KEY_FILE")),
            "client_ca_file": Path(required("KNOWLEDGE_MODEL_GATEWAY_CLIENT_CA_FILE")),
        }
        if role is ModelGatewayRole.ADMISSION:
            common["database_url"] = database_url()
        elif role is ModelGatewayRole.BROKER:
            common.update(
                database_url=database_url(),
                openbao_base_url=source.get("KNOWLEDGE_OPENBAO_BASE_URL", "https://openbao:8200"),
                openbao_mount=source.get("KNOWLEDGE_OPENBAO_MOUNT", "knowledge-model"),
                openbao_agent_token_file=Path(
                    required("KNOWLEDGE_OPENBAO_AGENT_TOKEN_FILE")
                ),
                openbao_ca_file=Path(required("KNOWLEDGE_OPENBAO_CA_FILE")),
                openbao_client_cert_file=Path(
                    required("KNOWLEDGE_OPENBAO_CLIENT_CERT_FILE")
                ),
                openbao_client_key_file=Path(
                    required("KNOWLEDGE_OPENBAO_CLIENT_KEY_FILE")
                ),
            )
        else:
            common.update(
                bailian_base_url=required("KNOWLEDGE_BAILIAN_BASE_URL"),
                admission_base_url=source.get(
                    "KNOWLEDGE_ADMISSION_BASE_URL",
                    "https://model-gateway-control:8443",
                ),
                broker_base_url=source.get(
                    "KNOWLEDGE_SECRET_BROKER_BASE_URL",
                    "https://secret-broker:8443",
                ),
                bridge_client_ca_file=Path(
                    required("KNOWLEDGE_BRIDGE_CLIENT_CA_FILE")
                ),
                bridge_client_cert_file=Path(
                    required("KNOWLEDGE_BRIDGE_CLIENT_CERT_FILE")
                ),
                bridge_client_key_file=Path(
                    required("KNOWLEDGE_BRIDGE_CLIENT_KEY_FILE")
                ),
            )
        return cls(**common)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ModelGatewayRuntimeDependencies:
    app: FastAPI
    engine: AsyncEngine | None


def _required_path(value: Path | None, code: str) -> Path:
    if value is None or not value.is_file():
        raise ModelGatewayRuntimeSettingsError(code)
    return value


def _read_secret_text_file(path: Path, code: str) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ModelGatewayRuntimeSettingsError(code) from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ModelGatewayRuntimeSettingsError(code)
        value = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    if not 1 <= len(value) <= 4096:
        raise ModelGatewayRuntimeSettingsError(code)
    while value and value[-1] in (10, 13):
        value = value[:-1]
    if not value or any(byte < 33 or byte == 127 for byte in value):
        raise ModelGatewayRuntimeSettingsError(code)
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise ModelGatewayRuntimeSettingsError(code) from None


def compose_model_gateway_runtime(
    settings: ModelGatewayRuntimeSettings,
) -> ModelGatewayRuntimeDependencies:
    if settings.role is ModelGatewayRole.ADMISSION:
        if settings.database_url is None:
            raise ModelGatewayRuntimeSettingsError("MODEL_GATEWAY_DATABASE_URL_REQUIRED")
        engine = build_async_engine(DatabaseEngineSettings(url=settings.database_url))
        authority = PostgresEgressPermitAuthority(build_session_factory(engine))
        return ModelGatewayRuntimeDependencies(create_admission_app(authority), engine)

    if settings.role is ModelGatewayRole.BROKER:
        if settings.database_url is None or settings.openbao_base_url is None:
            raise ModelGatewayRuntimeSettingsError("MODEL_GATEWAY_BROKER_SETTINGS_REQUIRED")
        engine = build_async_engine(DatabaseEngineSettings(url=settings.database_url))
        authority = PostgresEgressPermitAuthority(build_session_factory(engine))
        token_file = _required_path(
            settings.openbao_agent_token_file,
            "MODEL_GATEWAY_OPENBAO_TOKEN_FILE_UNAVAILABLE",
        )
        ca_file = _required_path(
            settings.openbao_ca_file,
            "MODEL_GATEWAY_OPENBAO_CA_FILE_UNAVAILABLE",
        )
        cert_file = _required_path(
            settings.openbao_client_cert_file,
            "MODEL_GATEWAY_OPENBAO_CLIENT_CERT_UNAVAILABLE",
        )
        key_file = _required_path(
            settings.openbao_client_key_file,
            "MODEL_GATEWAY_OPENBAO_CLIENT_KEY_UNAVAILABLE",
        )
        reader = OpenBaoKv2Client(
            base_url=settings.openbao_base_url,
            mount=settings.openbao_mount,
            token_provider=OpenBaoAgentFileTokenProvider(token_file),
            verify=str(ca_file),
            cert=(str(cert_file), str(key_file)),
        )
        broker = AuthorizedOpenBaoSecretBroker(reader=reader, authorizer=authority)
        return ModelGatewayRuntimeDependencies(create_secret_broker_app(broker), engine)

    if settings.bailian_base_url is None:
        raise ModelGatewayRuntimeSettingsError("MODEL_GATEWAY_BAILIAN_BASE_URL_REQUIRED")
    ca_file = _required_path(
        settings.bridge_client_ca_file,
        "MODEL_GATEWAY_INTERNAL_CA_FILE_UNAVAILABLE",
    )
    cert_file = _required_path(
        settings.bridge_client_cert_file,
        "MODEL_GATEWAY_BRIDGE_CLIENT_CERT_UNAVAILABLE",
    )
    key_file = _required_path(
        settings.bridge_client_key_file,
        "MODEL_GATEWAY_BRIDGE_CLIENT_KEY_UNAVAILABLE",
    )
    context = build_workload_mtls_context(
        ca_file=ca_file,
        client_cert=cert_file,
        client_key=key_file,
    )
    admission = HttpModelEgressAdmission(
        base_url=settings.admission_base_url or "",
        ssl_context=context,
    )
    secret_broker = HttpProviderSecretBroker(
        base_url=settings.broker_base_url or "",
        ssl_context=context,
    )
    bridge = BailianEgressBridge(
        provider_base_url=settings.bailian_base_url,
        admission=admission,
        secret_broker=secret_broker,
    )
    return ModelGatewayRuntimeDependencies(create_bridge_app(bridge), None)


def _tls13_context(
    config: uvicorn.Config,
    default_factory: Callable[[], ssl.SSLContext],
) -> ssl.SSLContext:
    del config
    context = default_factory()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    return context


def build_mtls_server_config(
    app: FastAPI,
    settings: ModelGatewayRuntimeSettings,
) -> uvicorn.Config:
    cert_file = _required_path(
        settings.server_cert_file,
        "MODEL_GATEWAY_SERVER_CERT_UNAVAILABLE",
    )
    key_file = _required_path(
        settings.server_key_file,
        "MODEL_GATEWAY_SERVER_KEY_UNAVAILABLE",
    )
    ca_file = _required_path(
        settings.client_ca_file,
        "MODEL_GATEWAY_CLIENT_CA_UNAVAILABLE",
    )
    return uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=True,
        proxy_headers=False,
        server_header=False,
        date_header=False,
        limit_concurrency=64,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=15,
        ssl_certfile=str(cert_file),
        ssl_keyfile=str(key_file),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        ssl_ca_certs=str(ca_file),
        ssl_context_factory=_tls13_context,
    )


async def serve(settings: ModelGatewayRuntimeSettings) -> None:
    dependencies = compose_model_gateway_runtime(settings)
    server = uvicorn.Server(build_mtls_server_config(dependencies.app, settings))
    try:
        await server.serve()
    finally:
        if dependencies.engine is not None:
            await dependencies.engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(ModelGatewayRuntimeSettings.from_env()))


if __name__ == "__main__":
    main()
