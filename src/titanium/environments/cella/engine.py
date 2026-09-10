"""The ``allow_internet`` judge: titanium's minimal cella policy engine.

A cella machine with a world nic decides nothing for itself: every
border crossing parks, cella's bridge (``cella-engine <machine> --dial
<addr>``) streams each park to a gRPC engine as an ``Event``, and each
returned ``Decision`` -- a release or a refusal, by operation id -- is
what actually moves or stops the frame (cella docs/WORLD-ENGINE.md).
This module is the engine on the other end of that dial.

``allow_internet`` from task.toml is a gate, not a grant. It says
whether internet is a *possibility* for the task; it releases nothing
by itself. What actually gets released is the per-task policy's to
decide -- the ``cella.policy`` file this engine will grow to read --
and that policy does not exist yet, so today this engine refuses every
crossing, with the why naming which wall it hit:

- ``allow_internet = false``: refused because internet is not a
  possibility at all. Per cella's titanium integration notes such a
  task should boot ``--net none`` and need no engine; this engine
  still answers so that a machine given a world nic by mistake fails
  closed and visibly instead of hanging on holds.
- ``allow_internet = true``: refused because no policy grants the
  crossing. The gate is open, the policy behind it is empty, and an
  empty policy grants nothing.

There are no unconditional releases. cella's motor fixture lets ARP
ride free; this engine deliberately does not -- ARP, NDP,
per-destination allows, all of it belongs to ``cella.policy``, not to
hardcoded carve-outs here. Every refusal lands in the chronicle, so a
task that needed a crossing shows exactly what it asked for.

Run standalone with ``python -m titanium.environments.cella.engine
--listen 127.0.0.1:50051 [--allow-internet]``, or embed via
:func:`serve`. The transport is grpclib -- pure-Python asyncio, no
protoc codegen -- speaking the hand-carried vocabulary in
:mod:`titanium.environments.cella.wire`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from dataclasses import dataclass

from grpclib.const import Cardinality, Handler
from grpclib.server import Server, Stream

from titanium.environments.cella.wire import (
    Decision,
    Event,
    Operation,
    Refusal,
)

logger = logging.getLogger(__name__)

# The full method name from ``service Engine { rpc Decide ... }`` in
# proto/cella.proto; the bridge dials exactly this.
DECIDE_METHOD = "/cella.Engine/Decide"

REFUSAL_WHY_DISABLED = "allow_internet is false for this task"
REFUSAL_WHY_NO_POLICY = (
    "allow_internet is true, but no cella.policy grants this crossing"
)


@dataclass(frozen=True)
class AllowInternetPolicy:
    """The ``allow_internet`` gate as a per-crossing judge.

    The flag decides only which refusal a crossing gets. Releases are
    the per-task ``cella.policy``'s to grant, and until that file is
    read here the policy behind an open gate is empty -- so ``decide``
    never releases; the granting policy is this type's designed
    successor, not a different seam.
    """

    allow_internet: bool

    def decide(self, operation: Operation) -> Decision:
        if not self.allow_internet:
            return Decision(id=operation.id, refusal=Refusal(why=REFUSAL_WHY_DISABLED))
        return Decision(id=operation.id, refusal=Refusal(why=REFUSAL_WHY_NO_POLICY))


class EngineService:
    """The cella.Engine service: Events in, Decisions out, one stream."""

    def __init__(self, policy: AllowInternetPolicy) -> None:
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


async def serve(
    policy: AllowInternetPolicy, host: str = "127.0.0.1", port: int = 0
) -> Server:
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


async def _run(host: str, port: int, policy: AllowInternetPolicy) -> None:
    server = await serve(policy, host, port)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, server.close)
    logger.info(
        "cella engine: listening on %s:%d (allow_internet=%s)",
        host,
        bound_port(server),
        policy.allow_internet,
    )
    await server.wait_closed()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m titanium.environments.cella.engine",
        description="Serve cella.Engine, deciding every crossing "
        "by the task's allow_internet flag.",
    )
    parser.add_argument(
        "--listen",
        required=True,
        type=_parse_listen,
        metavar="HOST:PORT",
        help="address to serve on (the bridge's --dial target)",
    )
    parser.add_argument(
        "--allow-internet",
        action="store_true",
        help="internet is a possibility for this task; releases still "
        "need a granting cella.policy, so every crossing is refused "
        "either way -- the flag only picks the recorded why",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    host, port = args.listen
    asyncio.run(_run(host, port, AllowInternetPolicy(args.allow_internet)))


if __name__ == "__main__":
    main()
