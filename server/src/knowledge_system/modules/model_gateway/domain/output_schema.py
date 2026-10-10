"""严格结构化输出契约：封闭JSON Schema与“不支持不得静默退json_object”规则。

依据模型网关模块设计§12与百炼结构化输出边界：严格JSON Schema与JSON Object是两种不同能力，
嵌套/可空对象必须仍以封闭对象（`additionalProperties: false`）表达；Provider不支持本项目Schema时
不得静默退化为`json_object`，只能失败关闭或改走无结构化路径。本模块是纯规则，不接真实模型、
不保存Schema正文（正文由上游Schema Bundle保存，此处只冻结引用与摘要）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Sha256 = Annotated[str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]


class OutputSchemaMode(StrEnum):
    """结构化输出模式：严格Schema / 普通JSON Object / 不支持。"""

    STRICT_JSON_SCHEMA = "STRICT_JSON_SCHEMA"
    JSON_OBJECT = "JSON_OBJECT"
    UNSUPPORTED = "UNSUPPORTED"


class StructuredOutputViolation(ValueError):
    """稳定code可公开；异常中不保存输入正文、Secret或Provider原包。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class StrictOutputSchemaV1(BaseModel):
    """冻结的严格结构化输出Schema引用；`strict`由类型本身表达，不设可变开关。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    output_schema_id: str = Field(min_length=1)
    output_schema_version: str = Field(min_length=1)
    schema_digest: Sha256


_SUBSCHEMA_KEYS = ("items", "prefixItems", "additionalProperties", "contains", "not")
_MAP_KEYS = ("properties", "$defs", "definitions", "dependentSchemas", "patternProperties")
_LIST_KEYS = ("anyOf", "oneOf", "allOf")


def is_strict_json_schema(schema: Any) -> bool:
    """校验JSON Schema是否严格封闭：每个对象定义显式`additionalProperties: false`。

    顶层必须是JSON Schema对象（dict）；`type="object"`或携带`properties`的节点若未显式关闭
    `additionalProperties`即视为非严格。不含对象的Schema（如纯`{"type":"string"}`）空真成立。
    `$ref`不展开（引用目标由Schema Bundle以摘要校验，不在本函数内联解析）。
    """
    if not isinstance(schema, dict):
        return False
    return _is_strict_node(schema)


def _is_strict_node(node: Any) -> bool:
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            if node.get("additionalProperties") is not False:
                return False
        for key in _MAP_KEYS:
            value = node.get(key)
            if isinstance(value, dict) and not all(
                _is_strict_node(child) for child in value.values()
            ):
                return False
        for key in _SUBSCHEMA_KEYS:
            value = node.get(key)
            if isinstance(value, dict) and not _is_strict_node(value):
                return False
            if isinstance(value, list) and not all(
                _is_strict_node(child) for child in value
            ):
                return False
        for key in _LIST_KEYS:
            value = node.get(key)
            if isinstance(value, list) and not all(
                _is_strict_node(child) for child in value
            ):
                return False
    return True


def resolve_output_mode(
    *,
    schema: StrictOutputSchemaV1 | None,
    provider_supports_strict_schema: bool,
) -> OutputSchemaMode:
    """未请求结构化输出→JSON_OBJECT；Provider支持严格Schema→STRICT_JSON_SCHEMA；否则UNSUPPORTED。"""
    if schema is None:
        return OutputSchemaMode.JSON_OBJECT
    if provider_supports_strict_schema:
        return OutputSchemaMode.STRICT_JSON_SCHEMA
    return OutputSchemaMode.UNSUPPORTED


def require_strict_output(
    *,
    schema: StrictOutputSchemaV1 | None,
    provider_supports_strict_schema: bool,
) -> None:
    """请求了严格Schema但Provider不支持时失败关闭，禁止静默退json_object。"""
    if resolve_output_mode(
        schema=schema, provider_supports_strict_schema=provider_supports_strict_schema
    ) is OutputSchemaMode.UNSUPPORTED:
        raise StructuredOutputViolation("STRUCTURED_OUTPUT_SCHEMA_UNSUPPORTED")


__all__ = [
    "OutputSchemaMode",
    "StrictOutputSchemaV1",
    "StructuredOutputViolation",
    "is_strict_json_schema",
    "require_strict_output",
    "resolve_output_mode",
]
