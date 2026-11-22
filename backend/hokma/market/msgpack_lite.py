"""The part of MessagePack that the Alpaca option stream needs (SPEC-016 5.2): the stream speaks msgpack only (A2, error 412 otherwise). It is
written here, in a page, so that Hokma gets no new dependency. Types: nil, bool, int, float (32 and 64 bit read, 64 written), str, bin, array, map, and the timestamp ext (type -1, read as a UTC datetime).
Anything else (other ext types) is refused with `MsgpackError`, never guessed.
"""
import struct
from datetime import datetime, timezone


class MsgpackError(ValueError):
    pass


def packb(value) -> bytes:
    out = bytearray()
    _pack(value, out)
    return bytes(out)


def _pack(value, out: bytearray) -> None:
    if value is None:
        out.append(0xC0)
    elif value is True:
        out.append(0xC3)
    elif value is False:
        out.append(0xC2)
    elif isinstance(value, int):
        _pack_int(value, out)
    elif isinstance(value, float):
        out += b"\xcb" + struct.pack(">d", value)
    elif isinstance(value, str):
        data = value.encode("utf-8")
        n = len(data)
        out += bytes([0xA0 | n]) if n < 32 else b"\xd9" + bytes([n]) if n < 256 else b"\xda" + struct.pack(">H", n) if n < 65536 else b"\xdb" + struct.pack(">I", n)
        out += data
    elif isinstance(value, (bytes, bytearray)):
        n = len(value)
        out += b"\xc4" + bytes([n]) if n < 256 else b"\xc5" + struct.pack(">H", n) if n < 65536 else b"\xc6" + struct.pack(">I", n)
        out += bytes(value)
    elif isinstance(value, (list, tuple)):
        n = len(value)
        out += bytes([0x90 | n]) if n < 16 else b"\xdc" + struct.pack(">H", n) if n < 65536 else b"\xdd" + struct.pack(">I", n)
        for item in value:
            _pack(item, out)
    elif isinstance(value, dict):
        n = len(value)
        out += bytes([0x80 | n]) if n < 16 else b"\xde" + struct.pack(">H", n) if n < 65536 else b"\xdf" + struct.pack(">I", n)
        for key, item in value.items():
            _pack(key, out)
            _pack(item, out)
    else:
        raise MsgpackError("cannot pack " + type(value).__name__)


def _pack_int(value: int, out: bytearray) -> None:
    if 0 <= value < 128:
        out.append(value)
    elif -32 <= value < 0:
        out.append(value & 0xFF)
    elif 0 <= value < 256:
        out += b"\xcc" + bytes([value])
    elif 0 <= value < 65536:
        out += b"\xcd" + struct.pack(">H", value)
    elif 0 <= value < 2**32:
        out += b"\xce" + struct.pack(">I", value)
    elif 0 <= value < 2**64:
        out += b"\xcf" + struct.pack(">Q", value)
    elif -128 <= value < 0:
        out += b"\xd0" + struct.pack(">b", value)
    elif -32768 <= value < 0:
        out += b"\xd1" + struct.pack(">h", value)
    elif -(2**31) <= value < 0:
        out += b"\xd2" + struct.pack(">i", value)
    elif -(2**63) <= value < 0:
        out += b"\xd3" + struct.pack(">q", value)
    else:
        raise MsgpackError("integer out of range")


def unpackb(data: bytes):
    value, end = _unpack(memoryview(data), 0, 0)
    if end != len(data):
        raise MsgpackError("trailing bytes")
    return value


MAX_DEPTH = 16


def _take(view, at: int, n: int) -> memoryview:
    if at + n > len(view):
        raise MsgpackError("truncated")
    return view[at:at + n]


def _unpack(view, at: int, depth: int):
    if depth > MAX_DEPTH:
        raise MsgpackError("too deep")
    head = _take(view, at, 1)[0]
    at += 1
    if head < 0x80:
        return head, at
    if head >= 0xE0:
        return head - 256, at
    if 0xA0 <= head < 0xC0:
        return _str(view, at, head & 0x1F)
    if 0x90 <= head < 0xA0:
        return _array(view, at, head & 0x0F, depth)
    if 0x80 <= head < 0x90:
        return _map(view, at, head & 0x0F, depth)
    fixed = {0xC0: None, 0xC2: False, 0xC3: True}
    if head in fixed:
        return fixed[head], at
    if head in (0xD6, 0xD7, 0xC7):                       # ext types: only the timestamp (type -1) is known
        return _timestamp(view, at, head)
    numbers = {0xCC: ">B", 0xCD: ">H", 0xCE: ">I", 0xCF: ">Q", 0xD0: ">b", 0xD1: ">h", 0xD2: ">i", 0xD3: ">q", 0xCA: ">f", 0xCB: ">d"}
    if head in numbers:
        size = struct.calcsize(numbers[head])
        return struct.unpack(numbers[head], _take(view, at, size))[0], at + size
    lengths = {0xD9: ">B", 0xDA: ">H", 0xDB: ">I", 0xC4: ">B", 0xC5: ">H", 0xC6: ">I", 0xDC: ">H", 0xDD: ">I", 0xDE: ">H", 0xDF: ">I"}
    if head in lengths:
        size = struct.calcsize(lengths[head])
        n = struct.unpack(lengths[head], _take(view, at, size))[0]
        at += size
        if head in (0xD9, 0xDA, 0xDB):
            return _str(view, at, n)
        if head in (0xC4, 0xC5, 0xC6):
            return bytes(_take(view, at, n)), at + n
        return _array(view, at, n, depth) if head in (0xDC, 0xDD) else _map(view, at, n, depth)
    raise MsgpackError("unsupported type 0x%02x" % head)


def _timestamp(view, at: int, head: int):
    """MessagePack timestamp (ext type -1): 32 bit seconds, 64 bit (30 bit nanoseconds and 34 bit seconds) or 96 bit. Returned as a UTC `datetime`."""
    size = 12 if head == 0xC7 else 4 if head == 0xD6 else 8
    if head == 0xC7:
        if _take(view, at, 1)[0] != 12:
            raise MsgpackError("unsupported ext length")
        at += 1
    if struct.unpack(">b", _take(view, at, 1))[0] != -1:
        raise MsgpackError("unsupported ext type")
    body = bytes(_take(view, at + 1, size))
    if size == 4:
        seconds, nanos = struct.unpack(">I", body)[0], 0
    elif size == 8:
        packed = struct.unpack(">Q", body)[0]
        seconds, nanos = packed & 0x3FFFFFFFF, packed >> 34
    else:
        nanos, seconds = struct.unpack(">Iq", body)
    if nanos > 999_999_999:
        raise MsgpackError("bad timestamp")
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=nanos // 1000), at + 1 + size
    except (OverflowError, OSError, ValueError):
        raise MsgpackError("bad timestamp") from None


def _str(view, at: int, n: int):
    try:
        return bytes(_take(view, at, n)).decode("utf-8"), at + n
    except UnicodeDecodeError:
        raise MsgpackError("bad utf-8") from None


def _array(view, at: int, n: int, depth: int):
    out = []
    for _ in range(n):
        item, at = _unpack(view, at, depth + 1)
        out.append(item)
    return out, at


def _map(view, at: int, n: int, depth: int):
    out = {}
    for _ in range(n):
        key, at = _unpack(view, at, depth + 1)
        value, at = _unpack(view, at, depth + 1)
        try:
            out[key] = value
        except TypeError:
            raise MsgpackError("unhashable key") from None
    return out, at
