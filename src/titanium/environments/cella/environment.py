"""The cella environment: titanium driving cella as its runtime.

This module grows into the ``--env cella`` environment class. Its first
resident is the one decision every cella machine is created under: the
task's ``allow_internet`` flag **defines the network topology**, not a
firewall posture inside one.

``allow_internet`` is harbor's knob -- the task author's declaration,
which every rung must honor however it can. On the cella rung the two
values are two different machines:

- ``false`` -- ``--net none``. No nic exists. Not a shut valve on a
  network: no translator, no membrane traffic, no ledger, and none of
  the judgment machinery (no ``gateway open``, no engine, no bridge).
- ``true`` -- ``--net world``, then ``cella gateway <vm> open`` after
  start. Open is the membrane, not a free path: every crossing parks
  for a decision, and the engine enforcing the task's ``cella.policy``
  (titanium+cella's knob, subordinate to harbor's) is what answers.

The mapping is total and closed: there is no third topology, and no
kwarg reopens the question somewhere else.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NetworkTopology:
    """What ``allow_internet`` means at the cella verbs.

    Attributes:
        net: The ``--net`` argument to ``cella create``.
        open_gateway: Whether ``cella gateway <vm> open`` runs after
            start. Meaningless without a nic; load-bearing with one --
            the valve is born closed, and open is what makes crossings
            park instead of nothing moving at all.
        judged: Whether the judgment loop must run for this machine:
            the policy engine serving the task's ``cella.policy``, and
            cella's bridge streaming the parks to it. A ``--net none``
            machine has nothing to judge.
    """

    net: str
    open_gateway: bool
    judged: bool


def network_topology(allow_internet: bool) -> NetworkTopology:
    """The topology the task's ``allow_internet`` declaration defines."""
    if allow_internet:
        return NetworkTopology(net="world", open_gateway=True, judged=True)
    return NetworkTopology(net="none", open_gateway=False, judged=False)
