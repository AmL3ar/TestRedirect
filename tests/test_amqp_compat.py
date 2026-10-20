from pamqp import decode, encode

from app.amqp_compat import configure_amqp_encoding


def test_amqp_signed_integers_round_trip() -> None:
    configure_amqp_encoding()
    values = (
        -(2**63),
        -2147483649,
        -32769,
        -32768,
        -129,
        -128,
        -1,
        0,
        127,
        128,
        255,
        256,
    )

    for value in values:
        encoded = encode.field_table({"value": value})
        _, decoded = decode.field_table(encoded)
        assert decoded == {"value": value}
