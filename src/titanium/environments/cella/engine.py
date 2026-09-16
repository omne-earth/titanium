"""The ``cella.policy`` judge: titanium's cella policy engine.

A cella machine decides nothing for itself: every border crossing
parks, cella's bridge (``cella-engine <machine> --dial <addr>``)
streams each park to a gRPC engine as an ``Event``, and each returned
``Decision`` -- a release, a refusal, or a membrane memory -- is what
actually moves, stops, or remembers the frame (cella
docs/WORLD-ENGINE.md). This module is the engine on the other end of
that dial.

The engine knows nothing of ``allow_internet``, on purpose. The two
knobs are orthogonal: harbor's flag defines the network *topology*
(:mod:`titanium.environments.cella.environment` -- ``false`` for an
agentless trial is ``--net none``, where no border, no ledger, and no
engine exist at all; otherwise the terminated pair), and
``cella.policy`` defines the crossing rules at a border
(:mod:`titanium.environments.cella.policy` -- ``outgoing`` grants are
the egress rules, ``incoming`` grants the ingress rules). An engine
only ever runs where a border exists, so the flag's whole meaning was
spent at ``cella create`` before this judge ever saw a crossing.

The one policy file travels in one of two directions:

- **Enforce** (the default): the policy file is read, a crossing a
  grant names is released, and everything else is refused as
  :data:`REFUSAL_WHY_NO_GRANT`. No file, or an empty one, grants
  nothing: fail closed.
- **Dry run** (``--dry-run``): every crossing is released and every
  distinct crossing is *written* to the policy file as a grant. The
  collected file is reviewed and checked in beside the task's build
  file, and the next run enforces it.

There are no unconditional releases in enforce mode: ARP, NDP, and
every finer exception is ``cella.policy``'s to say, not a hardcoded
carve-out's, and every refusal lands in the chronicle. But a *granted*
crossing need not freeze: like cella's reference engine
(``cella-engine motor``), this judge plants a **membrane memory** for a
windowed grant, so a remembered destination waits live instead of
freezing. That lifecycle is an explicit per-destination state machine
(:class:`MembraneMemoryTable`) -- pre-planted at stream open for a
concrete destination so the first ARP never freezes, reset per machine,
and lapsed when ``keep_open`` clears (docs/environments/CELLA.md, "The
membrane-memory state machine").

Titanium runs the engine **in-process** -- :func:`serve` on a
background asyncio loop, one server per machine -- so nothing is spawned
and no process boundary is crossed per exec cycle. The standalone
``python -m titanium.environments.cella.engine --listen HOST:PORT
--policy cella.policy [--dry-run]`` entry point remains for debugging.
The transport is grpclib -- pure-Python asyncio, no protoc codegen --
speaking the hand-carried vocabulary in
:mod:`titanium.environments.cella.wire`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time
from pathlib import Path

from grpclib.const import Cardinality, Handler
from grpclib.server import Server, Stream

from titanium.environments.cella.policy import Policy, PolicyRecorder
from titanium.environments.cella.wire import (
    Decision,
    Destination,
    Event,
    Operation,
    Refusal,
    Release,
)

logger = logging.getLogger(__name__)

# The full method name from ``service Engine { rpc Decide ... }`` in
# proto/cella.proto; the bridge dials exactly this.
DECIDE_METHOD = "/cella.Engine/Decide"

REFUSAL_WHY_NO_GRANT = "no cella.policy grants this crossing"


def _memory_key(destination: Destination) -> tuple:
    """The exact identity of a destination, for planting each membrane
    memory once. A memory names an exact destination, so two crossings
    to the same (ip, port, proto) or the same ethertype share one."""
    if destination.ip:
        return ("ip", bytes(destination.ip), destination.port, destination.proto)
    return ("l2", destination.ethertype)


class MembraneMemoryTable:
    """The membrane-memory lifecycle as an explicit state machine: one
    small automaton per destination, scoped to a single machine's bridge
    stream (docs/environments/CELLA.md, "The membrane-memory state
    machine").

        unplanted --(plant)--> remembered --(keep_open lapses)--> unplanted

    A destination is *remembered* while its ``keep_open`` window is live;
    cella clears its memory file when the window expires, so the
    destination lapses back to *unplanted* by the same arithmetic and the
    next crossing re-plants it. State is per machine: :meth:`reset` at
    each new stream, because every exec cycle is a fresh machine with an
    empty membrane memory."""

    def __init__(self) -> None:
        self._until: dict[tuple, float] = {}

    def reset(self) -> None:
        self._until.clear()

    def plant(self, key: tuple, keep_open: int, now: float) -> bool:
        """Move a destination to *remembered* when it is *unplanted* or
        its window has lapsed; return ``True`` when this call planted (so
        a memory must be emitted), ``False`` when it was already
        remembered (the verdict rides alone)."""
        deadline = self._until.get(key)
        if deadline is not None and now < deadline:
            return False
        self._until[key] = now + keep_open
        return True


class PolicyJudge:
    """The task's ``cella.policy`` grants as a per-crossing judge.

    ``decide`` returns the verdict for one park -- release, or refuse
    with the grant's reason (or :data:`REFUSAL_WHY_NO_GRANT` when no
    grant matches: default-refuse). When the matching grant carries a
    ``keep_open`` window, ``decide`` also returns a membrane memory to
    plant, built from the park's *exact* destination and sent once per
    destination -- so a remembered crossing waits live instead of
    freezing the machine.

    ``policy=None`` is a border with no file: grants nothing, fail
    closed. A *recorder* (``--dry-run``) replaces judgment with
    collection: every crossing releases and lands in the policy file.
    """

    def __init__(
        self,
        policy: Policy | None = None,
        recorder: PolicyRecorder | None = None,
    ) -> None:
        self.policy = policy
        self.recorder = recorder
        self._memory = MembraneMemoryTable()

    def reset(self) -> None:
        """Reset the memory circuit -- called when a new bridge stream
        opens. Each stream serves a fresh machine with an empty membrane
        memory, so every machine is planted from scratch: pre-planted at
        open and reactively as it crosses. A global memory would plant
        only the first machine's, leaving every later exec cycle to
        freeze on its first ARP again."""
        self._memory.reset()

    def standing_decisions(self) -> list[Decision]:
        """The memories to pre-plant when a bridge stream opens, before
        any Event -- the reference engine (cella-engine motor) does this
        so the first crossing to a granted *concrete* destination (ARP,
        an exact ip:port/proto) never freezes. Without it, every
        destination's first crossing freezes once (ARP included), which
        under load wedges the wire at ARP."""
        if self.policy is None or self.recorder is not None:
            return []
        now = time.monotonic()
        decisions = []
        for grant in self.policy.grants:
            memory = grant.standing_memory()
            if memory is None:
                continue
            key = _memory_key(memory.destination)
            if self._memory.plant(key, memory.keep_open, now):
                decisions.append(Decision(id=b"", membrane_memory=memory))
        return decisions

    def decide(self, operation: Operation) -> list[Decision]:
        if self.recorder is not None:
            self.recorder.record(operation)
            return [Decision(id=operation.id, release=Release())]

        if self.policy is None:
            return [
                Decision(id=operation.id, refusal=Refusal(why=REFUSAL_WHY_NO_GRANT))
            ]

        match = self.policy.evaluate(operation)
        if match.release:
            verdict = Decision(id=operation.id, release=Release())
        else:
            why = match.reason or REFUSAL_WHY_NO_GRANT
            verdict = Decision(id=operation.id, refusal=Refusal(why=why))

        decisions = [verdict]
        # Plant the standing memory when the destination is unplanted or
        # its window has lapsed: the bridge stamps and appends it, and
        # the membrane stops freezing on that crossing. id empty -- a
        # memory names a destination, not an operation.
        if match.memory is not None and operation.destination is not None:
            key = _memory_key(operation.destination)
            if self._memory.plant(key, match.memory.keep_open, time.monotonic()):
                decisions.append(Decision(id=b"", membrane_memory=match.memory))
        return decisions


class EngineService:
    """The cella.Engine service: Events in, Decisions out, one stream."""

    def __init__(
        self, policy: PolicyJudge, engine_logger: logging.Logger | None = None
    ) -> None:
        self._policy = policy
        # A per-machine logger when run in-process (one log file per vm),
        # the module logger when run standalone.
        self._logger = engine_logger or logger
        # Per-verdict latency: with cella's event-driven ledger tail a park
        # reaches the judge in ~1ms, so the engine's own decision cost is
        # the new throughput ceiling. Track it (the judge is a dict lookup,
        # so this should stay well under a millisecond) and report the
        # summary when the stream ends.
        self._verdict_count = 0
        self._verdict_total_ns = 0
        self._verdict_max_ns = 0

    def __mapping__(self) -> dict[str, Handler]:
        return {
            DECIDE_METHOD: Handler(
                self.Decide,
                Cardinality.STREAM_STREAM,
                Event,
                Decision,
            )
        }

    async def Decide(self, stream: Stream[Event, Decision]) -> None:
        # Answer with response headers before waiting for anything:
        # tonic clients (cella's bridge) block inside their call until
        # the server's initial metadata arrives, and only then start
        # streaming Events. grpclib defers metadata until the first
        # message by default, which deadlocks the two -- each side
        # waiting for the other, measured against the real bridge.
        await stream.send_initial_metadata()
        # A new stream is a new machine with an empty membrane memory:
        # plant it from scratch.
        self._policy.reset()
        # Pre-plant the standing memories before the first Event (as
        # cella-engine motor does): the first crossing to each granted
        # concrete destination -- ARP, the appliance's ports, the reply
        # window -- then never freezes, so the wire comes up at once
        # instead of wedging on a first-ARP freeze under load.
        try:
            for decision in self._policy.standing_decisions():
                memory = decision.membrane_memory
                self._logger.info(
                    "cella engine: pre-plant ip=%s port=%d ethertype=0x%04x keep_open=%d",
                    ".".join(str(b) for b in memory.destination.ip),
                    memory.destination.port,
                    memory.destination.ethertype,
                    memory.keep_open,
                )
                await stream.send_message(decision)
            while (event := await stream.recv_message()) is not None:
                operation = event.parked
                if operation is None:
                    # Completions and looks are evidence, not questions.
                    self._logger.debug("cella engine: event (not a park)")
                    continue
                destination = operation.destination
                started_ns = time.perf_counter_ns()
                decisions = self._policy.decide(operation)
                elapsed_ns = time.perf_counter_ns() - started_ns
                self._verdict_count += 1
                self._verdict_total_ns += elapsed_ns
                self._verdict_max_ns = max(self._verdict_max_ns, elapsed_ns)
                for decision in decisions:
                    if decision.membrane_memory is not None:
                        self._logger.info(
                            "cella engine: remember ip=%s port=%d skip_freeze=%s keep_open=%d",
                            ".".join(
                                str(b) for b in (destination.ip if destination else b"")
                            ),
                            destination.port if destination else 0,
                            decision.membrane_memory.skip_freeze,
                            decision.membrane_memory.keep_open,
                        )
                    else:
                        self._logger.info(
                            "cella engine: %s id=%s host=%r ip=%s port=%d direction=%d",
                            "release" if decision.release is not None else "refuse",
                            operation.id.hex(),
                            destination.host if destination else "",
                            ".".join(
                                str(b) for b in (destination.ip if destination else b"")
                            ),
                            destination.port if destination else 0,
                            operation.direction,
                        )
                    await stream.send_message(decision)
        except (ConnectionError, OSError, asyncio.CancelledError) as exc:
            # The bridge (`cella-engine <machine> --dial`) exits with its
            # machine when an exec cycle ends, dropping this stream. That
            # is a normal teardown, not a fault -- this engine persists,
            # and the next cycle's bridge reconnects. Say so plainly
            # instead of grpclib's bare "Request was cancelled: Connection
            # lost" (its own logger is quieted in main()).
            self._logger.info(
                "cella engine: bridge closed (%s) -- cycle ended, awaiting reconnect",
                type(exc).__name__,
            )
            if isinstance(exc, asyncio.CancelledError):
                raise  # cancellation must propagate
        finally:
            # The engine's own decision cost, now that cella delivers each
            # park in ~1ms: this is what would re-throttle throughput if it
            # were not tiny. Reported per stream (cumulative for the engine).
            if self._verdict_count:
                self._logger.info(
                    "cella engine: %d verdicts, avg %d ns, max %d ns",
                    self._verdict_count,
                    self._verdict_total_ns // self._verdict_count,
                    self._verdict_max_ns,
                )


async def serve(
    policy: PolicyJudge,
    host: str = "127.0.0.1",
    port: int = 0,
    engine_logger: logging.Logger | None = None,
) -> Server:
    """Start the engine listening on *host*:*port*; return the server.

    Port 0 binds an ephemeral port; read it back with
    :func:`bound_port`. ``engine_logger`` routes this engine's lines to
    a per-machine file when embedded in-process. The caller owns
    shutdown: ``server.close()`` then ``await server.wait_closed()``.
    """
    server = Server([EngineService(policy, engine_logger)])
    await server.start(host, port)
    return server


def build_judge(policy_path: Path | None, dry_run: bool) -> PolicyJudge:
    """Build the judge titanium serves in-process: a recorder in
    dry-run, otherwise the enforcing policy (a missing file fails
    closed, refusing every crossing)."""
    if dry_run:
        if policy_path is None:
            raise ValueError("dry-run needs a policy path to collect into")
        return PolicyJudge(recorder=PolicyRecorder(policy_path))
    policy = None
    if policy_path is not None and policy_path.exists():
        policy = Policy.load(policy_path)
    return PolicyJudge(policy=policy)


def bound_port(server: Server) -> int:
    """The port a started server actually listens on.

    grpclib exposes no public accessor for the bound address, so this
    reads the underlying asyncio server's first socket -- needed
    whenever :func:`serve` was given port 0.
    """
    if server._server is None or not server._server.sockets:
        raise RuntimeError("server is not listening")
    port: int = server._server.sockets[0].getsockname()[1]
    return port


def _parse_listen(listen: str) -> tuple[str, int]:
    host, sep, port = listen.rpartition(":")
    if not sep or not host:
        raise argparse.ArgumentTypeError(f"--listen {listen!r}: want host:port")
    try:
        return host, int(port)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--listen {listen!r}: {exc}") from exc


async def _run(host: str, port: int, policy: PolicyJudge) -> None:
    server = await serve(policy, host, port)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, server.close)
    logger.info(
        "cella engine: listening on %s:%d (mode=%s)",
        host,
        bound_port(server),
        "dry-run" if policy.recorder is not None else "enforce",
    )
    await server.wait_closed()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m titanium.environments.cella.engine",
        description="Serve cella.Engine, deciding every crossing by the "
        "task's cella.policy grants.",
    )
    parser.add_argument(
        "--listen",
        required=True,
        type=_parse_listen,
        metavar="HOST:PORT",
        help="address to serve on (the bridge's --dial target)",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        metavar="CELLA_POLICY",
        help="the per-task cella.policy file: read and enforced by "
        "default, written when --dry-run is given",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="release every crossing and write each one observed to "
        "--policy as a grant, instead of enforcing the file",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # grpclib logs every bridge disconnect at INFO as "Request was
    # cancelled: Connection lost" -- one per exec-cycle teardown, which
    # is normal and which Decide already reports in cella's own words.
    # Quiet grpclib's bare version so the engine log stays readable.
    logging.getLogger("grpclib.server").setLevel(logging.WARNING)
    host, port = args.listen

    if args.dry_run:
        if args.policy is None:
            parser.error("--dry-run needs --policy: the collected grants go there")
        judge = PolicyJudge(recorder=PolicyRecorder(args.policy))
    else:
        policy = None
        if args.policy is not None and args.policy.exists():
            policy = Policy.load(args.policy)
            logger.info(
                "cella engine: enforcing %s (%d grants)",
                args.policy,
                len(policy.grants),
            )
        elif args.policy is not None:
            # Fail closed, out loud: an absent file grants nothing, and
            # the operator should hear that before the first refusal.
            logger.warning(
                "cella engine: %s does not exist; every crossing will be "
                "refused (collect one with --dry-run)",
                args.policy,
            )
        judge = PolicyJudge(policy=policy)

    asyncio.run(_run(host, port, judge))


if __name__ == "__main__":
    main()
