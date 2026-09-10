"""Unit tests for the cella policy engine (the ``allow_internet`` judge).

Three layers, matching the module split:

1. The wire vocabulary: the golden byte strings here were produced by
   ``protoc`` from cella's own ``proto/cella.proto`` and cross-checked
   byte-for-byte against this codec, so they pin the wire contract
   without pulling protoc into the test run.
2. The policy: ``allow_internet`` as a pure function over operations.
3. The seam: a real grpclib client streaming Events to a served engine
   on a loopback ephemeral port and reading Decisions back.
"""

import asyncio

import pytest
from grpclib.client import Channel
from grpclib.const import Cardinality

from titanium.environments.cella.engine import (
    DECIDE_METHOD,
    REFUSAL_WHY_DISABLED,
    REFUSAL_WHY_NO_POLICY,
    AllowInternetPolicy,
    bound_port,
    serve,
)
from titanium.environments.cella.wire import (
    DIRECTION_INCOMING,
    ETHERTYPE_ARP,
    Decision,
    Destination,
    Event,
    Operation,
    Refusal,
    Release,
    WireError,
)

# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------

# One fully populated parked Event, as protoc encodes it.
_PARKED_EVENT = Event(
    parked=Operation(
        id=b"\x01" * 16,
        destination=Destination(
            host="pypi.org",
            ip=bytes([151, 101, 0, 223]),
            port=443,
            proto=6,
            ethertype=0x0800,
            mac=b"\xaa\xbb\xcc\xdd\xee\xff",
        ),
        guest_ns=123456789012345,
        host_ns=98765,
        direction=DIRECTION_INCOMING,
    )
)
_PARKED_EVENT_BYTES = bytes.fromhex(
    "0a420a10010101010101010101010101010101011220"
    "0a08707970692e6f72671204976500df18bb0320062880103206aabbccddeeff"
    "18f9beb7b088891c20cd83062801"
)


def test_parked_event_encodes_like_protoc():
    assert _PARKED_EVENT.SerializeToString() == _PARKED_EVENT_BYTES


def test_parked_event_round_trips():
    assert Event.FromString(_PARKED_EVENT_BYTES) == _PARKED_EVENT


def test_release_decision_encodes_like_protoc():
    decision = Decision(id=b"\xab\xcd", release=Release())
    # The empty Release arm still crosses as ``tag, length 0``: its
    # presence is the verdict.
    assert decision.SerializeToString() == bytes.fromhex("0a02abcd1200")
    assert Decision.FromString(decision.SerializeToString()) == decision


def test_refusal_decision_encodes_like_protoc():
    decision = Decision(id=b"\xab\xcd", refusal=Refusal(why=REFUSAL_WHY_DISABLED))
    assert decision.SerializeToString() == bytes.fromhex(
        "0a02abcd1a270a25616c6c6f775f696e7465726e65742069732066616c736520"
        "666f722074686973207461736b"
    )
    assert Decision.FromString(decision.SerializeToString()) == decision


def test_unknown_fields_are_skipped():
    # The predecessor chain (Event field 15, bytes) rides the file wire;
    # the engine must parse around it. Also append a varint and a
    # fixed64 under field numbers the vocabulary does not speak.
    grown = (
        _PARKED_EVENT_BYTES
        + bytes.fromhex("7a03aabbcc")  # field 15, length-delimited
        + bytes.fromhex("a00602")  # field 100, varint
        + bytes.fromhex("a9060102030405060708")  # field 101, fixed64
    )
    assert Event.FromString(grown) == _PARKED_EVENT


def test_non_parked_arms_decode():
    # Event { released: Released {} } is exactly ``field 2, length 0``.
    event = Event.FromString(bytes.fromhex("1200"))
    assert event.parked is None
    assert event.released is not None


def test_truncated_bytes_are_refused():
    with pytest.raises(WireError):
        Event.FromString(_PARKED_EVENT_BYTES[:-1])


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------


def _operation(ethertype: int = 0x0800, direction: int = 0) -> Operation:
    return Operation(
        id=b"\x07" * 16,
        destination=Destination(
            ip=bytes([93, 184, 216, 34]), port=443, ethertype=ethertype
        ),
        direction=direction,
    )


def test_allow_internet_true_still_refuses_without_a_policy():
    # The flag is a gate, not a grant: internet is a possibility, but a
    # release needs a granting cella.policy, and none is loaded yet.
    decision = AllowInternetPolicy(allow_internet=True).decide(_operation())
    assert decision.id == b"\x07" * 16
    assert decision.release is None
    assert decision.refusal is not None
    assert decision.refusal.why == REFUSAL_WHY_NO_POLICY


def test_allow_internet_false_refuses_with_a_why():
    decision = AllowInternetPolicy(allow_internet=False).decide(_operation())
    assert decision.release is None
    assert decision.refusal is not None
    assert decision.refusal.why == REFUSAL_WHY_DISABLED


def test_allow_internet_false_refuses_incoming_too():
    decision = AllowInternetPolicy(allow_internet=False).decide(
        _operation(direction=DIRECTION_INCOMING)
    )
    assert decision.refusal is not None


def test_arp_is_refused_like_everything_else():
    # Unlike cella's motor fixture, there is no ARP carve-out: under
    # allow_internet=false the machine stays fully dark, and any finer
    # exception belongs to the per-task cella.policy file.
    decision = AllowInternetPolicy(allow_internet=False).decide(
        _operation(ethertype=ETHERTYPE_ARP)
    )
    assert decision.release is None
    assert decision.refusal is not None


def test_destinationless_operation_follows_the_flag():
    bare = Operation(id=b"\x09")
    assert AllowInternetPolicy(True).decide(bare).refusal.why == REFUSAL_WHY_NO_POLICY
    assert AllowInternetPolicy(False).decide(bare).refusal.why == REFUSAL_WHY_DISABLED


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


async def _decide(policy: AllowInternetPolicy, events: list[Event]) -> list[Decision]:
    """Serve *policy* on loopback, stream *events*, return every Decision."""
    server = await serve(policy)
    channel = Channel("127.0.0.1", bound_port(server))
    try:
        stream = channel.request(
            DECIDE_METHOD, Cardinality.STREAM_STREAM, Event, Decision
        )
        async with stream:
            for event in events:
                await stream.send_message(event)
            await stream.end()
            decisions = []
            while (decision := await stream.recv_message()) is not None:
                decisions.append(decision)
            return decisions
    finally:
        channel.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_engine_open_gate_refuses_without_a_policy_over_the_wire():
    decisions = await asyncio.wait_for(
        _decide(AllowInternetPolicy(allow_internet=True), [_PARKED_EVENT]),
        timeout=10,
    )
    assert len(decisions) == 1
    assert decisions[0].id == b"\x01" * 16
    assert decisions[0].release is None
    assert decisions[0].refusal.why == REFUSAL_WHY_NO_POLICY


@pytest.mark.asyncio
async def test_engine_refuses_over_the_wire_and_skips_evidence():
    parked = Event(parked=_operation())
    arp = Event(parked=_operation(ethertype=ETHERTYPE_ARP))
    evidence = Event.FromString(bytes.fromhex("1200"))  # a Released event
    decisions = await asyncio.wait_for(
        _decide(AllowInternetPolicy(allow_internet=False), [parked, evidence, arp]),
        timeout=10,
    )
    # Completions are evidence, not questions: two parks, two decisions,
    # in park order -- and both refused, ARP included.
    assert len(decisions) == 2
    for decision in decisions:
        assert decision.release is None
        assert decision.refusal is not None
        assert decision.refusal.why == REFUSAL_WHY_DISABLED
