"""The ``cella.policy`` judge: titanium's cella policy engine.

A cella machine with a world nic decides nothing for itself: every
border crossing parks, cella's bridge (``cella-engine <machine> --dial
<addr>``) streams each park to a gRPC engine as an ``Event``, and each
returned ``Decision`` -- a release or a refusal, by operation id -- is
what actually moves or stops the frame (cella docs/WORLD-ENGINE.md).
This module is the engine on the other end of that dial.

The engine knows nothing of ``allow_internet``, on purpose. The two
knobs are orthogonal: harbor's flag defines the network *topology*
(:mod:`titanium.environments.cella.environment` -- ``false`` is
``--net none``, where no border, no ledger, and no engine exist at
all), and ``cella.policy`` defines the crossing rules at a border
(:mod:`titanium.environments.cella.policy` -- ``outgoing`` grants are
the egress rules, ``incoming`` grants the ingress rules). An engine
only ever runs where a border exists, so the flag's whole meaning was
spent at ``cella create`` before this process started.

The one policy file travels in one of two directions:

- **Enforce** (the default): the policy file is read, a crossing a
  grant names is released, and everything else is refused as
  :data:`REFUSAL_WHY_NO_GRANT`. No file, or an empty one, grants
  nothing: fail closed.
- **Dry run** (``--dry-run``): every crossing is released and every
  distinct crossing is *written* to the policy file as a grant. The
  collected file is reviewed and checked in beside the task's build
  file, and the next run enforces it.

There are no unconditional releases in enforce mode. cella's motor
fixture lets ARP ride free; this engine deliberately does not -- ARP,
NDP, per-destination allows, all of it is ``cella.policy``'s to say,
not a hardcoded carve-out's. Every refusal lands in the chronicle, so
a task that needed a crossing shows exactly what it asked for.

Run standalone with ``python -m titanium.environments.cella.engine
--listen 127.0.0.1:50051 --policy cella.policy [--dry-run]``, or embed
via :func:`serve`. The transport is grpclib -- pure-Python asyncio, no
protoc codegen -- speaking the hand-carried vocabulary in
:mod:`titanium.environments.cella.wire`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from dataclasses import dataclass
from pathlib import Path

from grpclib.const import Cardinality, Handler
from grpclib.server import Server, Stream

from titanium.environments.cella.policy import Policy, PolicyRecorder
from titanium.environments.cella.wire import (
    Decision,
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


@dataclass(frozen=True)
class PolicyJudge:
    """The task's ``cella.policy`` grants as a per-crossing judge.

    A crossing a grant names is released; everything else is refused
    as :data:`REFUSAL_WHY_NO_GRANT`. ``policy=None`` is a border with
    no file behind it, which grants nothing: fail closed.

    A *recorder* replaces judgment with collection (``--dry-run``):
    every crossing is released and written to the policy file as the
    grant that would have released it. The grants are not consulted --
    a dry run exists to observe what the task asks for.
    """

    policy: Policy | None = None
    recorder: PolicyRecorder | None = None

    def decide(self, operation: Operation) -> Decision:
        if self.recorder is not None:
            self.recorder.record(operation)
            return Decision(id=operation.id, release=Release())
        if self.policy is not None and self.policy.grants_crossing(operation):
            return Decision(id=operation.id, release=Release())
        return Decision(id=operation.id, refusal=Refusal(why=REFUSAL_WHY_NO_GRANT))


class EngineService:
    """The cella.Engine service: Events in, Decisions out, one stream."""

    def __init__(self, policy: PolicyJudge) -> None:
        self._policy = policy

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
        while (event := await stream.recv_message()) is not None:
            operation = event.parked
            if operation is None:
                # Completions and looks are evidence, not questions.
                logger.debug("cella engine: event (not a park)")
                continue
            decision = self._policy.decide(operation)
            destination = operation.destination
            logger.info(
                "cella engine: %s id=%s host=%r ip=%s port=%d direction=%d",
                "release" if decision.release is not None else "refuse",
                operation.id.hex(),
                destination.host if destination else "",
                ".".join(str(b) for b in (destination.ip if destination else b"")),
                destination.port if destination else 0,
                operation.direction,
            )
            await stream.send_message(decision)


async def serve(policy: PolicyJudge, host: str = "127.0.0.1", port: int = 0) -> Server:
    """Start the engine listening on *host*:*port*; return the server.

    Port 0 binds an ephemeral port; read it back with
    :func:`bound_port`. The caller owns shutdown: ``server.close()``
    then ``await server.wait_closed()``.
    """
    server = Server([EngineService(policy)])
    await server.start(host, port)
    return server


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
