"""Low-level byte helpers: big-endian integers, variable-length quantities, chunks.

Implements the encoding rules of Standard MIDI Files 1.1 (File Structure,
Variable-Length Quantity).
"""

from __future__ import annotations

from typing import Optional

from .errors import ParseError

#: Largest value a MIDI variable-length quantity may encode (4 bytes, 28 bits).
MAX_VLQ = 0x0FFFFFFF


class ByteReader:
    """Bounds-checked big-endian reader over a bytes buffer."""

    __slots__ = ("data", "pos", "name")

    def __init__(self, data: bytes, pos: int = 0, name: str = "") -> None:
        self.data = data
        self.pos = pos
        self.name = name

    # -- introspection -------------------------------------------------
    def __len__(self) -> int:
        return len(self.data)

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos

    @property
    def eof(self) -> bool:
        return self.pos >= len(self.data)

    def _where(self) -> str:
        return (" in %s" % self.name) if self.name else ""

    def tell(self) -> int:
        return self.pos

    # -- raw -----------------------------------------------------------
    def read_bytes(self, n: int) -> bytes:
        if n < 0:
            raise ParseError("negative read length", length=n)
        end = self.pos + n
        if end > len(self.data):
            raise ParseError(
                "unexpected end of data%s: wanted %d byte(s), %d available"
                % (self._where(), n, self.remaining),
                offset=self.pos,
                wanted=n,
                available=self.remaining,
            )
        out = self.data[self.pos:end]
        self.pos = end
        return out

    def peek_u8(self) -> Optional[int]:
        if self.eof:
            return None
        return self.data[self.pos]

    def u8(self) -> int:
        if self.pos >= len(self.data):
            raise ParseError("unexpected end of data%s" % self._where(), offset=self.pos)
        b = self.data[self.pos]
        self.pos += 1
        return b

    def u16(self) -> int:
        b = self.read_bytes(2)
        return (b[0] << 8) | b[1]

    def u24(self) -> int:
        b = self.read_bytes(3)
        return (b[0] << 16) | (b[1] << 8) | b[2]

    def u32(self) -> int:
        b = self.read_bytes(4)
        return (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]

    # -- variable length quantity --------------------------------------
    def read_vlq(self, strict: bool = True) -> int:
        """Read a variable-length quantity (1-4 bytes per spec).

        With strict=False, over-long encodings produced by some sequencers
        are tolerated instead of raising.
        """
        value = 0
        count = 0
        while True:
            if self.pos >= len(self.data):
                raise ParseError("truncated variable-length quantity%s" % self._where(), offset=self.pos)
            b = self.data[self.pos]
            self.pos += 1
            count += 1
            value = (value << 7) | (b & 0x7F)
            if not b & 0x80:
                break
            if count >= 4 and strict:
                raise ParseError(
                    "variable-length quantity longer than 4 bytes%s" % self._where(),
                    offset=self.pos - count,
                )
            if count >= 8:
                raise ParseError("variable-length quantity longer than 8 bytes", offset=self.pos - count)
        return value


class ByteWriter:
    """Append-only big-endian writer."""

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def __len__(self) -> int:
        return len(self._buf)

    def getvalue(self) -> bytes:
        return bytes(self._buf)

    def write_bytes(self, data) -> None:
        self._buf += data

    def u8(self, value: int) -> None:
        self._buf.append(value & 0xFF)

    def u16(self, value: int) -> None:
        self._buf.append((value >> 8) & 0xFF)
        self._buf.append(value & 0xFF)

    def u24(self, value: int) -> None:
        self._buf.append((value >> 16) & 0xFF)
        self._buf.append((value >> 8) & 0xFF)
        self._buf.append(value & 0xFF)

    def u32(self, value: int) -> None:
        self._buf.append((value >> 24) & 0xFF)
        self._buf.append((value >> 16) & 0xFF)
        self._buf.append((value >> 8) & 0xFF)
        self._buf.append(value & 0xFF)

    def vlq(self, value: int) -> None:
        self._buf += vlq_bytes(value)

    def chunk(self, chunk_type: bytes, payload: bytes) -> None:
        self._buf += chunk_bytes(chunk_type, payload)


def vlq_bytes(value: int) -> bytes:
    """Encode value as a MIDI variable-length quantity (1-4 bytes)."""
    if value < 0:
        raise ValueError("variable-length quantity cannot be negative: %r" % (value,))
    if value > MAX_VLQ:
        raise ValueError("value %d exceeds maximum VLQ (0x0FFFFFFF)" % value)
    buf = bytearray([value & 0x7F])
    value >>= 7
    while value:
        buf.append(0x80 | (value & 0x7F))
        value >>= 7
    buf.reverse()
    return bytes(buf)


def chunk_bytes(chunk_type: bytes, payload: bytes) -> bytes:
    """Build a RIFF-style chunk: 4-byte type, 4-byte big-endian length, payload."""
    if len(chunk_type) != 4:
        raise ValueError("chunk type must be exactly 4 bytes: %r" % (chunk_type,))
    out = bytearray(chunk_type)
    length = len(payload)
    out += bytes(((length >> 24) & 0xFF, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF))
    out += payload
    return bytes(out)


def hex_bytes(data: bytes, sep: str = " ") -> str:
    """Lower-case hex representation, e.g. 'f0 43 12 f7'."""
    return sep.join("%02x" % b for b in data)


def bytes_from_hex(text: str) -> bytes:
    """Parse 'f043 12f7' / 'f0,43,12' / '[240, 67]' style input."""
    cleaned = text.strip()
    decimal = cleaned.startswith("[") and cleaned.endswith("]")
    if decimal:
        cleaned = cleaned[1:-1].replace(",", " ")
        try:
            return bytes(bytearray(int(tok) & 0xFF for tok in cleaned.split()))
        except ValueError:
            raise ValueError("bad decimal byte list: %r" % (text,))
    cleaned = cleaned.replace(",", " ").replace("0x", " ").replace("0X", " ")
    tokens = [t for t in cleaned.replace("\\x", " ").split() if t]
    out = bytearray()
    if len(tokens) == 1 and len(tokens[0]) > 2 and len(tokens[0]) % 2 == 0:
        tokens = [tokens[0][i : i + 2] for i in range(0, len(tokens[0]), 2)]
    for tok in tokens:
        if len(tok) > 2:
            raise ValueError("bad hex byte %r" % (tok,))
        out.append(int(tok, 16))
    return bytes(out)
