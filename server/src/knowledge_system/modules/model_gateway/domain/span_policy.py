"""LLM Trace 属性白名单与Secret无条件剥离。

依据审计可观测设计§3.7与DEV-04F离线切片：项目自定义Span字段只允许版本化`llm_trace_allowlist_v1`；
Secret/认证材料、正文、URL、Digest全文与业务ID在任何环境无条件删除（开发`FULL_TRACE`也不放行）。
本模块是纯规则，只做字段名级判定；按字段类型/数据分类的多层脱敏（`RedactionProcessor`）属基础设施，
留待真实观测接入。
"""

from __future__ import annotations

from collections.abc import Mapping

# 版本化白名单：允许进入Phoenix LLM Trace的Span属性名（低基数、非敏感、可对账）。
LLM_TRACE_ALLOWLIST_V1 = frozenset(
    {
        "capability_alias",
        "approved_model_id",
        "approved_model_revision",
        "actual_model_id",
        "actual_model_revision",
        "actual_provider",
        "prompt_version",
        "model_call_status",
        "error_code",
        "settlement_kind",
        "usage_source",
        "input_tokens",
        "output_tokens",
        "queue_ms",
        "ttft_ms",
        "duration_ms",
        "context_pack_hash",
    }
)

# Secret/认证材料属性名（规范化小写）；命中无条件剥离，且优先于白名单。
_SENSITIVE_ATTRIBUTE_NAMES = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "api_key",
        "apikey",
        "x_api_key",
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "bearer",
        "password",
        "passwd",
        "secret",
        "secret_ref",
        "secretref",
        "credential",
        "credentials",
        "private_key",
        "signing_key",
        "jwt",
    }
)


class SpanPolicyViolation(ValueError):
    """稳定code可公开；异常中不保存属性值或Secret。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def is_sensitive_attribute_name(name: str) -> bool:
    """属性名是否命中Secret/认证材料集合（大小写不敏感、剥离优先于白名单）。"""
    return name.strip().lower() in _SENSITIVE_ATTRIBUTE_NAMES


def strip_secret_attributes(attributes: Mapping[str, object]) -> dict[str, object]:
    """无条件删除Secret/认证材料属性；适用于所有Span（含开发`FULL_TRACE`）。"""
    return {
        name: value for name, value in attributes.items() if not is_sensitive_attribute_name(name)
    }


def filter_llm_span_attributes(attributes: Mapping[str, object]) -> dict[str, object]:
    """先剥离Secret，再仅保留`llm_trace_allowlist_v1`白名单内的属性。"""
    stripped = strip_secret_attributes(attributes)
    return {name: value for name, value in stripped.items() if name in LLM_TRACE_ALLOWLIST_V1}


def require_no_secret_attributes(attributes: Mapping[str, object]) -> None:
    """断言不含Secret/认证材料；命中以稳定`SPAN_SECRET_ATTRIBUTE_PRESENT`失败关闭。"""
    for name in attributes:
        if is_sensitive_attribute_name(name):
            raise SpanPolicyViolation("SPAN_SECRET_ATTRIBUTE_PRESENT")


__all__ = [
    "LLM_TRACE_ALLOWLIST_V1",
    "SpanPolicyViolation",
    "filter_llm_span_attributes",
    "is_sensitive_attribute_name",
    "require_no_secret_attributes",
    "strip_secret_attributes",
]
