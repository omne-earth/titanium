"""The cella vocabulary, hand-carried onto the wire.

cella's control language is ``proto/cella.proto`` in the cella
repository, and its comments state the contract this module leans on:
*"Field numbers are the wire contract: never reuse one."* The engine
seam needs only a sliver of that language -- decode an ``Event`` (is it
a parked ``Operation``, and where does it point?), encode a ``Decision``
(a ``Release`` or a ``Refusal``, by id) -- so the sliver is hand-written
here as plain dataclasses over proto3 wire bytes rather than pulling
``protoc`` codegen and its toolchain into titanium's build.

Every message class exposes ``SerializeToString`` / ``FromString`` with
protobuf-generated semantics, which is the exact duck type grpclib's
``ProtoCodec`` requires, so these classes plug straight into a grpclib
service definition.

Decoding is lenient the way protobuf is lenient: unknown fields are
skipped, not errors, so a newer cella speaking a grown vocabulary (for
example the ``predecessor`` chain on ``Event``) still parses here.
Proto3 presence rules are honored on the encode side: scalar defaults
are omitted, while a set oneof arm is always emitted -- even an empty
embedded message like ``Release`` -- because arm presence *is* the
decision.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from titanium.environments.cella.constants import (
    DIRECTION_OUTGOING,
    I32_WIRE_TYPE,
    I64_WIRE_TYPE,
    LEN_WIRE_TYPE,
    VARINT_WIRE_TYPE,
)

# proto3 wire types. Groups (3 and 4) predate proto3 and cannot appear
# in bytes produced from cella.proto; meeting one means the frame is not
# a cella message at all, so the decoder refuses rather than guesses.

# Operation.Direction: which way the crossing faces.

# Named because the vocabulary speaks ethertypes and ARP is the one a
# policy is most likely to reason about. The allow_internet engine
# grants it nothing: whether ARP ever rides free is a per-task
# cella.policy decision, not a constant's.


class WireError(ValueError):
    """Bytes that do not parse as the cella message they claim to be."""


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise WireError(f"varint cannot carry a negative value: {value}")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Decode one varint at *pos*; return ``(value, next_pos)``."""
    value = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise WireError("truncated varint")
        if shift > 63:
            raise WireError("varint wider than 64 bits")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _tag(field_number: int, wire_type: int) -> bytes:
    return _encode_varint((field_number << 3) | wire_type)


def _varint_field(field_number: int, value: int) -> bytes:
    """One varint field, omitted at its proto3 default of zero."""
    if value == 0:
        return b""
    return _tag(field_number, VARINT_WIRE_TYPE) + _encode_varint(value)


def _len_field(field_number: int, payload: bytes) -> bytes:
    """One length-delimited field (bytes, string, embedded message)."""
    return _tag(field_number, LEN_WIRE_TYPE) + _encode_varint(len(payload)) + payload


def _bytes_field(field_number: int, payload: bytes) -> bytes:
    """A bytes/string field, omitted at its proto3 default of empty."""
    if not payload:
        return b""
    return _len_field(field_number, payload)


def _iter_fields(buf: bytes) -> Iterator[tuple[int, int | bytes]]:
    """Walk a message's fields, yielding ``(field_number, value)``.

    A varint field yields its ``int``; a length-delimited field yields
    its ``bytes``. Fixed32/fixed64 fields -- absent from the cella
    vocabulary but legal wire -- are skipped like any other unknown
    field, by never being yielded.
    """
    pos = 0
    while pos < len(buf):
        key, pos = _decode_varint(buf, pos)
        field_number = key >> 3
        wire_type = key & 0x07
        if field_number == 0:
            raise WireError("field number 0 is not valid protobuf")
        if wire_type == VARINT_WIRE_TYPE:
            value, pos = _decode_varint(buf, pos)
            yield field_number, value
        elif wire_type == LEN_WIRE_TYPE:
            length, pos = _decode_varint(buf, pos)
            if pos + length > len(buf):
                raise WireError("truncated length-delimited field")
            yield field_number, buf[pos : pos + length]
            pos += length
        elif wire_type == I64_WIRE_TYPE:
            pos += 8
        elif wire_type == I32_WIRE_TYPE:
            pos += 4
        else:
            raise WireError(f"unsupported wire type {wire_type}")
        if pos > len(buf):
            raise WireError("truncated fixed-width field")


def _expect_bytes(value: int | bytes, what: str) -> bytes:
    if not isinstance(value, bytes):
        raise WireError(f"{what}: expected length-delimited wire type")
    return value


def _expect_int(value: int | bytes, what: str) -> int:
    if not isinstance(value, int):
        raise WireError(f"{what}: expected varint wire type")
    return value


@dataclass
class Destination:
    """Where an operation points (cella.Destination)."""

    host: str = ""
    ip: bytes = b""
    port: int = 0
    proto: int = 0
    ethertype: int = 0
    mac: bytes = b""

    def SerializeToString(self) -> bytes:
        return (
            _bytes_field(1, self.host.encode())
            + _bytes_field(2, self.ip)
            + _varint_field(3, self.port)
            + _varint_field(4, self.proto)
            + _varint_field(5, self.ethertype)
            + _bytes_field(6, self.mac)
        )

    @classmethod
    def FromString(cls, data: bytes) -> Destination:
        dest = cls()
        for number, value in _iter_fields(data):
            if number == 1:
                dest.host = _expect_bytes(value, "Destination.host").decode()
            elif number == 2:
                dest.ip = _expect_bytes(value, "Destination.ip")
            elif number == 3:
                dest.port = _expect_int(value, "Destination.port")
            elif number == 4:
                dest.proto = _expect_int(value, "Destination.proto")
            elif number == 5:
                dest.ethertype = _expect_int(value, "Destination.ethertype")
            elif number == 6:
                dest.mac = _expect_bytes(value, "Destination.mac")
        return dest


@dataclass
class Operation:
    """One held flow (cella.Operation)."""

    id: bytes = b""
    destination: Destination | None = None
    guest_ns: int = 0
    host_ns: int = 0
    direction: int = DIRECTION_OUTGOING

    def SerializeToString(self) -> bytes:
        out = _bytes_field(1, self.id)
        if self.destination is not None:
            out += _len_field(2, self.destination.SerializeToString())
        out += _varint_field(3, self.guest_ns)
        out += _varint_field(4, self.host_ns)
        out += _varint_field(5, self.direction)
        return out

    @classmethod
    def FromString(cls, data: bytes) -> Operation:
        op = cls()
        for number, value in _iter_fields(data):
            if number == 1:
                op.id = _expect_bytes(value, "Operation.id")
            elif number == 2:
                op.destination = Destination.FromString(
                    _expect_bytes(value, "Operation.destination")
                )
            elif number == 3:
                op.guest_ns = _expect_int(value, "Operation.guest_ns")
            elif number == 4:
                op.host_ns = _expect_int(value, "Operation.host_ns")
            elif number == 5:
                op.direction = _expect_int(value, "Operation.direction")
        return op


@dataclass
class Released:
    """A completed release, on the record (cella.Released)."""

    id: bytes = b""
    first_response_ns: int = 0
    bytes_in: int = 0
    bytes_out: int = 0

    def SerializeToString(self) -> bytes:
        return (
            _bytes_field(1, self.id)
            + _varint_field(2, self.first_response_ns)
            + _varint_field(3, self.bytes_in)
            + _varint_field(4, self.bytes_out)
        )

    @classmethod
    def FromString(cls, data: bytes) -> Released:
        msg = cls()
        for number, value in _iter_fields(data):
            if number == 1:
                msg.id = _expect_bytes(value, "Released.id")
            elif number == 2:
                msg.first_response_ns = _expect_int(value, "Released.first_response_ns")
            elif number == 3:
                msg.bytes_in = _expect_int(value, "Released.bytes_in")
            elif number == 4:
                msg.bytes_out = _expect_int(value, "Released.bytes_out")
        return msg


@dataclass
class Lapsed:
    """A hold that expired unanswered (cella.Lapsed)."""

    id: bytes = b""
    why: str = ""

    def SerializeToString(self) -> bytes:
        return _bytes_field(1, self.id) + _bytes_field(2, self.why.encode())

    @classmethod
    def FromString(cls, data: bytes) -> Lapsed:
        msg = cls()
        for number, value in _iter_fields(data):
            if number == 1:
                msg.id = _expect_bytes(value, "Lapsed.id")
            elif number == 2:
                msg.why = _expect_bytes(value, "Lapsed.why").decode()
        return msg


@dataclass
class Inspected:
    """The operator's recorded look at a held operation (cella.Inspected)."""

    id: bytes = b""

    def SerializeToString(self) -> bytes:
        return _bytes_field(1, self.id)

    @classmethod
    def FromString(cls, data: bytes) -> Inspected:
        msg = cls()
        for number, value in _iter_fields(data):
            if number == 1:
                msg.id = _expect_bytes(value, "Inspected.id")
        return msg


@dataclass
class Event:
    """A ledger entry (cella.Event): exactly one arm is set on the wire."""

    parked: Operation | None = None
    released: Released | None = None
    lapsed: Lapsed | None = None
    inspected: Inspected | None = None

    def SerializeToString(self) -> bytes:
        if self.parked is not None:
            return _len_field(1, self.parked.SerializeToString())
        if self.released is not None:
            return _len_field(2, self.released.SerializeToString())
        if self.lapsed is not None:
            return _len_field(3, self.lapsed.SerializeToString())
        if self.inspected is not None:
            return _len_field(4, self.inspected.SerializeToString())
        return b""

    @classmethod
    def FromString(cls, data: bytes) -> Event:
        event = cls()
        for number, value in _iter_fields(data):
            if number == 1:
                event.parked = Operation.FromString(
                    _expect_bytes(value, "Event.parked")
                )
            elif number == 2:
                event.released = Released.FromString(
                    _expect_bytes(value, "Event.released")
                )
            elif number == 3:
                event.lapsed = Lapsed.FromString(_expect_bytes(value, "Event.lapsed"))
            elif number == 4:
                event.inspected = Inspected.FromString(
                    _expect_bytes(value, "Event.inspected")
                )
            # Field 15 is the tamper-evident predecessor chain; it rides
            # the file wire, not the judgment, so it is skipped with
            # every other field the engine does not speak.
        return event


@dataclass
class Release:
    """The only allow that exists: deliver one named operation, once."""

    def SerializeToString(self) -> bytes:
        return b""

    @classmethod
    def FromString(cls, data: bytes) -> Release:
        for _ in _iter_fields(data):
            pass
        return cls()


@dataclass
class Refusal:
    """A clean in-frame no (cella.Refusal)."""

    why: str = ""

    def SerializeToString(self) -> bytes:
        return _bytes_field(1, self.why.encode())

    @classmethod
    def FromString(cls, data: bytes) -> Refusal:
        msg = cls()
        for number, value in _iter_fields(data):
            if number == 1:
                msg.why = _expect_bytes(value, "Refusal.why").decode()
        return msg


@dataclass
class MembraneMemory:
    """A standing entry for the membrane's memory (cella.MembraneMemory).

    Not a verdict on one park: a memory names a *destination*, and the
    engine plants it so matching crossings need no fresh judgment. Its
    one power is over freezing (``skip_freeze``, outgoing only) -- a
    remembered egress destination waits *live* instead of freezing the
    machine, which is what keeps a TLS handshake's flights inside the
    real peer's patience. ``keep_open`` is a policy window in seconds;
    ``written`` is left zero for the engine to send -- the bridge
    stamps it with the host clock as the entry lands, and expiry is
    ``written + keep_open``, absolute.
    """

    destination: Destination | None = None
    skip_freeze: bool = False
    keep_open: int = 0
    written: int = 0

    def SerializeToString(self) -> bytes:
        out = b""
        if self.destination is not None:
            out += _len_field(1, self.destination.SerializeToString())
        out += _varint_field(2, 1 if self.skip_freeze else 0)
        out += _varint_field(3, self.keep_open)
        out += _varint_field(4, self.written)
        return out

    @classmethod
    def FromString(cls, data: bytes) -> MembraneMemory:
        msg = cls()
        for number, value in _iter_fields(data):
            if number == 1:
                msg.destination = Destination.FromString(
                    _expect_bytes(value, "MembraneMemory.destination")
                )
            elif number == 2:
                msg.skip_freeze = _expect_int(value, "MembraneMemory.skip_freeze") != 0
            elif number == 3:
                msg.keep_open = _expect_int(value, "MembraneMemory.keep_open")
            elif number == 4:
                msg.written = _expect_int(value, "MembraneMemory.written")
        return msg


@dataclass
class Decision:
    """The external word on one operation (cella.Decision).

    Three arms, one oneof: ``release`` and ``refusal`` answer a park by
    id; ``membrane_memory`` plants a standing entry (its id is empty --
    a memory names a destination, not an operation).
    """

    id: bytes = b""
    release: Release | None = None
    refusal: Refusal | None = None
    membrane_memory: MembraneMemory | None = None

    def SerializeToString(self) -> bytes:
        out = _bytes_field(1, self.id)
        # A oneof arm is emitted whenever it is set: the empty Release
        # still crosses as ``tag, length 0`` -- its presence is the verdict.
        if self.release is not None:
            out += _len_field(2, self.release.SerializeToString())
        elif self.refusal is not None:
            out += _len_field(3, self.refusal.SerializeToString())
        elif self.membrane_memory is not None:
            out += _len_field(4, self.membrane_memory.SerializeToString())
        return out

    @classmethod
    def FromString(cls, data: bytes) -> Decision:
        decision = cls()
        for number, value in _iter_fields(data):
            if number == 1:
                decision.id = _expect_bytes(value, "Decision.id")
            elif number == 2:
                _expect_bytes(value, "Decision.release")
                decision.release = Release()
            elif number == 3:
                decision.refusal = Refusal.FromString(
                    _expect_bytes(value, "Decision.refusal")
                )
            elif number == 4:
                decision.membrane_memory = MembraneMemory.FromString(
                    _expect_bytes(value, "Decision.membrane_memory")
                )
        return decision
