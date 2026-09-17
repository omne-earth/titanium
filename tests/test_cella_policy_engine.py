"""Unit tests for the cella policy engine (the ``cella.policy`` judge).

Three layers, matching the module split:

1. The wire vocabulary: the golden byte strings here were produced by
   ``protoc`` from cella's own ``proto/cella.proto`` and cross-checked
   byte-for-byte against this codec, so they pin the wire contract
   without pulling protoc into the test run.
2. The policy: the ``cella.policy`` grants as a pure function over
   operations. ``allow_internet`` appears nowhere: it is harbor's
   topology knob, spent before an engine exists.
3. The seam: a real grpclib client streaming Events to a served engine
   on a loopback ephemeral port and reading Decisions back.
"""

import argparse
import asyncio

import pytest
from grpclib.client import Channel
from grpclib.const import Cardinality
from grpclib.server import Server

from titanium.environments.cella.engine import (
    DECIDE_METHOD,
    REFUSAL_WHY_NO_GRANT,
    PolicyJudge,
    _parse_listen,
    _run,
    bound_port,
    main,
    serve,
)
from titanium.environments.cella.policy import (
    Grant,
    Policy,
    PolicyError,
    PolicyRecorder,
    grant_for,
)
from titanium.environments.cella.wire import (
    DIRECTION_INCOMING,
    ETHERTYPE_ARP,
    Decision,
    Destination,
    Event,
    Inspected,
    Lapsed,
    Operation,
    Refusal,
    Release,
    Released,
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
    # The why is any string on the wire; this one is 37 bytes long and
    # pins the golden encoding below.
    decision = Decision(
        id=b"\xab\xcd", refusal=Refusal(why="allow_internet is false for this task")
    )
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


def _verdict(judge, operation):
    """The verdict decision (the memory, if any, follows it)."""
    return judge.decide(operation)[0]


def test_a_policyless_border_refuses_everything():
    # A border with no cella.policy behind it grants nothing, both
    # directions.
    for direction in (0, DIRECTION_INCOMING):
        decision = _verdict(PolicyJudge(), _operation(direction=direction))
        assert decision.id == b"\x07" * 16
        assert decision.release is None
        assert decision.refusal.why == REFUSAL_WHY_NO_GRANT


def test_arp_is_refused_like_everything_else():
    # Unlike cella's motor fixture, there is no ARP carve-out: any
    # finer exception belongs to the per-task cella.policy file.
    decision = _verdict(PolicyJudge(), _operation(ethertype=ETHERTYPE_ARP))
    assert decision.release is None
    assert decision.refusal is not None


def test_a_destinationless_operation_is_refused():
    decision = _verdict(PolicyJudge(), Operation(id=b"\x09"))
    assert decision.refusal.why == REFUSAL_WHY_NO_GRANT


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


async def _decide(policy: PolicyJudge, events: list[Event]) -> list[Decision]:
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
async def test_a_policyless_engine_refuses_over_the_wire():
    decisions = await asyncio.wait_for(
        _decide(PolicyJudge(), [_PARKED_EVENT]),
        timeout=10,
    )
    assert len(decisions) == 1
    assert decisions[0].id == b"\x01" * 16
    assert decisions[0].release is None
    assert decisions[0].refusal.why == REFUSAL_WHY_NO_GRANT


@pytest.mark.asyncio
async def test_engine_refuses_over_the_wire_and_skips_evidence():
    parked = Event(parked=_operation())
    arp = Event(parked=_operation(ethertype=ETHERTYPE_ARP))
    evidence = Event.FromString(bytes.fromhex("1200"))  # a Released event
    decisions = await asyncio.wait_for(
        _decide(PolicyJudge(), [parked, evidence, arp]),
        timeout=10,
    )
    # Completions are evidence, not questions: two parks, two decisions,
    # in park order -- and both refused, ARP included.
    assert len(decisions) == 2
    for decision in decisions:
        assert decision.release is None
        assert decision.refusal is not None
        assert decision.refusal.why == REFUSAL_WHY_NO_GRANT


# ---------------------------------------------------------------------------
# The cella.policy file
# ---------------------------------------------------------------------------


_POLICY_TEXT = """\
# a comment, and a blank line below

release outgoing 140.82.112.3:443/tcp (keep_open=5m) (skip_freeze=true)
release incoming *:2222/tcp
release outgoing 10.0.0.1:*/udp
release outgoing arp (keep_open=24h) (skip_freeze=true)
"""


def test_policy_parses_and_renders_canonically():
    policy = Policy.parse(_POLICY_TEXT)
    assert len(policy.grants) == 4
    rendered = Policy.parse(policy.render())
    assert set(rendered.grants) == set(policy.grants)


@pytest.mark.parametrize(
    "line",
    [
        "deny outgoing 1.2.3.4:443/tcp",  # not a verb
        "release sideways 1.2.3.4:443/tcp",  # not a direction
        "release outgoing 1.2.3:443/tcp",  # short ip
        "release outgoing 1.2.3.4:70000/tcp",  # port range
        "release outgoing 1.2.3.4:443/xtp",  # bad proto
        "release outgoing bogus",  # bad ethertype
        "release outgoing",  # missing dest
        "release outgoing 1.2.3.4:443/tcp (keep_open=nope)",  # bad window
        "release incoming 1.2.3.4:443/tcp (skip_freeze=true)",  # skip on ingress
        "release outgoing 1.2.3.4:443/tcp (skip_freeze=true)",  # skip w/o window
        'release outgoing 1.2.3.4:443/tcp (reason="x")',  # reason on release
        "release outgoing 1.2.3.4:443/tcp (bogus=1)",  # unknown key
    ],
)
def test_policy_refuses_unreadable_lines(line):
    with pytest.raises(PolicyError):
        Policy.parse(line)


def _op(
    ip=(140, 82, 112, 3),
    port=443,
    proto=6,
    ethertype=0x0800,
    direction=0,
    host="",
) -> Operation:
    return Operation(
        id=b"\x21" * 16,
        destination=Destination(
            host=host, ip=bytes(ip), port=port, proto=proto, ethertype=ethertype
        ),
        direction=direction,
    )


def _grants(policy, operation):
    return policy.evaluate(operation).release


def test_a_host_grant_matches_the_resolved_name_not_the_ip():
    # The terminator stamps the resolved name on the crossing; a host
    # grant releases by that name, so a rotated ip is a non-issue.
    policy = Policy.parse(
        "release outgoing deb.debian.org:80/tcp (keep_open=5m) (skip_freeze=true)\n"
    )
    # Same name, two different (CDN-rotated) ips: both released.
    assert _grants(policy, _op(ip=(1, 2, 3, 4), port=80, host="deb.debian.org"))
    assert _grants(policy, _op(ip=(9, 9, 9, 9), port=80, host="deb.debian.org"))
    # A crossing to a different name is refused.
    assert not _grants(policy, _op(ip=(1, 2, 3, 4), port=80, host="evil.example"))
    # An unnamed (ip-only) crossing never matches a host grant: fail closed.
    assert not _grants(policy, _op(ip=(1, 2, 3, 4), port=80, host=""))
    # The port and proto still bind.
    assert not _grants(policy, _op(port=443, host="deb.debian.org"))


def test_a_leading_dot_host_grant_matches_subdomains():
    policy = Policy.parse("release outgoing .pythonhosted.org:443/tcp\n")
    assert _grants(policy, _op(host="files.pythonhosted.org"))
    assert _grants(policy, _op(host="pythonhosted.org"))  # the bare domain too
    assert not _grants(policy, _op(host="pythonhosted.org.evil.com"))
    assert not _grants(policy, _op(host="notpythonhosted.org"))


def test_a_host_grant_round_trips_through_its_line():
    grant = Policy.parse(
        "release outgoing deb.debian.org:80/tcp (keep_open=5m)\n"
    ).grants[0]
    assert grant.host == "deb.debian.org" and grant.ip == "*"
    assert grant.line() == "release outgoing deb.debian.org:80/tcp (keep_open=5m)"


def test_a_bare_word_is_not_a_host():
    # No dot: not a host, not an ip, not '*' -- a strict parse error,
    # never a silently-dropped rule.
    with pytest.raises(PolicyError):
        Policy.parse("release outgoing localhost:80/tcp\n")


def test_grants_match_exactly_and_by_wildcard():
    policy = Policy.parse(_POLICY_TEXT)
    assert _grants(policy, _op())
    assert not _grants(policy, _op(port=80))
    assert not _grants(policy, _op(proto=17))
    # The incoming grant: any source, port 2222, tcp.
    assert _grants(
        policy, _op(ip=(8, 8, 8, 8), port=2222, direction=DIRECTION_INCOMING)
    )
    assert not _grants(policy, _op(port=2222))  # wrong direction
    # The port wildcard.
    assert _grants(policy, _op(ip=(10, 0, 0, 1), port=9999, proto=17))
    # The L2 grant speaks only for its ethertype.
    arp = Operation(
        id=b"\x22",
        destination=Destination(ethertype=ETHERTYPE_ARP, mac=b"\xff" * 6),
    )
    assert _grants(policy, arp)
    ipv6 = Operation(
        id=b"\x23", destination=Destination(ethertype=0x86DD, mac=b"\xff" * 6)
    )
    assert not _grants(policy, ipv6)


def test_grant_for_names_the_crossing_exactly():
    assert grant_for(_op()) == Grant(
        verb="release", direction="outgoing", ip="140.82.112.3", port=443, proto=6
    )
    arp = Operation(
        id=b"\x22",
        destination=Destination(ethertype=ETHERTYPE_ARP, mac=b"\xff" * 6),
    )
    assert grant_for(arp) == Grant(
        verb="release", direction="outgoing", ethertype=ETHERTYPE_ARP
    )
    assert grant_for(Operation(id=b"\x24")) is None


def test_enforce_releases_granted_and_refuses_the_rest():
    policy = Policy.parse(_POLICY_TEXT)
    judge = PolicyJudge(policy=policy)
    assert _verdict(judge, _op()).release is not None
    refused = _verdict(judge, _op(port=80))
    assert refused.refusal is not None
    assert refused.refusal.why == REFUSAL_WHY_NO_GRANT


def test_dry_run_releases_everything_and_collects_the_policy(tmp_path):
    path = tmp_path / "cella.policy"
    judge = PolicyJudge(recorder=PolicyRecorder(path))
    crossings = [
        _op(),
        _op(),  # a repeat collapses into one grant
        _op(ip=(8, 8, 8, 8), port=53, proto=17),
        Operation(
            id=b"\x22",
            destination=Destination(ethertype=ETHERTYPE_ARP, mac=b"\xff" * 6),
        ),
    ]
    for crossing in crossings:
        assert _verdict(judge, crossing).release is not None

    collected = Policy.load(path)
    assert len(collected.grants) == 3
    # The round trip that makes dry-run useful: enforcing the collected
    # file releases exactly what was observed and refuses the rest.
    enforcing = PolicyJudge(policy=collected)
    for crossing in crossings:
        assert _verdict(enforcing, crossing).release is not None
    assert _verdict(enforcing, _op(port=80)).refusal is not None


def test_dry_run_accumulates_across_recorders(tmp_path):
    # Successive dry runs collect one cohesive policy: a second
    # recorder seeds from the file, keeps what the first observed, and
    # adds only new destinations -- so an oracle run and an agent run
    # can collect together instead of each discarding the last.
    path = tmp_path / "cella.policy"
    first = PolicyRecorder(path)
    first.record(_op())
    second = PolicyRecorder(path)
    second.record(_op(ip=(8, 8, 8, 8), port=53, proto=17))
    collected = Policy.load(path)
    assert len(collected.grants) == 2

    # A groomed grant holds its destination: re-observing it bare adds
    # no duplicate line.
    path.write_text(
        "release outgoing 1.2.3.4:443/tcp (keep_open=60m) (skip_freeze=true)\n"
    )
    third = PolicyRecorder(path)
    third.record(_op(ip=(1, 2, 3, 4), port=443, proto=6))
    groomed = Policy.load(path)
    assert len(groomed.grants) == 1
    assert next(iter(groomed.grants)).keep_open > 0


def test_dry_run_plants_skip_freeze_memory_once_per_outgoing_destination(tmp_path):
    # Collection must not freeze on every frame: the recorder plants a
    # skip_freeze memory the first time it sees each outgoing destination,
    # so a repeat waits live. Otherwise a slow-thaw host wedges mid-collect.
    judge = PolicyJudge(recorder=PolicyRecorder(tmp_path / "cella.policy"))

    first = judge.decide(_op())
    assert first[0].release is not None
    memory = first[1].membrane_memory
    assert memory.skip_freeze is True
    assert memory.keep_open > 0
    assert memory.destination == _op().destination

    # The same destination again rides alone -- already remembered.
    assert len(judge.decide(_op())) == 1
    # A new destination plants its own memory.
    assert len(judge.decide(_op(ip=(8, 8, 8, 8), port=53, proto=17))) == 2
    # An incoming crossing never plants (skip_freeze is outgoing-only).
    assert len(judge.decide(_op(port=8080, direction=DIRECTION_INCOMING))) == 1


# ---------------------------------------------------------------------------
# The wire's remaining arms and refusals
# ---------------------------------------------------------------------------


def test_lapsed_and_inspected_round_trip():
    lapsed = Event(lapsed=Lapsed(id=b"\x31", why="gave up"))
    assert Event.FromString(lapsed.SerializeToString()) == lapsed
    inspected = Event(inspected=Inspected(id=b"\x32"))
    assert Event.FromString(inspected.SerializeToString()) == inspected
    released = Event(
        released=Released(id=b"\x33", first_response_ns=1, bytes_in=2, bytes_out=3)
    )
    assert Event.FromString(released.SerializeToString()) == released


def test_release_body_skips_unknown_fields():
    # Release is empty on purpose; a grown one still parses.
    assert Release.FromString(bytes.fromhex("0801")) == Release()


@pytest.mark.parametrize(
    "data",
    [
        bytes.fromhex("0b"),  # field 1, wire type 3: a group
        bytes.fromhex("00"),  # field number 0
        bytes.fromhex("0a05abcd"),  # length-delimited, truncated
        bytes.fromhex("08"),  # varint field with no payload
        bytes.fromhex("08ffffffffffffffffffff01"),  # varint wider than 64 bits
        bytes.fromhex("0d0102"),  # fixed32, truncated
    ],
)
def test_unparseable_wire_bytes_are_refused(data):
    with pytest.raises(WireError):
        Destination.FromString(data)


def test_a_field_of_the_wrong_wire_type_is_refused():
    # Destination.host (field 1) as a varint instead of a string.
    with pytest.raises(WireError, match="length-delimited"):
        Destination.FromString(bytes.fromhex("0807"))
    # Destination.port (field 3) as bytes instead of a varint.
    with pytest.raises(WireError, match="varint"):
        Destination.FromString(bytes.fromhex("1a01ff"))


def test_negative_varints_cannot_be_encoded():
    with pytest.raises(WireError, match="negative"):
        Operation(id=b"\x01", guest_ns=-1).SerializeToString()


# ---------------------------------------------------------------------------
# The policy grammar's remaining refusals and matches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "release outgoing 0x10000",  # ethertype out of range
        "release outgoing 1.2.3.4:443/300",  # proto out of range
        "release outgoing 1.2.3.4:abc/tcp",  # port not a number
        "release outgoing :443/tcp",  # no ip at all
        "release outgoing 1.2.3.4:443",  # no proto separator
    ],
)
def test_more_unreadable_policy_lines(line):
    with pytest.raises(PolicyError):
        Policy.parse(line)


def test_numeric_ethertype_and_proto_parse():
    policy = Policy.parse("release outgoing 0x88cc\nrelease incoming 1.2.3.4:443/47\n")
    lldp, gre = policy.grants
    assert lldp.ethertype == 0x88CC
    assert gre.proto == 47
    # And the canonical rendering survives a round trip.
    assert set(Policy.parse(policy.render()).grants) == set(policy.grants)


def test_grant_shapes_do_not_cross():
    ipv4_grant = Grant(direction="outgoing", ip="1.2.3.4", port=443, proto=6)
    l2_op = Operation(
        id=b"\x41", destination=Destination(ethertype=ETHERTYPE_ARP, mac=b"\xff" * 6)
    )
    assert not ipv4_grant.matches(l2_op)
    l2_grant = Grant(direction="outgoing", ethertype=ETHERTYPE_ARP)
    ipv4_op = Operation(
        id=b"\x42", destination=Destination(ip=bytes([1, 2, 3, 4]), port=443, proto=6)
    )
    assert not l2_grant.matches(ipv4_op)
    assert not l2_grant.matches(Operation(id=b"\x43"))


def test_grant_for_refuses_an_unnamed_l2_frame():
    unnamed = Operation(id=b"\x44", destination=Destination(mac=b"\xff" * 6))
    assert grant_for(unnamed) is None


def test_loading_a_missing_policy_file_is_an_error(tmp_path):
    with pytest.raises(PolicyError, match="cannot read"):
        Policy.load(tmp_path / "absent.policy")


# ---------------------------------------------------------------------------
# The CLI and the serve loop
# ---------------------------------------------------------------------------


def test_listen_addresses_must_be_host_port():
    with pytest.raises(argparse.ArgumentTypeError, match="want host:port"):
        _parse_listen("no-port")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_listen("host:not-a-number")
    assert _parse_listen("127.0.0.1:0") == ("127.0.0.1", 0)


@pytest.mark.asyncio
async def test_bound_port_refuses_an_unstarted_server():
    # Constructed inside a running loop (grpclib requires one), but
    # never started: there is no bound socket to report.
    with pytest.raises(RuntimeError, match="not listening"):
        bound_port(Server([]))


@pytest.fixture
def captured_judge(monkeypatch):
    """Run main() with asyncio.run captured; return the judge it built."""
    import titanium.environments.cella.engine as engine_module

    seen = {}

    def fake_run(coro):
        coro.close()

    monkeypatch.setattr(engine_module.asyncio, "run", fake_run)
    original = engine_module._run

    def spy_run(host, port, policy):
        seen["judge"] = policy
        return original(host, port, policy)

    monkeypatch.setattr(engine_module, "_run", spy_run)

    def invoke(argv):
        main(argv)
        return seen["judge"]

    return invoke


def test_cli_dry_run_needs_a_policy_path(captured_judge):
    with pytest.raises(SystemExit):
        captured_judge(["--listen", "127.0.0.1:0", "--dry-run"])


def test_cli_dry_run_builds_a_recorder(captured_judge, tmp_path):
    judge = captured_judge(
        ["--listen", "127.0.0.1:0", "--policy", str(tmp_path / "p"), "--dry-run"]
    )
    assert judge.recorder is not None
    assert judge.policy is None


def test_cli_enforces_an_existing_policy_file(captured_judge, tmp_path):
    path = tmp_path / "cella.policy"
    path.write_text("release outgoing 1.2.3.4:443/tcp\n")
    judge = captured_judge(["--listen", "127.0.0.1:0", "--policy", str(path)])
    assert judge.recorder is None
    assert len(judge.policy.grants) == 1


def test_cli_warns_and_fails_closed_on_a_missing_policy_file(
    captured_judge, tmp_path, caplog
):
    judge = captured_judge(
        ["--listen", "127.0.0.1:0", "--policy", str(tmp_path / "absent")]
    )
    assert judge.policy is None
    assert any("does not exist" in message for message in caplog.messages)


def test_cli_with_no_policy_at_all_fails_closed(captured_judge):
    judge = captured_judge(["--listen", "127.0.0.1:0"])
    assert judge.policy is None
    assert judge.recorder is None


@pytest.mark.asyncio
async def test_run_serves_until_closed(monkeypatch):
    import titanium.environments.cella.engine as engine_module

    class FakeServer:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    fake = FakeServer()

    async def fake_serve(policy, host, port):
        return fake

    monkeypatch.setattr(engine_module, "serve", fake_serve)
    monkeypatch.setattr(engine_module, "bound_port", lambda server: 12345)
    await asyncio.wait_for(_run("127.0.0.1", 0, PolicyJudge()), timeout=5)


# ---------------------------------------------------------------------------
# Accord 4: membrane memory
# ---------------------------------------------------------------------------


def test_a_windowed_grant_plants_memory_once():
    policy = Policy.parse(
        "release outgoing 1.1.1.1:443/tcp (keep_open=5m) (skip_freeze=true)\n"
    )
    judge = PolicyJudge(policy=policy)
    op = _op(ip=(1, 1, 1, 1), port=443, proto=6)
    first = judge.decide(op)
    # First park: verdict, then the standing memory for the exact dest.
    assert len(first) == 2
    assert first[0].release is not None
    mem = first[1].membrane_memory
    assert mem is not None and mem.skip_freeze and mem.keep_open == 300
    assert mem.destination.port == 443 and first[1].id == b""
    # Second park to the same dest: verdict only, memory already planted.
    second = judge.decide(op)
    assert len(second) == 1 and second[0].release is not None


def test_a_wildcard_grant_remembers_each_concrete_destination():
    policy = Policy.parse(
        "release outgoing *:443/tcp (keep_open=5m) (skip_freeze=true)\n"
    )
    judge = PolicyJudge(policy=policy)
    a = judge.decide(_op(ip=(104, 20, 23, 154), port=443))
    b = judge.decide(_op(ip=(1, 1, 1, 1), port=443))
    # Each concrete IP gets its own exact memory, though the grant is *.
    assert a[1].membrane_memory.destination.ip == bytes([104, 20, 23, 154])
    assert b[1].membrane_memory.destination.ip == bytes([1, 1, 1, 1])


def test_concrete_grants_pre_plant_at_stream_open_but_names_do_not():
    # Like cella-engine motor: ARP and exact-ip grants pre-plant their
    # skip_freeze memory before any crossing, so the first ARP never
    # freezes and the wire comes up at once. Host and wildcard-ip grants
    # name no concrete destination, so they are not pre-planted.
    policy = Policy.parse(
        "release outgoing arp (keep_open=24h) (skip_freeze=true)\n"
        "release outgoing 10.77.0.1:443/tcp (keep_open=5m) (skip_freeze=true)\n"
        "release incoming 10.77.0.1:443/tcp\n"
        "release outgoing example.com:443/tcp (keep_open=5m) (skip_freeze=true)\n"
        "release outgoing *:53/udp (keep_open=90s) (skip_freeze=true)\n"
    )
    judge = PolicyJudge(policy=policy)
    standing = judge.standing_decisions()
    dests = [d.membrane_memory.destination for d in standing]
    # ARP (ethertype) and the exact ip -- not the host, not the wildcard.
    assert any(x.ethertype == ETHERTYPE_ARP and not x.ip for x in dests)
    assert any(bytes(x.ip) == bytes([10, 77, 0, 1]) and x.port == 443 for x in dests)
    assert len(standing) == 2
    assert all(d.membrane_memory.skip_freeze for d in standing)
    # Pre-planted destinations are not re-planted reactively.
    again = judge.decide(_op(ip=(10, 77, 0, 1), port=443, proto=6))
    assert len(again) == 1 and again[0].release is not None


def test_the_memory_lapses_and_re_plants_when_keep_open_elapses():
    from titanium.environments.cella.engine import MembraneMemoryTable

    table = MembraneMemoryTable()
    key = ("ip", bytes([1, 1, 1, 1]), 443, 6)
    # First crossing at t=0 plants (300s window); a later crossing while
    # remembered does not.
    assert table.plant(key, keep_open=300, now=0.0) is True
    assert table.plant(key, keep_open=300, now=100.0) is False
    # After the window lapses, the destination is unplanted again and the
    # next crossing re-plants -- the remembered -> unplanted edge.
    assert table.plant(key, keep_open=300, now=301.0) is True
    # reset() empties the whole circuit (a fresh machine).
    table.reset()
    assert table.plant(key, keep_open=300, now=302.0) is True


def test_an_incoming_grant_never_plants_a_memory():
    # An incoming park never freezes, so an incoming memory is
    # meaningless -- and cella keys a memory by destination alone, so an
    # incoming memory (skip_freeze=False) would collide with and suppress
    # the outgoing leg's skip_freeze=True memory for the same
    # destination. The incoming leg must plant nothing.
    policy = Policy.parse("release incoming 10.77.0.2:50002/udp (keep_open=1h)\n")
    decisions = PolicyJudge(policy=policy).decide(
        _op(ip=(10, 77, 0, 2), port=50002, proto=17, direction=DIRECTION_INCOMING)
    )
    assert len(decisions) == 1  # verdict only, no memory


def test_a_grant_without_a_window_plants_no_memory():
    policy = Policy.parse("release outgoing 1.1.1.1:443/tcp\n")
    decisions = PolicyJudge(policy=policy).decide(_op(ip=(1, 1, 1, 1), port=443))
    assert len(decisions) == 1  # verdict only: the park is the freeze


def test_a_standing_refusal_plants_a_skip_freeze_memory_once():
    # The negative-probe pattern (cella docs MEMBRANE-MEMORY.md): a
    # refuse line carrying skip_freeze plants a standing refusal, so
    # every SYN retransmit to the refused destination lapses live
    # instead of paying a full deep re-warm per park.
    policy = Policy.parse(
        "refuse outgoing 8.8.8.8:443/tcp "
        '(keep_open=24h) (skip_freeze=true) (reason="ungranted")\n'
    )
    judge = PolicyJudge(policy=policy)
    op = _op(ip=(8, 8, 8, 8), port=443, proto=6)
    first = judge.decide(op)
    # First park: the refusal verdict, then the standing memory.
    assert len(first) == 2
    assert first[0].refusal is not None and first[0].refusal.why == "ungranted"
    mem = first[1].membrane_memory
    assert mem is not None and mem.skip_freeze and mem.keep_open == 86400
    assert mem.destination.port == 443 and first[1].id == b""
    # Second park to the same dest: verdict only, memory already planted.
    second = judge.decide(op)
    assert len(second) == 1 and second[0].refusal is not None


def test_a_bare_refusal_is_deliberately_costly():
    # No window on the refuse line: no standing memory, so the park is
    # the freeze -- the deliberately costly refusal cella documents.
    policy = Policy.parse(
        'refuse outgoing 8.8.8.8:53/udp (reason="no public dns")\n'
    )
    decisions = PolicyJudge(policy=policy).decide(
        _op(ip=(8, 8, 8, 8), port=53, proto=17)
    )
    assert len(decisions) == 1
    assert decisions[0].refusal.why == "no public dns"


def test_the_window_sugar_round_trips():
    for text, seconds in [("90s", 90), ("5m", 300), ("24h", 86400), ("45", 45)]:
        g = Policy.parse(f"release outgoing 1.2.3.4:443/tcp (keep_open={text})\n")
        assert g.grants[0].keep_open == seconds
    # Render prefers the largest exact unit.
    rendered = Policy.parse(
        "release outgoing 1.2.3.4:443/tcp (keep_open=3600)\n"
    ).render()
    assert "keep_open=1h" in rendered
