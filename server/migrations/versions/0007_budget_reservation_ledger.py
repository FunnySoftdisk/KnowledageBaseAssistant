"""budget reservation and ledger

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-22

DEV-04C：`workflow.budget_reservation`与`workflow.budget_ledger_entry`。预留保存同一逻辑外调
唯一的五维预留上限与实耗、状态与`row_version`；账本只追加带符号五维增量分录，`entry_kind`限定
六种且每维总账守恒。预算账户/Branch/Delegation/Action Attempt/Capability Invocation/事件表尚未
冻结，`account_id`/`branch_id`/`delegation_id`/`action_attempt_id`/`invocation_id`/
`caused_by_event_id`保持裸UUID列；`task`/`task_attempt`/`model_call`已冻结，落地真实外键。
`model_call.budget_reservation_id`的逆向边为避免跨迁移环形外键，暂维持FK_DEFERRED。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "budget_reservation",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("branch_id", sa.UUID(), nullable=True),
        sa.Column("delegation_id", sa.UUID(), nullable=True),
        sa.Column("task_attempt_id", sa.UUID(), nullable=True),
        sa.Column("model_call_id", sa.UUID(), nullable=True),
        sa.Column("action_attempt_id", sa.UUID(), nullable=True),
        sa.Column("invocation_id", sa.UUID(), nullable=True),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("reservation_version", sa.Integer(), nullable=False),
        sa.Column("reserved_model_call_count", sa.Integer(), nullable=False),
        sa.Column("reserved_input_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_output_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_tool_count", sa.Integer(), nullable=False),
        sa.Column("reserved_cost_amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("settled_model_call_count", sa.Integer(), nullable=False),
        sa.Column("settled_input_tokens", sa.Integer(), nullable=False),
        sa.Column("settled_output_tokens", sa.Integer(), nullable=False),
        sa.Column("settled_tool_count", sa.Integer(), nullable=False),
        sa.Column("settled_cost_amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("reservation_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "reservation_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_budget_reservation_reservation_digest_lower_hex"),
        ),
        sa.CheckConstraint(
            "reserved_model_call_count >= 0 AND reserved_input_tokens >= 0 AND "
            "reserved_output_tokens >= 0 AND reserved_tool_count >= 0 AND "
            "reserved_cost_amount >= 0",
            name=op.f("ck_budget_reservation_reserved_nonnegative"),
        ),
        sa.CheckConstraint(
            "reservation_version >= 1",
            name=op.f("ck_budget_reservation_reservation_version_positive"),
        ),
        sa.CheckConstraint(
            "row_version >= 1",
            name=op.f("ck_budget_reservation_row_version_positive"),
        ),
        sa.CheckConstraint(
            "settled_model_call_count <= reserved_model_call_count AND "
            "settled_input_tokens <= reserved_input_tokens AND "
            "settled_output_tokens <= reserved_output_tokens AND "
            "settled_tool_count <= reserved_tool_count AND "
            "settled_cost_amount <= reserved_cost_amount",
            name=op.f("ck_budget_reservation_settled_le_reserved"),
        ),
        sa.CheckConstraint(
            "settled_model_call_count >= 0 AND settled_input_tokens >= 0 AND "
            "settled_output_tokens >= 0 AND settled_tool_count >= 0 AND "
            "settled_cost_amount >= 0",
            name=op.f("ck_budget_reservation_settled_nonnegative"),
        ),
        sa.CheckConstraint(
            "status IN ('RESERVED','CONSUMING','SETTLED','RELEASED','EXPIRED','RECONCILING')",
            name=op.f("ck_budget_reservation_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["model_call_id"],
            ["workflow.model_call.id"],
            name=op.f("fk_budget_reservation_model_call_id_model_call"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_attempt_id"],
            ["workflow.task_attempt.id"],
            name=op.f("fk_budget_reservation_task_attempt_id_task_attempt"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["workflow.intelligent_task.id"],
            name=op.f("fk_budget_reservation_task_id_intelligent_task"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_reservation")),
        schema="workflow",
    )
    op.create_index(
        "ix_budget_reservation_status",
        "budget_reservation",
        ["status"],
        unique=False,
        schema="workflow",
    )
    op.create_index(
        "ix_budget_reservation_task_created",
        "budget_reservation",
        ["task_id", "created_at"],
        unique=False,
        schema="workflow",
    )
    op.create_index(
        "uq_budget_reservation_invocation",
        "budget_reservation",
        ["invocation_id"],
        unique=True,
        schema="workflow",
        postgresql_where=sa.text("invocation_id IS NOT NULL"),
    )
    op.create_index(
        "uq_budget_reservation_model_call",
        "budget_reservation",
        ["model_call_id"],
        unique=True,
        schema="workflow",
        postgresql_where=sa.text("model_call_id IS NOT NULL"),
    )

    op.create_table(
        "budget_ledger_entry",
        sa.Column("entry_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("reservation_id", sa.UUID(), nullable=True),
        sa.Column("entry_kind", sa.String(length=32), nullable=False),
        sa.Column("model_call_count_delta", sa.Integer(), nullable=False),
        sa.Column("input_tokens_delta", sa.Integer(), nullable=False),
        sa.Column("output_tokens_delta", sa.Integer(), nullable=False),
        sa.Column("tool_count_delta", sa.Integer(), nullable=False),
        sa.Column("cost_amount_delta", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("rate_card_version", sa.String(length=64), nullable=True),
        sa.Column("exchange_rate_snapshot_version", sa.String(length=64), nullable=True),
        sa.Column("caused_by_event_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "currency <> ''", name=op.f("ck_budget_ledger_entry_currency_nonempty")
        ),
        sa.CheckConstraint(
            "entry_kind IN ('ALLOCATE','RESERVE','SETTLE','RELEASE','RETURN',"
            "'WORST_CASE_SETTLE')",
            name=op.f("ck_budget_ledger_entry_entry_kind_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["workflow.budget_reservation.id"],
            name=op.f("fk_budget_ledger_entry_reservation_id_budget_reservation"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["workflow.intelligent_task.id"],
            name=op.f("fk_budget_ledger_entry_task_id_intelligent_task"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("entry_id", name=op.f("pk_budget_ledger_entry")),
        schema="workflow",
    )
    op.create_index(
        "ix_budget_ledger_entry_reservation_created",
        "budget_ledger_entry",
        ["reservation_id", "created_at"],
        unique=False,
        schema="workflow",
    )
    op.create_index(
        "ix_budget_ledger_entry_task",
        "budget_ledger_entry",
        ["task_id"],
        unique=False,
        schema="workflow",
    )


def downgrade() -> None:
    # 预算预留与账本是计费事实；有数据时只能先备份/对账，不能以降级静默删除。
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM workflow.budget_ledger_entry) "
            "OR EXISTS (SELECT 1 FROM workflow.budget_reservation) THEN "
            "RAISE EXCEPTION 'BUDGET_LEDGER_DOWNGRADE_REQUIRES_BACKUP_RESTORE'; "
            "END IF; END $$"
        )
    )
    op.drop_index("ix_budget_ledger_entry_task", table_name="budget_ledger_entry", schema="workflow")
    op.drop_index(
        "ix_budget_ledger_entry_reservation_created",
        table_name="budget_ledger_entry",
        schema="workflow",
    )
    op.drop_table("budget_ledger_entry", schema="workflow")
    op.drop_index(
        "uq_budget_reservation_model_call",
        table_name="budget_reservation",
        schema="workflow",
        postgresql_where=sa.text("model_call_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_budget_reservation_invocation",
        table_name="budget_reservation",
        schema="workflow",
        postgresql_where=sa.text("invocation_id IS NOT NULL"),
    )
    op.drop_index(
        "ix_budget_reservation_task_created",
        table_name="budget_reservation",
        schema="workflow",
    )
    op.drop_index("ix_budget_reservation_status", table_name="budget_reservation", schema="workflow")
    op.drop_table("budget_reservation", schema="workflow")
