from collections.abc import Callable
from importlib.metadata import version
from struct import pack

from pamqp import encode

_original_table_integer: Callable[[int], bytes] = encode.table_integer


def _encode_table_integer(value: int) -> bytes:
    if value < 0:
        if value >= -128:
            return b"b" + pack(">b", value)
        if value >= -32768:
            return b"s" + pack(">h", value)
        if value >= -2147483648:
            return b"I" + pack(">i", value)
        if value >= -(1 << 63):
            return b"l" + pack(">q", value)
        raise ValueError(f"Unsupported numeric value: {value}")
    return _original_table_integer(value)


def configure_amqp_encoding() -> None:
    if version("pamqp") == "3.3.0":
        encode.table_integer = _encode_table_integer
