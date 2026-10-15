"""Database URL file ingress tests for the model-gateway runtime."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from knowledge_system.model_gateway_runtime import (
    ModelGatewayRuntimeSettings,
    ModelGatewayRuntimeSettingsError,
)


def _environment(directory: Path, url_file: Path) -> dict[str, str]:
    return {
        "KNOWLEDGE_MODEL_GATEWAY_ROLE": "admission",
        "KNOWLEDGE_MODEL_GATEWAY_TLS_CERT_FILE": str(directory / "tls.crt"),
        "KNOWLEDGE_MODEL_GATEWAY_TLS_KEY_FILE": str(directory / "tls.key"),
        "KNOWLEDGE_MODEL_GATEWAY_CLIENT_CA_FILE": str(directory / "ca.crt"),
        "KNOWLEDGE_DATABASE_URL_FILE": str(url_file),
    }


class GatewayRuntimeSecretFileTests(unittest.TestCase):
    def test_reads_database_url_only_from_a_mode_0600_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            url_file = directory / "database-url"
            url_file.write_text(
                "postgresql+psycopg://gateway:synthetic@postgres/knowledge\n",
                encoding="utf-8",
            )
            url_file.chmod(0o600)
            settings = ModelGatewayRuntimeSettings.from_env(
                _environment(directory, url_file)
            )
        self.assertEqual(
            settings.database_url,
            "postgresql+psycopg://gateway:synthetic@postgres/knowledge",
        )

    def test_rejects_ambiguous_or_over_permissive_database_url_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            url_file = directory / "database-url"
            url_file.write_text(
                "postgresql+psycopg://gateway:synthetic@postgres/knowledge",
                encoding="utf-8",
            )
            url_file.chmod(0o644)
            with self.assertRaisesRegex(
                ModelGatewayRuntimeSettingsError,
                "MODEL_GATEWAY_DATABASE_URL_FILE_INVALID",
            ):
                ModelGatewayRuntimeSettings.from_env(_environment(directory, url_file))

            url_file.chmod(0o600)
            ambiguous = _environment(directory, url_file) | {
                "KNOWLEDGE_DATABASE_URL": (
                    "postgresql+psycopg://gateway:synthetic@postgres/knowledge"
                )
            }
            with self.assertRaisesRegex(
                ModelGatewayRuntimeSettingsError,
                "MODEL_GATEWAY_DATABASE_URL_SOURCE_INVALID",
            ):
                ModelGatewayRuntimeSettings.from_env(ambiguous)


if __name__ == "__main__":
    unittest.main()
