"""DEV-03D：组装带加密/外置Codec的Temporal `DataConverter`（Client与Worker共用）。

Client以`build_data_converter(codec)`连接，Worker继承Client的Converter，因此两端
使用同一套加密/外置语义，重放与查询都以同一`kid`解密。
"""

from __future__ import annotations

from temporalio.contrib.pydantic import PydanticPayloadConverter
from temporalio.converter import DataConverter, PayloadCodec


def build_data_converter(payload_codec: PayloadCodec) -> DataConverter:
    """在Pydantic JSON转换器外包一层加密/外置Codec，其余行为与`pydantic_data_converter`一致。"""

    return DataConverter(
        payload_converter_class=PydanticPayloadConverter,
        payload_codec=payload_codec,
    )


__all__ = ["build_data_converter"]
