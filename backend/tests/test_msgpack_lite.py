import pytest

from hokma.market.msgpack_lite import MsgpackError, packb, unpackb


@pytest.mark.parametrize("value,hexa", [
    (None, "c0"), (True, "c3"), (False, "c2"), (0, "00"), (127, "7f"), (128, "cc80"), (255, "cc ff"), (256, "cd0100"), (65536, "ce00010000"),
    (-1, "ff"), (-32, "e0"), (-33, "d0df"), (-129, "d1ff7f"), ("", "a0"), ("abc", "a3616263"), ([], "90"), ({}, "80"),
    ({"T": "q"}, "81a154a171"), ([1, 2], "920102"),
])
def test_known_byte_vectors_pack_and_read_back(value, hexa):
    data = bytes.fromhex(hexa.replace(" ", ""))
    assert packb(value) == data and unpackb(data) == value


def test_floats_round_trip_and_a_32_bit_float_is_read():
    assert unpackb(packb(12.5)) == 12.5
    assert unpackb(bytes.fromhex("ca41480000")) == 12.5


def test_a_real_looking_alpaca_message_round_trips():
    message = [{"T": "q", "S": "QQQ261218C00500000", "bp": 1.25, "bs": 3, "ap": 1.3, "as": 4, "t": "2026-11-20T15:00:00.123456789Z", "c": None}]
    assert unpackb(packb(message)) == message
    assert unpackb(packb({"action": "auth", "key": "k" * 40, "secret": "s" * 300})) == {"action": "auth", "key": "k" * 40, "secret": "s" * 300}


@pytest.mark.parametrize("data", [b"", b"\xc1", b"\x92\x01", b"\xa3ab", b"\x01\x02", b"\xd9\x05ab", b"\xc7\x01\x01\x00"])
def test_truncated_unknown_and_extra_bytes_are_refused(data):
    with pytest.raises(MsgpackError):
        unpackb(data)


def test_the_timestamp_ext_is_read_in_its_three_sizes_and_other_ext_types_are_refused():
    from datetime import datetime, timezone

    assert unpackb(bytes.fromhex("d6ff") + (1_700_000_000).to_bytes(4, "big")) == datetime.fromtimestamp(1_700_000_000, timezone.utc)
    packed = (123_456_000 << 34 | 1_700_000_000).to_bytes(8, "big")
    assert unpackb(bytes.fromhex("d7ff") + packed) == datetime.fromtimestamp(1_700_000_000, timezone.utc).replace(microsecond=123_456)
    wide = (123_456_000).to_bytes(4, "big") + (1_700_000_000).to_bytes(8, "big", signed=True)
    assert unpackb(bytes.fromhex("c70cff") + wide) == datetime.fromtimestamp(1_700_000_000, timezone.utc).replace(microsecond=123_456)
    for bad in ("d601" + "00000000", "c70cff" + "ffffffff" + "00" * 8, "c70801" + "00" * 8):
        with pytest.raises(MsgpackError):
            unpackb(bytes.fromhex(bad))


def test_a_bomb_of_nested_arrays_is_refused():
    with pytest.raises(MsgpackError):
        unpackb(b"\x91" * 40 + b"\x00")
