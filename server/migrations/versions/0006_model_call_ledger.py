"""model_call ledger

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-22

DEV-04B：`workflow.model_call`模型调用账本表。保存稳定`model_call_id`、批准/实际端点与模型、
Context Pack引用、Token Accounting与Usage、预算预留引用、耗时、状态与稳定错误码。Token
Accounting按EXACT/ESTIMATED_DEV判别列+互斥分支列落地（估算分支不伪造tokenizer/template精确
字段）；Usage按三态落地（UNKNOWN不携带Token/费用）。尚未冻结FK的引用（Grant/Endpoint/Branch/
Delegation/Budget Reservation/Action Attempt）保持裸UUID列，待对应表投影批准后闭合。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_call",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=True),
        sa.Column("attempt_id", sa.UUID(), nullable=True),
        sa.Column("action_attempt_id", sa.UUID(), nullable=True),
        sa.Column("branch_id", sa.UUID(), nullable=True),
        sa.Column("delegation_id", sa.UUID(), nullable=True),
        sa.Column("execution_generation", sa.BigInteger(), nullable=True),
        sa.Column("cancellation_generation", sa.BigInteger(), nullable=True),
        sa.Column("approved_endpoint_id", sa.UUID(), nullable=True),
        sa.Column("actual_endpoint_id", sa.UUID(), nullable=True),
        sa.Column("capability_alias", sa.String(length=64), nullable=False),
        sa.Column("approved_model_id", sa.String(length=128), nullable=False),
        sa.Column("approved_model_revision", sa.String(length=128), nullable=False),
        sa.Column("actual_provider", sa.String(length=64), nullable=True),
        sa.Column("actual_model_id", sa.String(length=128), nullable=True),
        sa.Column("actual_model_revision", sa.String(length=128), nullable=True),
        sa.Column("bifrost_request_id", sa.String(length=128), nullable=True),
        sa.Column("provider_request_id", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column(
            "tool_schema_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("context_pack_id", sa.UUID(), nullable=False),
        sa.Column("context_policy_version", sa.String(length=64), nullable=False),
        sa.Column("authorization_version", sa.BigInteger(), nullable=False),
        sa.Column("grant_id", sa.UUID(), nullable=True),
        sa.Column("runtime_version", sa.String(length=64), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=True),
        sa.Column("agent_manifest_version", sa.String(length=64), nullable=False),
        sa.Column("conversation_memory_version", sa.BigInteger(), nullable=False),
        sa.Column("user_private_memory_version", sa.BigInteger(), nullable=False),
        sa.Column("task_memory_version", sa.BigInteger(), nullable=False),
        sa.Column("token_accounting_kind", sa.String(length=16), nullable=False),
        sa.Column("tokenizer_revision", sa.String(length=128), nullable=True),
        sa.Column("chat_template_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("exact_input_tokens", sa.Integer(), nullable=True),
        sa.Column("estimator_revision", sa.String(length=128), nullable=True),
        sa.Column("serialized_request_bytes", sa.Integer(), nullable=True),
        sa.Column("protocol_overhead_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_schema_id", sa.String(length=128), nullable=True),
        sa.Column("output_schema_version", sa.String(length=64), nullable=True),
        sa.Column("data_scope", sa.String(length=32), nullable=False),
        sa.Column("request_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("response_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("usage_source", sa.String(length=24), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_amount", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("cost_currency", sa.String(length=8), nullable=True),
        sa.Column("budget_reservation_id", sa.UUID(), nullable=True),
        sa.Column("rate_card_version", sa.String(length=64), nullable=True),
        sa.Column("exchange_rate_snapshot_version", sa.String(length=64), nullable=True),
        sa.Column("queue_ms", sa.Integer(), nullable=True),
        sa.Column("ttft_ms", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(token_accounting_kind = 'EXACT' AND tokenizer_revision IS NOT NULL AND "
            "chat_template_hash IS NOT NULL AND exact_input_tokens IS NOT NULL AND "
            "estimator_revision IS NULL AND serialized_request_bytes IS NULL AND "
            "protocol_overhead_tokens IS NULL AND estimated_input_tokens IS NULL) OR "
            "(token_accounting_kind = 'ESTIMATED_DEV' AND estimator_revision IS NOT NULL AND "
            "serialized_request_bytes IS NOT NULL AND protocol_overhead_tokens IS NOT NULL AND "
            "estimated_input_tokens IS NOT NULL AND tokenizer_revision IS NULL AND "
            "chat_template_hash IS NULL AND exact_input_tokens IS NULL)",
            name=op.f("ck_model_call_token_accounting_branch"),
        ),
        sa.CheckConstraint(
            "(exact_input_tokens IS NULL OR exact_input_tokens >= 0) AND "
            "(estimated_input_tokens IS NULL OR estimated_input_tokens >= 0) AND "
            "(serialized_request_bytes IS NULL OR serialized_request_bytes >= 0) AND "
            "(protocol_overhead_tokens IS NULL OR protocol_overhead_tokens >= 0) AND "
            "(input_tokens IS NULL OR input_tokens >= 0) AND "
            "(output_tokens IS NULL OR output_tokens >= 0) AND "
            "(queue_ms IS NULL OR queue_ms >= 0) AND (ttft_ms IS NULL OR ttft_ms >= 0) AND "
            "(duration_ms IS NULL OR duration_ms >= 0)",
            name=op.f("ck_model_call_counts_nonnegative"),
        ),
        sa.CheckConstraint(
            "data_scope IN ('PUBLIC_DEVELOPMENT','APPROVED_EXTERNAL_TRIAL',"
            "'INTERNAL_PRODUCTION','PUBLIC_EVALUATION')",
            name=op.f("ck_model_call_data_scope_allowed"),
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$' AND "
            "(response_hash IS NULL OR response_hash ~ '^[0-9a-f]{64}$') AND "
            "(chat_template_hash IS NULL OR chat_template_hash ~ '^[0-9a-f]{64}$')",
            name=op.f("ck_model_call_digests_lower_hex"),
        ),
        sa.CheckConstraint(
            "conversation_memory_version >= 0 AND user_private_memory_version >= 0 AND "
            "task_memory_version >= 0",
            name=op.f("ck_model_call_memory_versions_nonnegative"),
        ),
        sa.CheckConstraint(
            "row_version >= 1", name=op.f("ck_model_call_row_version_positive")
        ),
        sa.CheckConstraint(
            "status IN ('ACCEPTED','SENDING','STREAMING','COMPLETED','FAILED','CANCELLED',"
            "'UNKNOWN')",
            name=op.f("ck_model_call_status_allowed"),
        ),
        sa.CheckConstraint(
            "token_accounting_kind IN ('EXACT','ESTIMATED_DEV')",
            name=op.f("ck_model_call_token_accounting_kind_allowed"),
        ),
        sa.CheckConstraint(
            "(usage_source = 'UNKNOWN' AND input_tokens IS NULL AND output_tokens IS NULL) OR "
            "(usage_source <> 'UNKNOWN' AND input_tokens IS NOT NULL AND output_tokens IS NOT "
            "NULL)",
            name=op.f("ck_model_call_usage_branch"),
        ),
        sa.CheckConstraint(
            "usage_source IN ('PROVIDER','WORST_CASE_RESERVATION','UNKNOWN')",
            name=op.f("ck_model_call_usage_source_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["workflow.task_attempt.id"],
            name=op.f("fk_model_call_attempt_id_task_attempt"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["context_pack_id"],
            ["workflow.context_pack.id"],
            name=op.f("fk_model_call_context_pack_id_context_pack"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["workflow.intelligent_task.id"],
            name=op.f("fk_model_call_task_id_intelligent_task"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_call")),
        schema="workflow",
    )
    op.create_index(
        "ix_model_call_status", "model_call", ["status"], unique=False, schema="workflow"
    )
    op.create_index(
        "ix_model_call_task_created",
        "model_call",
        ["task_id", "created_at"],
        unique=False,
        schema="workflow",
    )
    op.create_index(
        "uq_model_call_provider_request_id",
        "model_call",
        ["provider_request_id"],
        unique=True,
        schema="workflow",
        postgresql_where=sa.text("provider_request_id IS NOT NULL"),
    )


def downgrade() -> None:
    # 模型调用账本是业务事实；有数据时只能先备份/对账，不能以降级静默删除。
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM workflow.model_call) THEN "
            "RAISE EXCEPTION 'MODEL_CALL_DOWNGRADE_REQUIRES_BACKUP_RESTORE'; "
            "END IF; END $$"
        )
    )
    op.drop_index(
        "uq_model_call_provider_request_id",
        table_name="model_call",
        schema="workflow",
        postgresql_where=sa.text("provider_request_id IS NOT NULL"),
    )
    op.drop_index("ix_model_call_task_created", table_name="model_call", schema="workflow")
    op.drop_index("ix_model_call_status", table_name="model_call", schema="workflow")
    op.drop_table("model_call", schema="workflow")
