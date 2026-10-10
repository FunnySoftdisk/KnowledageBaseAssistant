"""进程级运行时装配：把API、Temporal客户端/Worker与两个Outbox轮询器组合为可启动图。

装配（`compose_runtime`）与启动（`serve`）分离：装配只做纯对象接线（不连真实
Temporal/PostgreSQL，可离线测试），启动才连接Temporal并进入事件循环（需真实基础设施）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.converter import DataConverter

from knowledge_system.api.app import create_app
from knowledge_system.api.settings import Settings
from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    SqlAlchemyOutboxRepository,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.modules.temporal.application.outbox_publisher import OutboxPublisher
from knowledge_system.modules.temporal.application.outbox_signal_publisher import (
    OutboxSignalPublisher,
)
from knowledge_system.modules.temporal.application.temporal_client import TemporalClientAdapter
from knowledge_system.modules.temporal.application.worker import (
    OutboxPoller,
    TemporalWorkerFactory,
)
from knowledge_system.modules.temporal.domain.topology import (
    CONTROL_TASK_QUEUE,
    WORKFLOW_TASK_QUEUE,
)
from knowledge_system.modules.temporal.payload import (
    EncryptionPayloadCodec,
    InMemoryPayloadBundleStore,
    InMemoryPayloadKeyProvider,
    build_data_converter,
)
from knowledge_system.modules.temporal.workflows.attempt_activities import AttemptActivities
from knowledge_system.modules.temporal.workflows.control_activities import ControlActivities

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    """`compose_runtime`装配出的进程级共享依赖图。"""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    app: FastAPI
    worker_factory: TemporalWorkerFactory
    start_poller: OutboxPoller
    signal_poller: OutboxPoller


def compose_runtime(
    settings: Settings,
    temporal_client: Client,
    *,
    workflow_task_queue: str = WORKFLOW_TASK_QUEUE,
    control_task_queue: str = CONTROL_TASK_QUEUE,
    build_id: str | None = None,
) -> RuntimeDependencies:
    """纯对象接线：一个引擎/会话工厂被API、两个Activity与两个Outbox消费者共享。

    - 同一`TemporalClientAdapter`同时充当`TEMPORAL_START`启动器与`TEMPORAL_SIGNAL`投递器；
    - `OutboxPoller`驱动`OutboxPublisher`（START）与`OutboxSignalPublisher`（SIGNAL）；
    - `ControlActivities`/`AttemptActivities`与`create_app`注入同一`session_factory`。
    """

    engine = build_async_engine(DatabaseEngineSettings(url=settings.database_url))
    session_factory = build_session_factory(engine)
    adapter = TemporalClientAdapter(temporal_client, workflow_task_queue=workflow_task_queue)
    repository = SqlAlchemyOutboxRepository(session_factory)
    start_publisher = OutboxPublisher(repository, adapter)
    signal_publisher = OutboxSignalPublisher(repository, adapter)
    control_activities = ControlActivities(session_factory)
    attempt_activities = AttemptActivities(session_factory)
    worker_factory = TemporalWorkerFactory(
        temporal_client,
        control_activities,
        attempt_activities,
        workflow_task_queue=workflow_task_queue,
        control_task_queue=control_task_queue,
        build_id=build_id,
    )
    start_poller = OutboxPoller(start_publisher)
    signal_poller = OutboxPoller(signal_publisher)
    app = create_app(settings, engine=engine, session_factory=session_factory)
    return RuntimeDependencies(
        engine=engine,
        session_factory=session_factory,
        app=app,
        worker_factory=worker_factory,
        start_poller=start_poller,
        signal_poller=signal_poller,
    )


def build_dev_data_converter() -> DataConverter:
    """为本地dev server装配加密`DataConverter`（一次性内存密钥 + 内存外置Store）。

    `Client.connect`以`data_converter`入参接收，Worker与Adapter自动继承Client的Converter，
    因此整条链路（启动/信号/查询/重放）共用同一套AES-256-GCM语义。密钥与Store仅存于进程
    内存，进程重启即失效；真实密钥提供（OpenBao/Broker）随Secret接线落地后替换本函数。
    """

    codec = EncryptionPayloadCodec(
        InMemoryPayloadKeyProvider(),
        InMemoryPayloadBundleStore(),
    )
    return build_data_converter(codec)


async def serve(settings: Settings) -> None:
    """真实进程入口：连接Temporal→装配→并行运行API(uvicorn)、两个Worker与两个轮询器。

    uvicorn自装SIGINT/SIGTERM处理器并置`should_exit`，其`serve()`任务最先完成，随后
    `FIRST_COMPLETED`返回、其余任务被取消：Worker与轮询器随进程优雅停机。
    """

    client = await Client.connect(
        settings.temporal_target,
        namespace=settings.temporal_namespace,
        data_converter=build_dev_data_converter(),
    )
    dependencies = compose_runtime(settings, client)
    workers = dependencies.worker_factory.build()
    server = uvicorn.Server(
        uvicorn.Config(
            dependencies.app,
            host=settings.api_host,
            port=settings.api_port,
            log_level="info",
        )
    )
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(server.serve()),
        *[asyncio.create_task(worker.run()) for worker in workers],
        asyncio.create_task(dependencies.start_poller.run()),
        asyncio.create_task(dependencies.signal_poller.run()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    """`python -m knowledge_system.runtime`入口。"""

    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(Settings.from_env()))


if __name__ == "__main__":
    main()
