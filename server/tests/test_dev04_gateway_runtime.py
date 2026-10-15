"""Offline process settings and TLS posture tests for the model gateway runtime."""

from __future__ import annotations

import ssl
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI

from knowledge_system.model_gateway_runtime import (
    ModelGatewayRole,
    ModelGatewayRuntimeSettings,
    ModelGatewayRuntimeSettingsError,
    _tls13_context,
    build_mtls_server_config,
)


def _common(directory: Path, role: str) -> dict[str, str]:
    return {
        "KNOWLEDGE_MODEL_GATEWAY_ROLE": role,
        "KNOWLEDGE_MODEL_GATEWAY_TLS_CERT_FILE": str(directory / "tls.crt"),
        "KNOWLEDGE_MODEL_GATEWAY_TLS_KEY_FILE": str(directory / "tls.key"),
        "KNOWLEDGE_MODEL_GATEWAY_CLIENT_CA_FILE": str(directory / "ca.crt"),
    }


class ModelGatewayRuntimeTests(unittest.TestCase):
    def test_role_specific_environment_does_not_accept_provider_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            admission_env = _common(directory, "admission") | {
                "KNOWLEDGE_DATABASE_URL": "postgresql+psycopg://app@postgres/knowledge"
            }
            settings = ModelGatewayRuntimeSettings.from_env(admission_env)
        self.assertEqual(settings.role, ModelGatewayRole.ADMISSION)
        self.assertFalse(hasattr(settings, "provider_api_key"))

    def test_broker_requires_only_secret_references_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            broker_env = _common(directory, "broker") | {
                "KNOWLEDGE_DATABASE_URL": "postgresql+psycopg://app@postgres/knowledge",
                "KNOWLEDGE_OPENBAO_AGENT_TOKEN_FILE": str(directory / "bao-token"),
                "KNOWLEDGE_OPENBAO_CA_FILE": str(directory / "openbao-ca.crt"),
                "KNOWLEDGE_OPENBAO_CLIENT_CERT_FILE": str(directory / "broker.crt"),
                "KNOWLEDGE_OPENBAO_CLIENT_KEY_FILE": str(directory / "broker.key"),
            }
            settings = ModelGatewayRuntimeSettings.from_env(broker_env)
        self.assertEqual(settings.openbao_base_url, "https://openbao:8200")
        self.assertEqual(settings.openbao_mount, "knowledge-model")

    def test_bridge_pins_internal_service_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            bridge_env = _common(directory, "bridge") | {
                "KNOWLEDGE_BAILIAN_BASE_URL": (
                    "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
                ),
                "KNOWLEDGE_BRIDGE_CLIENT_CA_FILE": str(directory / "ca.crt"),
                "KNOWLEDGE_BRIDGE_CLIENT_CERT_FILE": str(directory / "bridge.crt"),
                "KNOWLEDGE_BRIDGE_CLIENT_KEY_FILE": str(directory / "bridge.key"),
            }
            settings = ModelGatewayRuntimeSettings.from_env(bridge_env)
        self.assertEqual(settings.admission_base_url, "https://model-gateway-control:8443")
        self.assertEqual(settings.broker_base_url, "https://secret-broker:8443")

    def test_invalid_or_incomplete_role_configuration_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            ModelGatewayRuntimeSettingsError,
            "MODEL_GATEWAY_ENV_VALUE_INVALID",
        ):
            ModelGatewayRuntimeSettings.from_env(
                {
                    "KNOWLEDGE_MODEL_GATEWAY_ROLE": "unknown",
                    "KNOWLEDGE_MODEL_GATEWAY_TLS_CERT_FILE": "cert",
                    "KNOWLEDGE_MODEL_GATEWAY_TLS_KEY_FILE": "key",
                    "KNOWLEDGE_MODEL_GATEWAY_CLIENT_CA_FILE": "ca",
                }
            )

    def test_server_config_requires_client_certificates_and_tls13(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            for name in ("tls.crt", "tls.key", "ca.crt"):
                (directory / name).touch()
            settings = ModelGatewayRuntimeSettings.from_env(
                _common(directory, "admission")
                | {"KNOWLEDGE_DATABASE_URL": "postgresql+psycopg://app@postgres/knowledge"}
            )
            config = build_mtls_server_config(FastAPI(), settings)
        self.assertEqual(config.ssl_cert_reqs, ssl.CERT_REQUIRED)
        self.assertFalse(config.proxy_headers)
        self.assertFalse(config.server_header)

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        result = _tls13_context(config, lambda: context)
        self.assertIs(result, context)
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_3)
        self.assertEqual(context.maximum_version, ssl.TLSVersion.TLSv1_3)


if __name__ == "__main__":
    unittest.main()
