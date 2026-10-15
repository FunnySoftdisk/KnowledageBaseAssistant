"""DEV-04C 预算预留与账本真实PostgreSQL验收：幂等预留、状态CAS与结算分账。

在隔离一次性数据库上以真实`BudgetLedgerRepository`验证：

- 预留按主键幂等：重复`reservation_id`返回既有行、不重复计入`RESERVE`分录；
- 状态转移按`from_status + expected_row_version`CAS：RESERVED→CONSUMING→SETTLED并落地
  `settled_at`与实耗列；冲突精确区分不存在/非预期状态/行版本陈旧；
- 账本分录按`entry_id`幂等只追加，结算分账每维预留=实耗+退还；
- 非法转移以稳定`BUDGET_RESERVATION_STATE_TRANSITION_INVALID`失败关闭；CHECK约束拒绝非法
  分录种类与超额实耗。
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.budget_repository import (
    BudgetLedgerRepository,
)
from knowledge_system.infrastructure.persistence.model_call_repository import (
    ModelCallLedgerRepository,
)
from knowledge_system.modules.model_gateway.application.budget import (
    BudgetReservationInsertDisposition,
    BudgetReservationTransitionConflict,
)
from knowledge_system.modules.model_gateway.domain.budget_ledger import (
    BudgetLedgerEntryKind,
    BudgetLedgerEntryV1,
    is_conserved,
    settle_deltas,
)
from knowledge_system.modules.model_gateway.domain.budget_quantity import (
    BudgetQuantityDeltaV1,
    BudgetQuantityV1,
)
from knowledge_system.modules.model_gateway.domain.budget_reservation import (
    BudgetReservationStateViolation,
    BudgetReservationStatus,
    BudgetReservationV1,
)
from knowledge_system.modules.model_gateway.domain.call_state import ModelCallState
from knowledge_system.modules.model_gateway.domain.model_call import DataScope, ModelCallV1
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    ExactTokenAccountingV1,
)
from knowledge_system.modules.model_gateway.domain.usage import UnknownModelUsageV1

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
SERVER_ROOT = Path(__file__).resolve().parents[1]

DIGEST = "a" * 64
RESERVED = BudgetQuantityV1(
    model_call_count=1,
    input_tokens=100,
    output_tokens=50,
    tool_count=0,
    cost_amount=Decimal("0.50"),
)
SETTLED = BudgetQuantityV1(
    model_call_count=1,
    input_tokens=80,
    output_tokens=40,
    tool_count=0,
    cost_amount=Decimal("0.40"),
)


def _pg_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    return url


def _isolated_url(base_url: str, dbname: str) -> str:
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def _alembic_config() -> Config:
    return Config(str(SERVER_ROOT / "alembic.ini"))


async def _seed(engine, ids: dict[str, UUID]) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO iam.organization (id,name,status,version) "
                "VALUES (:id,:name,'ACTIVE',1)"
            ),
            {"id": ids["org"], "name": f"test-{ids['org']}"},
        )
        username = f"u-{ids['user']}"
        await connection.execute(
            text(
                "INSERT INTO iam.user_account "
                "(id,organization_id,username,normalized_username,display_name,password_hash,"
                "status,must_change_password,failed_login_count,credential_version,version) "
                "VALUES (:id,:org,:username,:username,:username,'hash','ACTIVE',false,0,1,1)"
            ),
            {"id": ids["user"], "org": ids["org"], "username": username},
        )
        await connection.execute(
            text(
                "INSERT INTO content.artifact "
                "(artifact_id,organization_id,owner_id,purpose,artifact_type,storage_backend,"
                "object_key,size_bytes,sha256,detected_media_type,schema_id,schema_version,"
                "schema_digest,data_labels_json,encryption_profile,state,legal_hold,row_version,"
                "created_by_actor_id) VALUES "
                "(:id,:org,:owner,'QUERY','TEXT','S3',:object_key,1,:digest,'text/plain',"
                "'query','v1',:digest,'{}'::jsonb,'dev','AVAILABLE',false,1,:owner)"
            ),
            {
                "id": ids["artifact"],
                "org": ids["org"],
                "owner": ids["user"],
                "object_key": f"test/{ids['artifact']}",
                "digest": DIGEST,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.conversation "
                "(id,owner_id,organization_id,title,mode,conversation_memory_version,"
                "memory_policy) VALUES (:id,:owner,:org,'test','internal',0,'OFF')"
            ),
            {"id": ids["conversation"], "owner": ids["user"], "org": ids["org"]},
        )
        deadline = datetime.now(UTC) + timedelta(hours=1)
        await connection.execute(
            text(
                "INSERT INTO workflow.intelligent_task "
                "(id,organization_id,owner_id,conversation_id,query_artifact_id,"
                "initial_input_id,current_input_id,authorization_ref_id,status,"
                "temporal_workflow_id,last_event_sequence,budget,budget_used,deadline_at,version) "
                "VALUES (:id,:org,:owner,:conversation,:artifact,:input,:input,:auth,'PLANNING',"
                ":temporal,0,'{}'::jsonb,'{}'::jsonb,:deadline,1)"
            ),
            {
                "id": ids["task"],
                "org": ids["org"],
                "owner": ids["user"],
                "conversation": ids["conversation"],
                "artifact": ids["artifact"],
                "input": ids["input"],
                "auth": ids["agent"],
                "temporal": f"agent-task-v1-{ids['task']}",
                "deadline": deadline,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.conversation_message "
                "(id,conversation_id,role,message_kind,content_artifact_id,task_id,sequence,"
                "revision,content_hash,state) VALUES "
                "(:id,:conversation,'USER','USER_QUERY',:artifact,:task,1,1,:digest,'ACTIVE')"
            ),
            {
                "id": ids["message"],
                "conversation": ids["conversation"],
                "artifact": ids["artifact"],
                "task": ids["task"],
                "digest": DIGEST,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.task_input_snapshot "
                "(input_id,task_id,input_revision,message_id,message_artifact_id,actor_id,"
                "organization_id,conversation_id,attachment_binding_refs_json,"
                "knowledge_scope_json,network_policy,network_policy_source,output_contract_json,"
                "selected_resource_refs_json,task_constraint_refs_json,policy_snapshot_ref_json,"
                "locale,timezone,contract_version,snapshot_digest) VALUES "
                "(:input,:task,1,:message,:artifact,:owner,:org,:conversation,"
                "'[]'::jsonb,'{}'::jsonb,'DENY','SYSTEM_DEFAULT','{}'::jsonb,"
                "'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,'zh-CN','Asia/Shanghai',"
                "'task_input_snapshot_v1',:digest)"
            ),
            {
                "input": ids["input"],
                "task": ids["task"],
                "message": ids["message"],
                "artifact": ids["artifact"],
                "owner": ids["user"],
                "org": ids["org"],
                "conversation": ids["conversation"],
                "digest": DIGEST,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.agent_instance "
                "(id,task_id,parent_agent_instance_id,manifest_id,manifest_version,kind,"
                "delegated_scope,status,expires_at,created_reason,created_reason_hash,"
                "completed_at) VALUES "
                "(:id,:task,NULL,'supervisor','v1','SUPERVISOR','{}'::jsonb,'ACTIVE',NULL,"
                "'goal understanding',:digest,NULL)"
            ),
            {"id": ids["agent"], "task": ids["task"], "digest": DIGEST},
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.context_pack "
                "(id,task_id,action_attempt_id,created_by_attempt_id,conversation_id,actor_id,"
                "authorization_ref_id,authorization_version,state,context_policy_version,"
                "conversation_memory_version,user_private_memory_version,task_memory_version,"
                "runtime_version,plan_version,agent_manifest_version,model_revision,prompt_version,"
                "tool_schema_versions,output_schema_id,output_schema_version,tokenizer_revision,"
                "chat_template_hash,max_context,reserved_output,protocol_overhead,safety_margin,"
                "input_tokens,body_artifact_id,pack_hash,input_digest,consumed_at) VALUES "
                "(:id,:task,NULL,NULL,:conversation,:actor,NULL,NULL,'READY','context-policy-v1',"
                "0,0,0,'runtime-v1',NULL,'supervisor','model-1','prompt-v1','{}'::jsonb,"
                "NULL,NULL,'tok-1',:digest,8192,1024,512,256,100,:artifact,:digest,:digest,NULL)"
            ),
            {
                "id": ids["pack"],
                "task": ids["task"],
                "conversation": ids["conversation"],
                "actor": ids["user"],
                "artifact": ids["artifact"],
                "digest": DIGEST,
            },
        )


def _accepted_call(context_pack_id: UUID) -> ModelCallV1:
    return ModelCallV1(
        model_call_id=uuid4(),
        context_pack_id=context_pack_id,
        context_pack_hash="a" * 64,
        context_pack_input_digest="b" * 64,
        capability_alias="planning_strong",
        approved_model_id="qwen-plus",
        approved_model_revision="rev1",
        prompt_version="v1",
        context_policy_version="context-policy-v1",
        authorization_version=1,
        runtime_version="runtime-v1",
        agent_manifest_version="supervisor",
        conversation_memory_version=0,
        user_private_memory_version=0,
        task_memory_version=0,
        token_accounting=ExactTokenAccountingV1(
            mode="EXACT",
            estimated=False,
            exact_input_tokens=128,
            tokenizer_revision="tok-1",
            chat_template_hash=DIGEST,
        ),
        data_scope=DataScope.INTERNAL_PRODUCTION,
        request_hash="b" * 64,
        usage=UnknownModelUsageV1(kind="UNKNOWN"),
        status=ModelCallState.ACCEPTED,
        created_at=datetime.now(UTC),
    )


def _reservation(
    *,
    task_id: UUID,
    model_call_id: UUID | None = None,
    reservation_id: UUID | None = None,
    account_id: UUID | None = None,
) -> BudgetReservationV1:
    return BudgetReservationV1(
        reservation_id=reservation_id or uuid4(),
        task_id=task_id,
        model_call_id=model_call_id,
        account_id=account_id or uuid4(),
        reservation_version=1,
        reserved=RESERVED,
        settled=BudgetQuantityV1(),
        reservation_digest=DIGEST,
        status=BudgetReservationStatus.RESERVED,
        created_at=datetime.now(UTC),
        row_version=1,
    )


def _reserve_entry(
    reservation: BudgetReservationV1, entry_id: UUID | None = None
) -> BudgetLedgerEntryV1:
    return BudgetLedgerEntryV1(
        entry_id=entry_id or uuid4(),
        task_id=reservation.task_id,
        account_id=reservation.account_id,
        reservation_id=reservation.reservation_id,
        entry_kind=BudgetLedgerEntryKind.RESERVE,
        delta=reservation.reserved.as_delta(),
        currency="CNY",
        created_at=reservation.created_at,
    )


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class Dev04BudgetPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev04_budget_{os.getpid()}_{uuid4().hex[:8]}"
        self.isolated_url = _isolated_url(TEST_DATABASE_URL, self.dbname)
        self.isolated_pg = _pg_url(self.isolated_url)
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{self.dbname}"')
        previous_url = os.environ.get("KNOWLEDGE_DATABASE_URL")
        os.environ["KNOWLEDGE_DATABASE_URL"] = self.isolated_url
        try:
            await asyncio.to_thread(command.upgrade, _alembic_config(), "head")
        finally:
            if previous_url is None:
                os.environ.pop("KNOWLEDGE_DATABASE_URL", None)
            else:
                os.environ["KNOWLEDGE_DATABASE_URL"] = previous_url

        self.engine = build_async_engine(DatabaseEngineSettings(url=self.isolated_url))
        self.session_factory = build_session_factory(self.engine)

        self.ids = {
            name: uuid4()
            for name in (
                "org",
                "user",
                "artifact",
                "conversation",
                "task",
                "message",
                "input",
                "agent",
                "pack",
            )
        }
        await _seed(self.engine, self.ids)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        assert TEST_DATABASE_URL is not None
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')

    async def _insert_reserved(
        self, reservation: BudgetReservationV1, entry: BudgetLedgerEntryV1
    ):
        async with self.session_factory() as session:
            repository = BudgetLedgerRepository(session)
            result = await repository.insert_reserved(reservation, entry)
            await session.commit()
        return result

    async def _insert_model_call(self, call: ModelCallV1) -> None:
        async with self.session_factory() as session:
            await ModelCallLedgerRepository(session).insert_accepted(call)
            await session.commit()

    async def _transition(
        self,
        reservation_id: UUID,
        *,
        from_status: BudgetReservationStatus,
        to_status: BudgetReservationStatus,
        expected_row_version: int,
        settled: BudgetQuantityV1 | None = None,
    ):
        async with self.session_factory() as session:
            repository = BudgetLedgerRepository(session)
            result = await repository.transition_status(
                reservation_id,
                from_status=from_status,
                to_status=to_status,
                expected_row_version=expected_row_version,
                settled=settled,
            )
            await session.commit()
        return result

    async def _append_entry(self, entry: BudgetLedgerEntryV1) -> BudgetLedgerEntryV1:
        async with self.session_factory() as session:
            repository = BudgetLedgerRepository(session)
            result = await repository.append_entry(entry)
            await session.commit()
        return result

    async def _get(self, reservation_id: UUID) -> BudgetReservationV1 | None:
        async with self.session_factory() as session:
            return await BudgetLedgerRepository(session).get_by_id(reservation_id)

    async def _entries(self, reservation_id: UUID) -> list[BudgetLedgerEntryV1]:
        async with self.session_factory() as session:
            return await BudgetLedgerRepository(session).list_entries(reservation_id)

    def _reservation_row(self, reservation_id: UUID) -> dict:
        with psycopg.connect(self.isolated_pg, row_factory=dict_row) as connection:
            rows = list(
                connection.execute(
                    "SELECT status, row_version, reserved_input_tokens, settled_input_tokens, "
                    "settled_cost_amount, settled_at FROM workflow.budget_reservation "
                    "WHERE id = %s",
                    (reservation_id,),
                )
            )
        return rows[0]

    async def test_insert_reserved_and_read_back(self) -> None:
        reservation = _reservation(task_id=self.ids["task"])
        entry = _reserve_entry(reservation)
        result = await self._insert_reserved(reservation, entry)

        self.assertEqual(result.disposition, BudgetReservationInsertDisposition.INSERTED)
        self.assertEqual(result.row_version, 1)

        restored = await self._get(reservation.reservation_id)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored, reservation)

        row = self._reservation_row(reservation.reservation_id)
        self.assertEqual(row["status"], "RESERVED")
        self.assertEqual(row["reserved_input_tokens"], 100)
        self.assertEqual(row["settled_input_tokens"], 0)

    async def test_insert_reserved_is_idempotent_on_duplicate_id(self) -> None:
        reservation_id = uuid4()
        first = _reservation(task_id=self.ids["task"], reservation_id=reservation_id)
        await self._insert_reserved(first, _reserve_entry(first))

        second = _reservation(
            task_id=self.ids["task"],
            reservation_id=reservation_id,
            account_id=uuid4(),  # 不同账户不应覆盖既有事实
        )
        result = await self._insert_reserved(second, _reserve_entry(second))

        self.assertEqual(result.disposition, BudgetReservationInsertDisposition.ALREADY_PRESENT)
        self.assertEqual(result.reservation.account_id, first.account_id)
        self.assertEqual(result.row_version, 1)

        entries = await self._entries(reservation_id)
        self.assertEqual(len(entries), 1)  # 未重复计入RESERVE分录

    async def test_full_chain_reserved_to_settled_with_conservation(self) -> None:
        call = _accepted_call(self.ids["pack"])
        await self._insert_model_call(call)

        reservation = _reservation(task_id=self.ids["task"], model_call_id=call.model_call_id)
        await self._insert_reserved(reservation, _reserve_entry(reservation))

        consuming = await self._transition(
            reservation.reservation_id,
            from_status=BudgetReservationStatus.RESERVED,
            to_status=BudgetReservationStatus.CONSUMING,
            expected_row_version=1,
        )
        self.assertTrue(consuming.committed)

        settle, ret = settle_deltas(RESERVED, SETTLED)
        settled = await self._transition(
            reservation.reservation_id,
            from_status=BudgetReservationStatus.CONSUMING,
            to_status=BudgetReservationStatus.SETTLED,
            expected_row_version=2,
            settled=SETTLED,
        )
        self.assertTrue(settled.committed)
        self.assertEqual(settled.reservation.status, BudgetReservationStatus.SETTLED)
        self.assertIsNotNone(settled.reservation.settled_at)

        await self._append_entry(
            _reserve_entry(reservation).model_copy(
                update={
                    "entry_id": uuid4(),
                    "entry_kind": BudgetLedgerEntryKind.SETTLE,
                    "delta": settle,
                    "created_at": reservation.created_at + timedelta(seconds=1),
                }
            )
        )
        await self._append_entry(
            _reserve_entry(reservation).model_copy(
                update={
                    "entry_id": uuid4(),
                    "entry_kind": BudgetLedgerEntryKind.RETURN,
                    "delta": ret,
                    "created_at": reservation.created_at + timedelta(seconds=2),
                }
            )
        )

        row = self._reservation_row(reservation.reservation_id)
        self.assertEqual(row["status"], "SETTLED")
        self.assertEqual(row["settled_input_tokens"], 80)
        self.assertEqual(row["settled_cost_amount"], Decimal("0.40"))
        self.assertIsNotNone(row["settled_at"])

        entries = await self._entries(reservation.reservation_id)
        kinds = [e.entry_kind for e in entries]
        self.assertEqual(kinds, [BudgetLedgerEntryKind.RESERVE, BudgetLedgerEntryKind.SETTLE,
                                 BudgetLedgerEntryKind.RETURN])
        self.assertTrue(is_conserved(RESERVED, SETTLED, RESERVED.subtract(SETTLED)))

    async def test_get_by_model_call_id(self) -> None:
        call = _accepted_call(self.ids["pack"])
        await self._insert_model_call(call)
        reservation = _reservation(task_id=self.ids["task"], model_call_id=call.model_call_id)
        await self._insert_reserved(reservation, _reserve_entry(reservation))

        async with self.session_factory() as session:
            restored = await BudgetLedgerRepository(session).get_by_model_call_id(
                call.model_call_id
            )
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.reservation_id, reservation.reservation_id)

    async def test_transition_rejects_stale_row_version(self) -> None:
        reservation = _reservation(task_id=self.ids["task"])
        await self._insert_reserved(reservation, _reserve_entry(reservation))

        result = await self._transition(
            reservation.reservation_id,
            from_status=BudgetReservationStatus.RESERVED,
            to_status=BudgetReservationStatus.CONSUMING,
            expected_row_version=2,
        )
        self.assertFalse(result.committed)
        self.assertEqual(
            result.conflict_reason, BudgetReservationTransitionConflict.ROW_VERSION_CONFLICT
        )

    async def test_transition_rejects_wrong_from_status(self) -> None:
        reservation = _reservation(task_id=self.ids["task"])
        await self._insert_reserved(reservation, _reserve_entry(reservation))

        result = await self._transition(
            reservation.reservation_id,
            from_status=BudgetReservationStatus.CONSUMING,
            to_status=BudgetReservationStatus.SETTLED,
            expected_row_version=1,
            settled=SETTLED,
        )
        self.assertFalse(result.committed)
        self.assertEqual(
            result.conflict_reason, BudgetReservationTransitionConflict.NOT_IN_STATE
        )

    async def test_transition_rejects_not_found(self) -> None:
        result = await self._transition(
            uuid4(),
            from_status=BudgetReservationStatus.RESERVED,
            to_status=BudgetReservationStatus.CONSUMING,
            expected_row_version=1,
        )
        self.assertFalse(result.committed)
        self.assertEqual(result.conflict_reason, BudgetReservationTransitionConflict.NOT_FOUND)

    async def test_illegal_transition_raises_stable_error(self) -> None:
        reservation = _reservation(task_id=self.ids["task"])
        await self._insert_reserved(reservation, _reserve_entry(reservation))

        with self.assertRaises(BudgetReservationStateViolation) as raised:
            await self._transition(
                reservation.reservation_id,
                from_status=BudgetReservationStatus.RESERVED,
                to_status=BudgetReservationStatus.SETTLED,
                expected_row_version=1,
                settled=SETTLED,
            )
        self.assertEqual(raised.exception.code, "BUDGET_RESERVATION_STATE_TRANSITION_INVALID")

    async def test_append_entry_idempotent_by_entry_id(self) -> None:
        reservation = _reservation(task_id=self.ids["task"])
        entry_id = uuid4()
        await self._insert_reserved(reservation, _reserve_entry(reservation, entry_id=entry_id))

        duplicate = _reserve_entry(reservation, entry_id=entry_id).model_copy(
            update={"delta": BudgetQuantityDeltaV1(input_tokens=999)}
        )
        result = await self._append_entry(duplicate)

        self.assertEqual(result.delta.input_tokens, 100)  # 不覆盖既有事实
        entries = await self._entries(reservation.reservation_id)
        self.assertEqual(len(entries), 1)

    async def test_entry_kind_check_rejects_malformed_row(self) -> None:
        reservation = _reservation(task_id=self.ids["task"])
        with self.assertRaises(IntegrityError):
            async with self.engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO workflow.budget_ledger_entry "
                        "(entry_id,task_id,account_id,reservation_id,entry_kind,"
                        "model_call_count_delta,input_tokens_delta,output_tokens_delta,"
                        "tool_count_delta,cost_amount_delta,currency,created_at) VALUES "
                        "(:id,:task,:account,:reservation,'BOGUS',0,0,0,0,0,'CNY',now())"
                    ),
                    {
                        "id": uuid4(),
                        "task": self.ids["task"],
                        "account": reservation.account_id,
                        "reservation": reservation.reservation_id,
                    },
                )

    async def test_settled_le_reserved_check_rejects_overrun(self) -> None:
        reservation = _reservation(task_id=self.ids["task"])
        with self.assertRaises(IntegrityError):
            async with self.engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO workflow.budget_reservation "
                        "(id,task_id,account_id,reservation_version,reserved_model_call_count,"
                        "reserved_input_tokens,reserved_output_tokens,reserved_tool_count,"
                        "reserved_cost_amount,settled_model_call_count,settled_input_tokens,"
                        "settled_output_tokens,settled_tool_count,settled_cost_amount,"
                        "reservation_digest,status,row_version,created_at) VALUES "
                        "(:id,:task,:account,1,1,100,50,0,0.50,1,101,0,0,0,:digest,'RESERVED',"
                        "1,now())"
                    ),
                    {
                        "id": uuid4(),
                        "task": self.ids["task"],
                        "account": reservation.account_id,
                        "digest": DIGEST,
                    },
                )


if __name__ == "__main__":
    unittest.main()
