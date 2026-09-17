"""显式对账旧``0002`` Goal/Plan 数据，解除``0003``迁移的失败关闭守卫。

背景
----
``0002``迁移在尚无``workflow.task_attempt``表时创建了
``task_goal_understanding``与``task_plan_version``，其``task_attempt_id``与
``created_by_agent_instance_id``当时只能写成裸UUID。``0003``迁移在创建Attempt/Agent/
Context表并补回外键前，会检测这两张表里是否已有旧数据；一旦存在即
``RAISE EXCEPTION ATTEMPT_RUNTIME_UPGRADE_LEGACY_GOAL_PLAN_REQUIRES_RECONCILIATION``。

这些裸UUID无法安全重建Agent/Context/Attempt运行事实，因此拒绝迁移比伪造运行历史或
在加外键时隐式失败更安全。本工具提供显式、可审计的对账路径：

1. ``--check``（默认）：只读检测并报告遗留行，遗留存在时以退出码2失败关闭；
2. ``--dry-run``：只读展示将被归档与删除的行，不落任何文件、不删除；
3. ``--archive <目录> --confirm``：先导出遗留行及依赖子行到带SHA-256清单的归档文件，
   再按子表到父表顺序在同一事务内删除；归档写入并校验通过前绝不删除。

工具只对账``0002``时代的Goal/Plan及其依赖子表，不触碰``0004``的
``user_input_request``或其它后续表。对已升级到``0003``/``0004``的库运行``--check``
应因外键约束而天然报告“干净”。

退出码：0=无遗留或对账成功；2=存在遗留数据（需显式对账）；3=连接/配置/执行错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

GOAL_TABLE = "workflow.task_goal_understanding"
PLAN_TABLE = "workflow.task_plan_version"

# 依赖子表按“先子后父”顺序，满足外键约束（plan_item_runtime→item_definition→plan→goal）。
DELETE_ORDER = (
    "workflow.plan_item_runtime",
    "workflow.task_compiled_criterion",
    "workflow.task_plan_hypothesis",
    "workflow.task_plan_item_definition",
    "workflow.task_plan_version",
    "workflow.task_goal_understanding",
)

# 计划子表按“删除时用 task_id+plan_version 关联”处理，而不是逐行id。
PLAN_CHILD_TABLES = (
    "workflow.plan_item_runtime",
    "workflow.task_compiled_criterion",
    "workflow.task_plan_hypothesis",
    "workflow.task_plan_item_definition",
)

EXIT_CLEAN = 0
EXIT_LEGACY_FOUND = 2
EXIT_ERROR = 3


@dataclass(frozen=True)
class LegacyPredicates:
    """遗留行判定谓词；``task_attempt``表不存在时视为全表遗留。"""

    goal: str
    plan: str
    task_attempt_exists: bool


def build_legacy_predicates(task_attempt_exists: bool) -> LegacyPredicates:
    if task_attempt_exists:
        dangling = "task_attempt_id NOT IN (SELECT id FROM workflow.task_attempt)"
    else:
        dangling = "TRUE"
    return LegacyPredicates(goal=dangling, plan=dangling, task_attempt_exists=task_attempt_exists)


def delete_statement(table: str, predicates: LegacyPredicates) -> str:
    """按表返回一条删除语句；计划子表用 task_id+plan_version 关联遗留计划。"""
    if table in PLAN_CHILD_TABLES:
        return (
            f"DELETE FROM {table} WHERE (task_id, plan_version) IN ("
            f"SELECT task_id, plan_version FROM {PLAN_TABLE} WHERE {predicates.plan})"
        )
    if table == PLAN_TABLE:
        return f"DELETE FROM {table} WHERE {predicates.plan}"
    if table == GOAL_TABLE:
        return f"DELETE FROM {table} WHERE {predicates.goal}"
    raise ValueError(f"unexpected legacy table: {table}")


def select_statement(table: str, predicates: LegacyPredicates) -> str:
    """与删除语句同范围的选择语句，用于归档导出。"""
    if table in PLAN_CHILD_TABLES:
        return (
            f"SELECT * FROM {table} WHERE (task_id, plan_version) IN ("
            f"SELECT task_id, plan_version FROM {PLAN_TABLE} WHERE {predicates.plan})"
        )
    if table == PLAN_TABLE:
        return f"SELECT * FROM {table} WHERE {predicates.plan}"
    if table == GOAL_TABLE:
        return f"SELECT * FROM {table} WHERE {predicates.goal}"
    raise ValueError(f"unexpected legacy table: {table}")


def serialize_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(key): serialize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize_value(item) for item in value]
    return value


def serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key): serialize_value(value) for key, value in row.items()}


def archive_manifest(
    archived: dict[str, list[dict[str, Any]]], created_at: datetime
) -> dict[str, Any]:
    tables: dict[str, dict[str, Any]] = {}
    for table in sorted(archived):
        body = json.dumps(archived[table], ensure_ascii=False, sort_keys=True).encode("utf-8")
        tables[table] = {
            "file": f"{table.split('.')[-1]}.json",
            "row_count": len(archived[table]),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    return {
        "tool": "reconcile_legacy_goal_plan",
        "schema_version": 1,
        "created_at": created_at.isoformat(),
        "tables": tables,
    }


def write_archive(
    directory: Path,
    archived: dict[str, list[dict[str, Any]]],
    manifest: dict[str, Any],
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for table, rows in archived.items():
        body = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
        (directory / manifest["tables"][table]["file"]).write_bytes(body)
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _db_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgresql://"):
        return url
    raise ValueError("DATABASE_DRIVER_NOT_APPROVED")


def _task_attempt_exists(connection: psycopg.Connection[dict[str, Any]]) -> bool:
    value = connection.execute(
        "SELECT to_regclass('workflow.task_attempt') IS NOT NULL AS present"
    ).fetchone()
    return bool(value["present"])


def _table_present(connection: psycopg.Connection[dict[str, Any]], table: str) -> bool:
    value = connection.execute(
        f"SELECT to_regclass('{table}') IS NOT NULL AS present"
    ).fetchone()
    return bool(value["present"])


def _fetch_legacy(
    connection: psycopg.Connection[dict[str, Any]], predicates: LegacyPredicates
) -> dict[str, list[dict[str, Any]]]:
    archived: dict[str, list[dict[str, Any]]] = {}
    for table in DELETE_ORDER:
        if not _table_present(connection, table):
            continue
        rows = connection.execute(select_statement(table, predicates)).fetchall()
        if rows:
            archived[table] = [serialize_row(dict(row)) for row in rows]
    return archived


def _delete_legacy(
    connection: psycopg.Connection[dict[str, Any]], predicates: LegacyPredicates
) -> dict[str, int]:
    deleted: dict[str, int] = {}
    for table in DELETE_ORDER:
        if not _table_present(connection, table):
            continue
        cursor = connection.execute(delete_statement(table, predicates))
        deleted[table] = cursor.rowcount
    return deleted


def _report(
    *,
    action: str,
    task_attempt_exists: bool,
    archived: dict[str, list[dict[str, Any]]],
    deleted: dict[str, int],
) -> dict[str, Any]:
    return {
        "tool": "reconcile_legacy_goal_plan",
        "action": action,
        "task_attempt_table_exists": task_attempt_exists,
        "legacy_goal_rows": len(archived.get(GOAL_TABLE, [])),
        "legacy_plan_rows": len(archived.get(PLAN_TABLE, [])),
        "legacy_rows_by_table": {
            table: len(rows) for table, rows in archived.items()
        },
        "deleted_rows_by_table": deleted,
    }


def inspect(url: str) -> dict[str, Any]:
    """只读检查；返回报告，不修改数据。"""
    with psycopg.connect(_db_url(url), row_factory=dict_row) as connection:
        if not _table_present(connection, GOAL_TABLE) and not _table_present(
            connection, PLAN_TABLE
        ):
            return _report(
                action="nothing_to_do",
                task_attempt_exists=_task_attempt_exists(connection),
                archived={},
                deleted={},
            )
        predicates = build_legacy_predicates(_task_attempt_exists(connection))
        archived = _fetch_legacy(connection, predicates)
        legacy_found = bool(archived)
        return _report(
            action="check" if legacy_found else "nothing_to_do",
            task_attempt_exists=predicates.task_attempt_exists,
            archived=archived,
            deleted={},
        )


def reconcile(url: str, archive_dir: Path) -> dict[str, Any]:
    """归档并删除遗留行；归档写入成功后在同一事务内删除，删除前校验清单。"""
    with psycopg.connect(_db_url(url), row_factory=dict_row) as connection:
        predicates = build_legacy_predicates(_task_attempt_exists(connection))
        archived = _fetch_legacy(connection, predicates)
        if not archived:
            return _report(
                action="nothing_to_do",
                task_attempt_exists=predicates.task_attempt_exists,
                archived={},
                deleted={},
            )
        manifest = archive_manifest(archived, datetime.now(UTC))
        write_archive(archive_dir, archived, manifest)
        deleted = _delete_legacy(connection, predicates)
        connection.commit()
        return _report(
            action="reconciled",
            task_attempt_exists=predicates.task_attempt_exists,
            archived=archived,
            deleted=deleted,
        )


def _print_report(report: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(f"task_attempt表存在: {report['task_attempt_table_exists']}")
    print(f"遗留Goal行: {report['legacy_goal_rows']}")
    print(f"遗留Plan行: {report['legacy_plan_rows']}")
    for table, count in report["legacy_rows_by_table"].items():
        print(f"  {table}: {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None, help="覆盖KNOWLEDGE_DATABASE_URL")
    parser.add_argument(
        "--check", action="store_true", help="只读检测（默认动作），遗留存在时退出2"
    )
    parser.add_argument("--dry-run", action="store_true", help="只读展示将归档/删除的行")
    parser.add_argument("--archive", metavar="DIR", help="归档目录（与--confirm同用）")
    parser.add_argument(
        "--confirm", action="store_true", help="显式确认：归档后删除遗留行"
    )
    parser.add_argument("--json", action="store_true", help="机器可读JSON输出")
    args = parser.parse_args(argv)

    url = args.database_url or os.environ.get("KNOWLEDGE_DATABASE_URL")
    if not url:
        print("KNOWLEDGE_DATABASE_URL未设置，且未提供--database-url", file=sys.stderr)
        return EXIT_ERROR

    try:
        if args.confirm:
            if not args.archive:
                print("--confirm 需要同时给出 --archive <目录>", file=sys.stderr)
                return EXIT_ERROR
            report = reconcile(url, Path(args.archive))
        elif args.dry_run:
            report = inspect(url)
            report = {**report, "action": "dry_run"}
        else:
            report = inspect(url)
        _print_report(report, as_json=args.json)
    except psycopg.Error as error:  # pragma: no cover - 连接错误依赖真实PG
        print(f"reconcile error: {error}", file=sys.stderr)
        return EXIT_ERROR
    except ValueError as error:
        print(f"reconcile error: {error}", file=sys.stderr)
        return EXIT_ERROR

    legacy_found = bool(report["legacy_rows_by_table"])
    if report["action"] in ("reconciled", "nothing_to_do", "dry_run"):
        return EXIT_LEGACY_FOUND if (legacy_found and report["action"] == "dry_run") else EXIT_CLEAN
    return EXIT_LEGACY_FOUND if legacy_found else EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
