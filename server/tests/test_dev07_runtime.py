"""DEV-07 进程级运行时装配的离线契约：`compose_runtime`把API/Worker/两个Outbox消费者接线。

不依赖运行中的Temporal/PostgreSQL：以纯对象假Client（不发起RPC）验证装配图——
- API、两个Activity与两个Outbox消费者共享同一个`session_factory`；
- 一个`TemporalClientAdapter`同时充当TEMPORAL_START启动器与TEMPORAL_SIGNAL投递器；
- 两个`OutboxPoller`分别驱动`OutboxPublisher`与`OutboxSignalPublisher`。
"""

from __future__ import annotations

import asyncio
import os
import unittest
from uuid import uuid4

from fastapi import FastAPI
from temporalio.converter import DataConverter

from knowledge_system.api.settings import SecretRef, Settings
from knowledge_system.modules.temporal.application.outbox_publisher import OutboxPublisher
from knowledge_system.modules.temporal.application.outbox_signal_publisher import (
    OutboxSignalPublisher,
)
from knowledge_system.modules.temporal.application.worker import OutboxPoller
from knowledge_system.modules.temporal.domain.contracts import TaskRefV1
from knowledge_system.modules.temporal.payload import ENCRYPTED_ENCODING
from knowledge_system.runtime import build_dev_data_converter, compose_runtime

os.environ.setdefault("TEST_JWT_SIGNING_SECRET", "j" * 64)
os.environ.setdefault("TEST_IDEMPOTENCY_HMAC_SECRET", "h" * 64)


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://localhost:5432/knowledge_test",
        jwt_signing_secret_ref=SecretRef(kind="env", reference="TEST_JWT_SIGNING_SECRET"),
        idempotency_hmac_secret_ref=SecretRef(kind="env", reference="TEST_IDEMPOTENCY_HMAC_SECRET"),
    )


class ComposeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        # 假Client仅被装配存储，不发起任何RPC；`object()`足以承载接线契约。
        self.fake_client = object()
        self.dependencies = compose_runtime(_settings(), self.fake_client)  # type: ignore[arg-type]

    def tearDown(self) -> None:
        asyncio.run(self.dependencies.engine.dispose())

    def test_compose_shares_single_session_factory_across_api_and_activities(self) -> None:
        deps = self.dependencies
        self.assertIsInstance(deps.app, FastAPI)
        self.assertIs(deps.app.state.runtime.session_factory, deps.session_factory)
        self.assertIs(
            deps.worker_factory._control_activities._session_factory, deps.session_factory
        )
        self.assertIs(
            deps.worker_factory._attempt_activities._session_factory, deps.session_factory
        )

    def test_compose_wires_start_and_signal_pollers_to_distinct_publishers(self) -> None:
        deps = self.dependencies
        self.assertIsInstance(deps.start_poller, OutboxPoller)
        self.assertIsInstance(deps.signal_poller, OutboxPoller)
        self.assertIsInstance(deps.start_poller._publisher, OutboxPublisher)
        self.assertIsInstance(deps.signal_poller._publisher, OutboxSignalPublisher)

    def test_compose_uses_one_adapter_for_both_destinations(self) -> None:
        deps = self.dependencies
        start_publisher = deps.start_poller._publisher
        signal_publisher = deps.signal_poller._publisher
        self.assertIs(start_publisher._starter, signal_publisher._signaller)
        self.assertIs(start_publisher._starter._client, self.fake_client)
        self.assertIs(deps.worker_factory._client, self.fake_client)


class BuildDevDataConverterTests(unittest.IsolatedAsyncioTestCase):
    """`build_dev_data_converter`离线契约：装配出的Converter能加解密Pydantic契约。

    `serve`把该Converter交给`Client.connect`，Worker与Adapter自动继承——这里只验证
    转换器本身确实带AES-256-GCM加密语义（编码产物为`binary/encrypted`而非`json/plain`），
    不需启动Temporal dev server。
    """

    async def test_converter_encrypts_and_round_trips_pydantic_contract(self) -> None:
        converter = build_dev_data_converter()
        self.assertIsInstance(converter, DataConverter)
        value = TaskRefV1(task_id=uuid4())
        (encoded,) = await converter.encode([value])
        self.assertEqual(encoded.metadata["encoding"], ENCRYPTED_ENCODING)
        (decoded,) = await converter.decode([encoded], [TaskRefV1])
        self.assertEqual(decoded, value)


if __name__ == "__main__":
    unittest.main()
